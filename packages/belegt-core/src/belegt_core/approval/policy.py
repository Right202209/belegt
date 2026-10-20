# Spec: 06 §4.3（各级审批要求）、§4.4（approval-policy@1.0.0、non_answer_subjects、floors、版本化与租户覆盖 org-overlay、
#       policy_version 后缀 +ov<N>）；FR-408
"""审批策略加载与租户覆盖校验。"""
from __future__ import annotations

import copy
import fnmatch
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..authz import Actor
from ..errors import GraphError

POLICY_FILE = Path(__file__).parent / "approval-policy-1.0.0.yaml"
LEVELS = ("L0", "L1", "L2", "L3")
LEVEL_ORDER = {lvl: i for i, lvl in enumerate(LEVELS)}
OVERLAY_ALLOWED_TOP = {"thresholds", "customer_tiers", "levels", "batch_undo_window_minutes", "special"}
OVERLAY_ALLOWED_LEVEL = {"sla_workdays", "batch"}
OVERLAY_ALLOWED_BATCH = {"max_items"}
ROLE_SET = {"compliance_owner", "executive"}


class PolicyOverlayError(GraphError):
    def __init__(self, message: str, path: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__("GRAPH_SCHEMA_INVALID", message, rule_id="approval-policy.floors", path=path,
                         details={**(details or {}), "alert": "org_admin"})


def max_level(*levels: str | None) -> str:
    ls = [lvl for lvl in levels if lvl]
    return max(ls, key=lambda x: LEVEL_ORDER[x]) if ls else "L0"


@dataclass
class ApprovalPolicy:
    raw: dict[str, Any]
    overlay_version: int | None = None
    overlay: dict[str, Any] = field(default_factory=dict)

    # --------------------------------------------------------------- 标识
    @property
    def version(self) -> str:
        base = f"{self.raw['policy_id']}@{self.raw['version']}"
        return f"{base}+ov{self.overlay_version}" if self.overlay_version else base

    @property
    def base_version(self) -> str:
        return f"{self.raw['policy_id']}@{self.raw['version']}"

    def approval_fields(self) -> dict[str, Any]:
        """写入 Approval 的策略字段。03 approval.json 的 ``policy_version`` 模式不接受 ``+ov<N>`` 后缀，
        因此写基础版本，覆盖版本以标签 ``policy_overlay:ov<N>`` 记录（见 README"与文档出入"）。"""
        out: dict[str, Any] = {"policy_version": self.base_version}
        if self.overlay_version:
            out["tags"] = [f"policy_overlay:ov{self.overlay_version}"]
        return out

    # --------------------------------------------------------------- 级别
    def level(self, lvl: str) -> dict[str, Any]:
        return self.raw["levels"][lvl]

    def roles_for(self, lvl: str, *, legal_commitment: bool = False, customer_tier: str | None = None) -> list[str]:
        roles = list(self.level(lvl)["roles"])
        if lvl == "L3":
            for sp in self.raw.get("special") or []:
                if _special_match(sp.get("when", ""), legal_commitment, customer_tier):
                    roles = list(sp["roles"])
        return roles

    def batch_allowed(self, lvl: str) -> bool:
        return bool(self.level(lvl).get("batch", {}).get("allowed")) and lvl != "L3"

    def batch_cfg(self, lvl: str) -> dict[str, Any]:
        return dict(self.level(lvl).get("batch") or {})

    def max_items(self, lvl: str) -> int:
        return int(self.batch_cfg(lvl).get("max_items") or 0)

    def sla_workdays(self, lvl: str) -> int:
        return int(self.level(lvl).get("sla_workdays", 3))

    def confirmations(self, lvl: str) -> list[str]:
        return list(self.level(lvl).get("confirm") or [])

    # --------------------------------------------------------------- 阈值与分类
    def threshold(self, name: str) -> Decimal:
        return Decimal(str(self.raw["thresholds"][name]))

    def customer_tier(self, cp_ids: Iterable[str]) -> str:
        tiers = self.raw.get("customer_tiers") or {}
        found = "tier_c"
        for cp in cp_ids:
            if cp in (tiers.get("tier_a") or []):
                return "tier_a"
            if cp in (tiers.get("tier_b") or []):
                found = "tier_b"
        return found

    def is_emission(self, dp: str) -> bool:
        base = dp.split("@", 1)[0]
        return any(fnmatch.fnmatchcase(base, p) for p in self.raw.get("emission_datapoint_patterns") or [])

    @property
    def commitment_tags(self) -> set[str]:
        return set(self.raw.get("commitment_datapoint_tags") or [])

    @property
    def undo_window_minutes(self) -> int:
        return int(self.raw.get("batch_undo_window_minutes", 10))

    @property
    def escalation(self) -> dict[str, Any]:
        return dict(self.raw.get("escalation") or {"after_sla_workdays": 1, "to": ["executive"]})

    def non_answer(self, kind: str) -> dict[str, Any]:
        return dict((self.raw.get("non_answer_subjects") or {})[kind])


def _special_match(expr: str, legal: bool, tier: str | None) -> bool:
    from ..consistency.cel import CelError, compile_expr

    try:
        return compile_expr(expr).evaluate_bool({"legal_commitment": bool(legal), "customer_tier": tier or ""})
    except CelError:
        return True  # 宁严勿宽：策略不可求值 → 采用特殊（更严）角色


def _check_floors(p: dict[str, Any]) -> None:
    if p["levels"]["L3"].get("batch", {}).get("allowed"):
        raise PolicyOverlayError("floor violated: L3.batch.allowed == false", path="/levels/L3/batch/allowed")
    for lvl in LEVELS:
        roles = set(p["levels"][lvl].get("roles") or [])
        if not roles or not roles <= ROLE_SET:
            raise PolicyOverlayError(f"floor violated: approver roles for {lvl} must be compliance_owner/executive",
                                     path=f"/levels/{lvl}/roles")
    if "supplier_request_first_send" not in (p.get("non_answer_subjects") or {}):
        raise PolicyOverlayError("floor violated: supplier_request_first_send => approval_required")


def load_policy(overlay: dict[str, Any] | None = None, overlay_version: int | None = None, *,
                approvers: Iterable[Actor] = (), path: str | Path | None = None) -> ApprovalPolicy:
    """加载 ``approval-policy@1.0.0``，可选应用租户覆盖（``org-overlay``）。

    覆盖须 ``compliance_owner`` 与 ``org_admin`` 双人确认（``approvers``）；任何放宽 ``floors`` 或修改不允许键的覆盖
    抛 ``PolicyOverlayError``（details.alert=org_admin）。
    """
    raw = yaml.safe_load(Path(path or POLICY_FILE).read_text("utf-8"))
    _check_floors(raw)
    if not overlay:
        return ApprovalPolicy(raw)
    approvers = list(approvers)
    roles = {r for a in approvers for r in a.roles}
    ids = {a.id for a in approvers}
    if not {"compliance_owner", "org_admin"} <= roles or len(ids) < 2:
        raise PolicyOverlayError("overlay requires dual confirmation by compliance_owner and org_admin (06 §4.4)",
                                 path="/approvers")
    if not overlay_version or overlay_version < 1:
        raise PolicyOverlayError("overlay_version (N >= 1) required", path="/overlay_version")
    p = copy.deepcopy(raw)
    for k, v in overlay.items():
        if k not in OVERLAY_ALLOWED_TOP:
            raise PolicyOverlayError(f"overlay may not modify {k!r}", path=f"/{k}")
        if k == "thresholds":
            for tk, tv in v.items():
                if tk not in p["thresholds"]:
                    raise PolicyOverlayError(f"unknown threshold {tk!r}", path=f"/thresholds/{tk}")
                if Decimal(str(tv)) < 0:
                    raise PolicyOverlayError("thresholds must be >= 0", path=f"/thresholds/{tk}")
                p["thresholds"][tk] = str(tv)
        elif k == "customer_tiers":
            for tier, members in v.items():
                if tier not in ("tier_a", "tier_b", "tier_c"):
                    raise PolicyOverlayError(f"unknown tier {tier!r}", path=f"/customer_tiers/{tier}")
                p["customer_tiers"][tier] = members
        elif k == "batch_undo_window_minutes":
            if not (5 <= int(v) <= 60):
                raise PolicyOverlayError("batch_undo_window_minutes must be 5–60", path="/batch_undo_window_minutes")
            p[k] = int(v)
        elif k == "levels":
            for lvl, cfg in v.items():
                if lvl not in LEVELS:
                    raise PolicyOverlayError(f"unknown level {lvl!r}", path=f"/levels/{lvl}")
                for ck, cv in cfg.items():
                    if ck not in OVERLAY_ALLOWED_LEVEL:
                        raise PolicyOverlayError(f"overlay may not modify levels.{lvl}.{ck}", path=f"/levels/{lvl}/{ck}")
                    if ck == "sla_workdays":
                        if int(cv) < 1:
                            raise PolicyOverlayError("sla_workdays must be >= 1", path=f"/levels/{lvl}/sla_workdays")
                        p["levels"][lvl]["sla_workdays"] = int(cv)
                    else:
                        for bk, bv in cv.items():
                            if bk not in OVERLAY_ALLOWED_BATCH:
                                raise PolicyOverlayError(f"overlay may not modify levels.{lvl}.batch.{bk}",
                                                         path=f"/levels/{lvl}/batch/{bk}")
                            if lvl == "L3":
                                raise PolicyOverlayError("floor violated: L3.batch.allowed == false", path="/levels/L3/batch")
                            if int(bv) < 1:
                                raise PolicyOverlayError("max_items must be >= 1", path=f"/levels/{lvl}/batch/max_items")
                            p["levels"][lvl]["batch"]["max_items"] = int(bv)
        elif k == "special":
            for sp in v:
                if not set(sp.get("roles") or []) or not set(sp["roles"]) <= ROLE_SET:
                    raise PolicyOverlayError("special may only tighten roles to compliance_owner/executive", path="/special")
            p["special"] = list(p.get("special") or []) + list(v)
    _check_floors(p)
    return ApprovalPolicy(p, overlay_version, overlay)


_DEFAULT: ApprovalPolicy | None = None


def default_policy() -> ApprovalPolicy:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_policy()
    return _DEFAULT


__all__ = ["ApprovalPolicy", "load_policy", "default_policy", "PolicyOverlayError", "max_level", "LEVELS", "LEVEL_ORDER"]

# Spec: 06 §4.2–§4.4（判定表、各级审批要求、approval-policy@1.0.0、non_answer_subjects.supplier_request_first_send）、
#       §5.1-5（批准人角色）、§6.3（法律题 L3）；04 §2.8（approval-router：定级、required_role、Approval(pending)、
#       pending 失效作废重建；策略不可用全部 L3）；03 §3.7/§3.8（Answer/Approval 状态机、stale 机制）；08 §2.3（首次外发确认）
"""approval-router（确定性）。

    router = ApprovalRouter(store)
    res = router.route(org_id, request_id="req_…")        # draft → in_review + Approval(pending)
    router.decide("apr_…", actor=co, decision="approved", confirmations={...})
    router.decide_supplier_first_send("sreq_…", actor=co, decision="approved")
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable

from ..authz import Actor
from ..errors import GraphError
from ..ids import new_id
from ..store import WriteItem
from ..consistency.context import AuditContext, parse_date, ref_of, split_ref, tag_map
from ..consistency.meta import MetaProvider
from .policy import ApprovalPolicy, default_policy
from ..legal.flags import question_legal_flag
from .risk import RiskDecision, assess

ROUTER_ID = "agent:approval-router"


def add_workdays(start: dt.date, n: int) -> dt.date:
    d = start
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def sla_due(policy: ApprovalPolicy, level: str, start: dt.date, request_due: str | None) -> dt.date:
    """06 §4.3：SLA 工作日，且不晚于 Request due_date 前 2 天。"""
    due = add_workdays(start, policy.sla_workdays(level))
    rd = parse_date(request_due)
    if rd is not None:
        due = min(due, rd - dt.timedelta(days=2))
    return due


@dataclass
class Routed:
    answer_ref: str
    approval_ref: str
    risk_level: str
    rule_id: str
    required_role: list[str]
    sla_due: str
    floors: list[str] = field(default_factory=list)


@dataclass
class RouteResult:
    routed: list[Routed] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    stale_rejected: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"routed": [asdict(r) for r in self.routed], "blocked": self.blocked, "stale_rejected": self.stale_rejected,
                "skipped": self.skipped}


class ApprovalRouter:
    def __init__(self, store: Any, policy: ApprovalPolicy | None = None, *, meta: MetaProvider | None = None,
                 amount_provider: Callable[[AuditContext, dict[str, Any]], Decimal | None] | None = None,
                 policy_available: bool = True) -> None:
        self.store = store
        self.policy = policy or default_policy()
        self.meta = meta or MetaProvider.default()
        self.amount_provider = amount_provider
        self.policy_available = policy_available

    # ------------------------------------------------------------------ helpers
    def ctx(self, org_id: str) -> AuditContext:
        return AuditContext.from_store(self.store, org_id, meta=self.meta)

    def router_actor(self, org_id: str) -> Actor:
        return Actor.of(ROUTER_ID, org_id)

    def assess(self, ctx: AuditContext, answer: dict[str, Any]) -> RiskDecision:
        dec, _ = assess(ctx, answer, self.policy if self.policy_available else None, self.amount_provider)
        return dec

    def required_role(self, ctx: AuditContext, answer: dict[str, Any], level: str) -> list[str]:
        from ..legal.flags import question_legal_flag

        legal = bool(answer.get("legal_commitment")) or any(question_legal_flag(q) for q in ctx.questions_of(answer))
        tier = self.policy.customer_tier(sorted(ctx.customers_of(answer)))
        return self.policy.roles_for(level, legal_commitment=legal, customer_tier=tier)

    # ------------------------------------------------------------------ 路由
    def route(self, org_id: str, *, request_id: str | None = None, answer_refs: Iterable[str] = (),
              assignee: str | None = None) -> RouteResult:
        res = RouteResult()
        res.stale_rejected = self.restale(org_id)
        ctx = self.ctx(org_id)
        ids = {split_ref(r)[0] for r in answer_refs}
        answers = [ctx.head(i) for i in ids] if ids else (ctx.request_answers(request_id) if request_id else [])
        today = ctx.today
        for a in [x for x in answers if x]:
            if a.get("status") != "draft":
                res.skipped.append({"answer_ref": ref_of(a), "status": a.get("status")})
                continue
            dec = self.assess(ctx, a)
            if dec.blocked or dec.level is None:
                res.blocked.append({"answer_ref": ref_of(a), "rule_id": dec.rule_id,
                                    "conflicts": [ref_of(c) for c in ctx.conflicts_for(a["id"], "open")
                                                  if c.get("severity") == "blocking"]})
                continue
            level = dec.level
            roles = self.required_role(ctx, a, level)
            req = ctx.request_of(a)
            patch: dict[str, Any] = {"id": a["id"], "status": "in_review", "risk_level": level}
            if not a.get("legal_commitment") and any(question_legal_flag(q) for q in ctx.questions_of(a)):
                patch["legal_commitment"] = True  # 06 §6.3 强制
            apr_id = new_id("Approval")
            apr = {"id": apr_id, "subject_ref": f"{a['id']}#v{a['version'] + 1}", "risk_level": level,
                   "required_role": roles, **self.policy.approval_fields(), "status": "pending",
                   "trust_zone": "trusted"}
            if req:
                apr["request_id"] = req["id"]
            if assignee:
                apr["assignee"] = assignee
            refs = self.store.write_batch([
                WriteItem("Answer", patch, expected_version=a["version"], patch_mode="merge_from_head"),
                WriteItem("Approval", apr),
            ], actor=self.router_actor(org_id))
            res.routed.append(Routed(refs[0], refs[1], level, dec.rule_id, roles,
                                     sla_due(self.policy, level, today, (req or {}).get("due_date")).isoformat(),
                                     dec.floors))
        return res

    def restale(self, org_id: str) -> list[str]:
        """03 §3.8：pending 审批钉住的 Answer 出现新版本 → 旧审批追加 rejected + comment="stale"。"""
        out = []
        ctx = self.ctx(org_id)
        for apr in ctx.heads("Approval", status="pending"):
            s = apr.get("subject_ref", "")
            if not s.startswith("ans_"):
                continue
            if ctx.is_head(s):
                continue
            ref = self.store.write("Approval", {"id": apr["id"], "status": "rejected", "decision": "rejected",
                                                "comment": "stale", "decided_by": ROUTER_ID,
                                                "decided_at": self.store.peek_now()}, expected_version=apr["version"],
                                   actor=self.router_actor(org_id), patch_mode="merge_from_head")
            out.append(ref)
        return out

    def escalations(self, org_id: str, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """SLA 超时：先提醒 assignee，超时 1 个工作日升级；永不自动批准（06 §4.3）。"""
        ctx = self.ctx(org_id)
        today = (now.date() if now else ctx.today)
        out = []
        for apr in ctx.heads("Approval", status="pending"):
            created = parse_date(apr.get("created_at")) or today
            req = ctx.head(apr["request_id"]) if apr.get("request_id") else None
            due = sla_due(self.policy, apr["risk_level"], created, (req or {}).get("due_date"))
            if today <= due:
                continue
            esc = self.policy.escalation
            hard = add_workdays(due, int(esc.get("after_sla_workdays", 1)))
            action = "escalate" if today > hard else "remind"
            out.append({"approval_ref": ref_of(apr), "action": action, "sla_due": due.isoformat(),
                        "to": list(esc.get("to") or []) if action == "escalate" else ([apr["assignee"]] if apr.get("assignee") else apr["required_role"]),
                        "auto_approve": False})
        return out

    # ------------------------------------------------------------------ 单条裁决
    def decide(self, approval_id: str, *, actor: Actor, decision: str, comment: str | None = None,
               confirmations: dict[str, Any] | None = None, modification: dict[str, Any] | None = None) -> dict[str, Any]:
        if decision not in ("approved", "rejected", "modified"):
            raise GraphError("GRAPH_SCHEMA_INVALID", "decision must be approved|rejected|modified", path="/decision")
        if not actor.is_user:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "approvals are decided by users")
        ctx = self.ctx(actor.org_id)
        apr = ctx.head(approval_id)
        if apr is None or not approval_id.startswith("apr_"):
            raise GraphError("GRAPH_NOT_FOUND", f"{approval_id} not found")
        if apr.get("status") != "pending":
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"approval is {apr.get('status')}", path="/status")
        subj = apr["subject_ref"]
        if not subj.startswith("ans_"):
            raise GraphError("GRAPH_SCHEMA_INVALID", "use the supplier_request_first_send / conflict resolve endpoints for "
                             "non-answer subjects", path="/approval_id")
        self.check_roles(apr, actor)
        if apr.get("batch_id") and apr["risk_level"] == "L3":
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "L3 cannot be batch approved", path="/batch_id")
        ans = ctx.get(subj)
        if ans is None:
            raise GraphError("GRAPH_NOT_FOUND", f"{subj} not found")
        if not ctx.is_head(subj):
            self.restale(actor.org_id)
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "answer changed after routing; approval is stale and was re-queued",
                             details={"approved_version": subj, "head": ref_of(ctx.head(ans["id"]))})  # type: ignore[arg-type]
        conf = dict(confirmations or {})
        now = self.store.peek_now()
        if decision == "approved":
            self.check_decision_preconditions(ctx, apr, ans, conf)
            items = [
                WriteItem("Approval", {"id": apr["id"], "status": "approved", "decision": "approved", "decided_by": actor.id,
                                       "decided_at": now, **({"comment": comment} if comment else {})},
                          expected_version=apr["version"], patch_mode="merge_from_head", actor=actor),
                WriteItem("Answer", {"id": ans["id"], "status": "approved", "approval_ref": f"{apr['id']}#v{apr['version'] + 1}"},
                          expected_version=ans["version"], patch_mode="merge_from_head", actor=self.router_actor(actor.org_id)),
            ]
        elif decision == "rejected":
            items = [
                WriteItem("Approval", {"id": apr["id"], "status": "rejected", "decision": "rejected", "decided_by": actor.id,
                                       "decided_at": now, **({"comment": comment} if comment else {})},
                          expected_version=apr["version"], patch_mode="merge_from_head", actor=actor),
                WriteItem("Answer", {"id": ans["id"], "status": "rejected"}, expected_version=ans["version"],
                          patch_mode="merge_from_head", actor=self.router_actor(actor.org_id)),
            ]
        else:
            items = self._modification_items(ctx, apr, ans, actor, modification or {}, comment, now)
        refs = self.store.write_batch(items, actor=actor)
        return {"approval_ref": next(r for r in refs if r.startswith("apr_")), "refs": refs, "decision": decision}

    def check_roles(self, apr: dict[str, Any], actor: Actor) -> None:
        need = set(apr.get("required_role") or [])
        if not (actor.roles & need):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", f"{actor.id} lacks a required approver role",
                             details={"required_role": sorted(need)})
        if actor.has_role("contributor", "sales") and not actor.has_role("compliance_owner", "executive"):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "contributor/sales may only prepare, not approve (06 §4.3)",
                             details={"required_role": sorted(need)})

    def check_decision_preconditions(self, ctx: AuditContext, apr: dict[str, Any], ans: dict[str, Any],
                                     conf: dict[str, Any]) -> None:
        open_c = ctx.conflicts_for(ans["id"], "open")
        if any(c.get("severity") == "blocking" for c in open_c):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "open blocking conflict: resolve it first (RISK-00)", rule_id="RISK-00",
                             details={"conflicts": [ref_of(c) for c in open_c if c.get("severity") == "blocking"]})
        lvl = apr["risk_level"]
        if lvl in ("L2", "L3") and ans.get("is_estimate") and not conf.get("estimate_ack"):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "estimate answers require estimate_ack (06 §4.3)",
                             path="/confirmations/estimate_ack")
        if lvl in ("L2", "L3") and any(c.get("severity") == "warning" for c in open_c) and not (conf.get("warn_note") or "").strip():
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "open warning conflict requires a warn_note (06 §2.1/§4.3)",
                             path="/confirmations/warn_note")
        if lvl == "L3":
            missing = [k for k in self.policy.confirmations("L3") if not conf.get(k)]
            if missing:
                raise GraphError("GRAPH_APPROVAL_REQUIRED", f"L3 requires single confirmation with {missing} (06 §4.3)",
                                 path=f"/confirmations/{missing[0]}", details={"missing": missing})

    def _modification_items(self, ctx: AuditContext, apr: dict[str, Any], ans: dict[str, Any], actor: Actor,
                            mod: dict[str, Any], comment: str | None, now: str) -> list[WriteItem]:
        src = mod.get("source")
        if src not in ("existing_evidence", "upload", "human_confirmation"):
            raise GraphError("GRAPH_NUM_UNREFERENCED", "modification requires a source: existing_evidence | upload | "
                             "human_confirmation (R-NUM, 01 §3.1)", path="/modification/source")
        items: list[WriteItem] = []
        patch: dict[str, Any] = {"id": ans["id"], "status": "draft"}
        apr_ref = ref_of(apr)
        if src == "existing_evidence":
            ev = ctx.get(mod.get("evidence_ref") or "")
            if ev is None:
                raise GraphError("GRAPH_REF_NOT_FOUND", "modification.evidence_ref not found", path="/modification/evidence_ref")
            ev_ref = ref_of(ev)
            if ans.get("value_type") == "number":
                n = ctx.numeric(ev)
                v = ctx.convert(n[0], n[1], ans.get("unit")) if n else None
                if v is None:
                    raise GraphError("GRAPH_UNIT_INCOMPATIBLE", "evidence value not convertible to the answer unit")
                patch["value"] = format(v.normalize(), "f")
            else:
                patch["value"] = ev.get("extracted_value")
            patch["evidence_refs"] = [ev_ref]
        else:
            if mod.get("value") in (None, ""):
                raise GraphError("GRAPH_SCHEMA_INVALID", "modification.value required", path="/modification/value")
            ev_id = new_id("Evidence")
            loc: dict[str, Any] = {"approval_ref": apr_ref}
            if src == "upload":
                if not mod.get("blob_sha256"):
                    raise GraphError("GRAPH_LOCATOR_MISSING", "upload requires blob_sha256", path="/modification/blob_sha256")
                loc["blob_sha256"] = mod["blob_sha256"]
            ev = {"id": ev_id, "source_type": "human_confirmation", "datapoint_id": ans["datapoint_id"],
                  "fact_key": f"answer.{ans['datapoint_id'].split('@')[0][3:]}", "extracted_value": mod["value"],
                  "value_type": ans["value_type"], "period": ans["period"], "boundary": ans["boundary"], "locator": loc,
                  "valid_from": ans["period"]["start"], "confidence": 1, "status": "verified", "trust_zone": "trusted",
                  "verification": {"method": "human_review", "by": actor.id}}
            if ans.get("unit"):
                ev["unit"] = ans["unit"]
            items.append(WriteItem("Evidence", ev, actor=actor))
            patch.update(value=mod["value"], evidence_refs=[f"{ev_id}#v1"])
        if ans.get("value_type") == "number":
            patch["calculation_ref"] = None
        new_ref = f"{ans['id']}#v{ans['version'] + 1}"
        items.append(WriteItem("Answer", patch, expected_version=ans["version"], patch_mode="merge_from_head", actor=actor))
        items.append(WriteItem("Approval", {"id": apr["id"], "status": "modified", "decision": "modified",
                                            "decided_by": actor.id, "decided_at": now, "modified_answer_ref": new_ref,
                                            **({"comment": comment or mod.get("note")} if (comment or mod.get("note")) else {})},
                               expected_version=apr["version"], patch_mode="merge_from_head", actor=actor))
        return items

    # ------------------------------------------------------------------ 非 Answer：SupplierRequest 首次外发
    def supplier_first_send_level(self, ctx: AuditContext, sreq: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        cfg = self.policy.non_answer("supplier_request_first_send")
        reasons = []
        if any((ctx.dp(d) and ctx.dp(d).sensitivity == "restricted") for d in sreq.get("datapoint_ids") or []):  # type: ignore[union-attr]
            reasons.append("payload_has_sensitivity_restricted")
        cp = ctx.head(sreq["counterparty_id"]) if sreq.get("counterparty_id") else None
        if (tag_map(cp).get("supplier_tier") or "").upper() == "A":
            reasons.append("supplier_tier == 'A'")
        if "custom_note" in tag_map(sreq):
            reasons.append("custom_note_present")
        if reasons:
            return "L2", list(cfg.get("L2_roles") or ["compliance_owner"]), reasons
        return cfg.get("default_level", "L1"), list(cfg.get("roles") or ["compliance_owner"]), []

    def route_supplier_request(self, org_id: str, sreq_id: str) -> str:
        ctx = self.ctx(org_id)
        s = ctx.head(sreq_id)
        if s is None or not sreq_id.startswith("sreq_"):
            raise GraphError("GRAPH_NOT_FOUND", f"{sreq_id} not found")
        existing = self._pending_for(ctx, s["id"])
        if existing:
            return ref_of(existing)
        level, roles, _ = self.supplier_first_send_level(ctx, s)
        return self.store.write("Approval", {"id": new_id("Approval"), "subject_ref": ref_of(s), "risk_level": level,
                                             "required_role": roles, **self.policy.approval_fields(),
                                             "status": "pending", "trust_zone": "trusted"}, actor=self.router_actor(org_id))

    def _pending_for(self, ctx: AuditContext, subject_id: str) -> dict[str, Any] | None:
        for a in ctx.heads("Approval", status="pending"):
            if split_ref(a.get("subject_ref", ""))[0] == subject_id:
                return a
        return None

    def decide_supplier_first_send(self, sreq_id: str, *, actor: Actor, decision: str = "approved",
                                   comment: str | None = None) -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise GraphError("GRAPH_SCHEMA_INVALID", "decision must be approved|rejected", path="/decision")
        ctx = self.ctx(actor.org_id)
        s = ctx.head(sreq_id)
        if s is None or not sreq_id.startswith("sreq_"):
            raise GraphError("GRAPH_NOT_FOUND", f"{sreq_id} not found")
        if s.get("status") != "draft" or any(v.get("status") == "sent" for v in ctx.versions(s["id"])):
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "first-send approval applies to a draft SupplierRequest that was "
                             "never sent (reminders of the same template are out of scope, 08 §2.2)", path="/status")
        level, roles, reasons = self.supplier_first_send_level(ctx, s)
        if not (actor.roles & set(roles)):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", f"{actor.id} lacks a required role for first send ({level})",
                             details={"required_role": roles, "escalation": reasons})
        if not actor.has_role("compliance_owner"):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "core RBAC/R-APR currently allow only compliance_owner to decide "
                             "Approvals (purchasing-lead contributor not yet supported)", details={"required_role": roles})
        items: list[WriteItem] = []
        pending = self._pending_for(ctx, s["id"])
        if pending and pending.get("subject_ref") != ref_of(s):
            items.append(WriteItem("Approval", {"id": pending["id"], "status": "rejected", "decision": "rejected",
                                                "comment": "stale", "decided_by": ROUTER_ID,
                                                "decided_at": self.store.peek_now()}, expected_version=pending["version"],
                                   patch_mode="merge_from_head", actor=self.router_actor(actor.org_id)))
            pending = None
        if pending is None:
            apr_id = new_id("Approval")
            items.append(WriteItem("Approval", {"id": apr_id, "subject_ref": ref_of(s), "risk_level": level,
                                                "required_role": roles, **self.policy.approval_fields(),
                                                "status": "pending", "trust_zone": "trusted"},
                                   actor=self.router_actor(actor.org_id)))
            ver = 1
        else:
            apr_id, ver = pending["id"], pending["version"]
            if pending["risk_level"] != level:  # 策略重新定级（级别变化 → 作废重建）
                items.append(WriteItem("Approval", {"id": apr_id, "status": "rejected", "decision": "rejected",
                                                    "comment": "stale", "decided_by": ROUTER_ID,
                                                    "decided_at": self.store.peek_now()}, expected_version=ver, patch_mode="merge_from_head",
                                       actor=self.router_actor(actor.org_id)))
                apr_id, ver = new_id("Approval"), 1
                items.append(WriteItem("Approval", {"id": apr_id, "subject_ref": ref_of(s), "risk_level": level,
                                                    "required_role": roles, **self.policy.approval_fields(),
                                                    "status": "pending", "trust_zone": "trusted"},
                                       actor=self.router_actor(actor.org_id)))
        items.append(WriteItem("Approval", {"id": apr_id, "status": decision, "decision": decision, "decided_by": actor.id,
                                            "decided_at": self.store.peek_now(), **({"comment": comment} if comment else {})},
                               expected_version=ver, patch_mode="merge_from_head", actor=actor))
        refs = self.store.write_batch(items, actor=actor)
        return {"approval_ref": refs[-1], "risk_level": level, "required_role": roles, "escalation": reasons,
                "decision": decision}


__all__ = ["ApprovalRouter", "RouteResult", "Routed", "sla_due", "add_workdays", "ROUTER_ID"]

# Spec: 03 §2.3（R-ZONE、R-PROMO、R-REF、R-STATE）、§1.6、07 §2.3（PC-08）、03 §6.8（GRAPH_HALTED）
"""通用写入规则：R-ZONE / R-PROMO / R-REF / R-STATE / 守卫中止。"""
from __future__ import annotations

from typing import Any

from ..canonical import hash_obj
from ..errors import GraphError
from ..ids import ORG_GLOBAL, type_of_id
from ..models.common import SCHEMA_VERSION
from ..statemachines import check_transition
from .context import RuleContext
from .refspec import iter_path, ref_fields, target_id

# 晋升/守卫比较时忽略的信封字段（07 §2.3 PC-08：除 trust_zone/status/promotion 外字节级相同）
_ENVELOPE_VOLATILE = {"version", "created_at", "created_by", "provenance", "supersedes", "content_hash"}
_PROMO_FREE = {"trust_zone", "status", "promotion"}
_GUARD_FREE = _PROMO_FREE | {"status_reason"}


def _strip(body: dict[str, Any], extra: set[str]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _ENVELOPE_VOLATILE | extra}


# ------------------------------------------------------------------ R-ZONE
def rule_zone(ctx: RuleContext) -> None:
    a, new = ctx.actor, ctx.new
    if a.zone == "quarantine" and new.get("trust_zone") != "quarantine":
        raise GraphError("GRAPH_TRUST_ZONE_VIOLATION", f"quarantine actor {a.id} may only write quarantine objects",
                         path="/trust_zone")
    if ctx.prev and ctx.prev.get("trust_zone") == "trusted" and new.get("trust_zone") == "quarantine":
        raise GraphError("GRAPH_PROMOTION_DENIED", "trust_zone may only move quarantine -> trusted", path="/trust_zone")
    # 数值输入只引用 trusted
    t = ctx.type_name
    if t == "Calculation":
        for i, inp in enumerate(new.get("inputs") or []):
            tgt = ctx.resolve(inp["ref"])
            if tgt is None:
                continue  # R-REF 报告
            ok_status = (tgt.get("status") == "verified") if inp["ref"].startswith("ev_") else (tgt.get("status") == "computed")
            if tgt.get("trust_zone") != "trusted" or not ok_status:
                raise GraphError("GRAPH_TRUST_ZONE_VIOLATION",
                                 f"calculation input {inp['ref']} must be trusted and verified/computed "
                                 f"(is {tgt.get('trust_zone')}/{tgt.get('status')})", path=f"/inputs/{i}/ref")
    if t == "Answer":
        refs = [(f"/evidence_refs/{i}", r) for i, r in enumerate(new.get("evidence_refs") or [])]
        for k in ("calculation_ref", "reused_from"):
            if new.get(k):
                refs.append((f"/{k}", new[k]))
        for p, r in refs:
            tgt = ctx.resolve(r)
            if tgt is not None and tgt.get("trust_zone") != "trusted":
                raise GraphError("GRAPH_TRUST_ZONE_VIOLATION", f"answer may only cite trusted objects: {r}", path=p)


# ------------------------------------------------------------------ R-PROMO
def is_promotion(ctx: RuleContext) -> bool:
    return bool(ctx.prev) and ctx.prev.get("trust_zone") == "quarantine" and ctx.new.get("trust_zone") == "trusted"


def rule_promo(ctx: RuleContext) -> None:
    a, new, prev = ctx.actor, ctx.new, ctx.prev
    promo = new.get("promotion")
    if is_promotion(ctx):
        via_cq = a.id == "service:graph-api" and (promo or {}).get("rule") == "human"  # ClosedQuestion 作答派生
        if not (a.id == "agent:guard" or (a.is_user and a.zone == "trusted") or via_cq):
            raise GraphError("GRAPH_PROMOTION_DENIED", f"only agent:guard or a human may promote ({a.id})")
        if not promo:
            raise GraphError("GRAPH_PROMOTION_DENIED", "promotion version requires promotion {rule, schema_version, checked_at}",
                             path="/promotion")
        if a.is_user and promo.get("rule") != "human":
            raise GraphError("GRAPH_PROMOTION_DENIED", "human promotion must use promotion.rule='human'",
                             path="/promotion/rule")
        if promo.get("schema_version") != SCHEMA_VERSION:
            raise GraphError("GRAPH_PROMOTION_DENIED", "promotion.schema_version must equal the graph schema version",
                             path="/promotion/schema_version")
        if hash_obj(_strip(new, _PROMO_FREE)) != hash_obj(_strip(prev, _PROMO_FREE)):
            diff = sorted(k for k in set(_strip(new, _PROMO_FREE)) | set(_strip(prev, _PROMO_FREE))
                          if hash_obj(new.get(k)) != hash_obj(prev.get(k)))
            raise GraphError("GRAPH_PROMOTION_DENIED", "promotion must not change values (PC-08)",
                             details={"changed_fields": diff})
        if ctx.type_name == "Mapping" and a.id == "agent:guard" and new.get("status") == "confirmed":
            if not (new.get("rank") == 1 and float(new.get("confidence", 0)) >= 0.85):
                raise GraphError("GRAPH_PROMOTION_DENIED", "PROMO-MAP-0.85: rank=1 and confidence>=0.85 required",
                                 path="/confidence")
    elif promo and not (a.id in ("agent:guard", "service:graph-api") or a.is_user) and not (prev and prev.get("promotion") == promo):
        raise GraphError("GRAPH_PROMOTION_DENIED", "only agent:guard or a human may set promotion", path="/promotion")
    # guard 只盖章不改值（03 §1.6 guard 禁止"改值"）
    if a.id == "agent:guard":
        if prev is None:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "agent:guard only stamps existing objects", rule_id="R-PROMO")
        if hash_obj(_strip(new, _GUARD_FREE)) != hash_obj(_strip(prev, _GUARD_FREE)):
            raise GraphError("GRAPH_PROMOTION_DENIED", "agent:guard may only change status/status_reason/trust_zone/promotion")


# ------------------------------------------------------------------ R-REF
def _ref_set(body: dict[str, Any] | None, path: str) -> set[str]:
    if not body:
        return set()
    return {v for _, v, _ in iter_path(body, path) if isinstance(v, str)}


_NEW_REF_EXPIRED_CHECK = {("Answer", "evidence_refs[]"), ("Calculation", "inputs[].ref")}


def rule_ref(ctx: RuleContext) -> None:
    new = ctx.new
    for f in ref_fields(ctx.type_name):
        prev_vals = _ref_set(ctx.prev, f.path)
        for ptr, val, _ in iter_path(new, f.path):
            tid, tver = target_id(f, val)
            if tid is None:
                continue
            is_new_ref = val not in prev_vals
            if f.kind == "dp":
                info = ctx.datapoints.get(val)
                if info is None:
                    raise GraphError("GRAPH_REF_NOT_FOUND", f"DataPoint {val} not found in dictionary", path=ptr)
                if info.status in ("deprecated", "retired") and ctx.type_name == "Mapping" and is_new_ref:
                    raise GraphError("GRAPH_DATAPOINT_DEPRECATED", f"DataPoint {val} is {info.status}; new mappings not allowed",
                                     path=ptr)
                if ctx.type_name == "Mapping" and info.status != "active" and is_new_ref:
                    raise GraphError("GRAPH_DATAPOINT_DEPRECATED", f"Mapping target must be active ({info.status})", path=ptr)
                continue
            ttype = type_of_id(tid)
            if ttype not in f.targets:
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{val} is not a {'/'.join(f.targets)}", path=ptr)
            if tid == ctx.new.get("id") and f.path != "supersedes":
                raise GraphError("GRAPH_REF_NOT_FOUND", "object may not reference itself", path=ptr)
            orgs = (ctx.org_id, ORG_GLOBAL)
            head = None
            for org in orgs:
                head = ctx.reader.get_head_body(org, tid)
                if head is not None:
                    break
            if head is None:
                if ctx.reader.orgs_of(tid):
                    raise GraphError("GRAPH_REF_CROSS_TENANT", f"{val} belongs to another tenant", path=ptr)
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{val} not found", path=ptr)
            if head.get("org_id") == ORG_GLOBAL and ttype not in ("FactorSet", "Organization"):
                raise GraphError("GRAPH_REF_CROSS_TENANT", f"{val}: only FactorSet/DataPoint may be global", path=ptr)
            if tver is not None:
                tgt = ctx.reader.get_version(head["org_id"], tid, tver)
                if tgt is None:
                    raise GraphError("GRAPH_REF_NOT_FOUND", f"{val}: version not found", path=ptr)
            if f.path == "supersedes":
                continue
            if is_new_ref and head.get("status") == "rejected":
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{val} is rejected", path=ptr)
            if is_new_ref and (ctx.type_name, f.path) in _NEW_REF_EXPIRED_CHECK and head.get("status") == "expired":
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{val} is expired and may not be newly cited (03 §3.5)", path=ptr)
            # 角色约束
            if f.path == "counterparty_id" and ttype == "Counterparty":
                want = "customer" if ctx.type_name == "Request" else "supplier"
                if head.get("role") != want:
                    raise GraphError("GRAPH_REF_NOT_FOUND", f"{tid} must be a Counterparty with role={want}", path=ptr)
            if f.kind == "fs" and head.get("status") == "deprecated" and is_new_ref:
                raise GraphError("GRAPH_REF_NOT_FOUND", f"FactorSet {tid} is deprecated (03 §3.13)", path=ptr)


# ------------------------------------------------------------------ R-STATE
def rule_state(ctx: RuleContext) -> None:
    if ctx.prev is not None and ctx.prev.get("type_name_mismatch"):
        raise GraphError("GRAPH_SCHEMA_INVALID", "type mismatch")  # pragma: no cover
    check_transition(ctx.type_name, ctx.prev, ctx.new, ctx.actor)


# ------------------------------------------------------------------ 守卫中止（GRAPH_HALTED）
def rule_halted(ctx: RuleContext) -> None:
    a = ctx.actor
    if not a.is_agent or a.id == "agent:guard":
        return
    req_id = ctx.new.get("id") if ctx.type_name == "Request" else ctx.new.get("request_id")
    if not isinstance(req_id, str) or not req_id.startswith("req_"):
        return
    head = ctx.reader.get_head_body(ctx.org_id, req_id)
    if head is not None and head.get("status") == "halted":
        raise GraphError("GRAPH_HALTED", f"request {req_id} is halted", details={"request_id": req_id})

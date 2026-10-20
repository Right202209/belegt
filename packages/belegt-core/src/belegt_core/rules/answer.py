# Spec: 03 §3.7 Answer、§2.3（R-NUM/R-VAL/R-UNIT/R-PERIOD/R-ONE/R-APR/R-SUBMIT）、§4.3（GRAPH_DATAPOINT_DEPRECATED）、
#       07 §4.1（职责分离：Approval.decided_by ≠ Answer 最后修改者）
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..authz import APPROVER_ROLES
from ..canonical import to_decimal
from ..errors import GraphError
from .context import RuleContext
from .numtext import allowed_numbers, unsourced_numbers


def _covers(outer: dict | None, inner: dict) -> bool:
    return bool(outer) and outer["start"] <= inner["start"] and outer["end"] >= inner["end"]


def rule_answer_datapoint(ctx: RuleContext) -> None:
    a = ctx.new
    dp = ctx.datapoints.get(a["datapoint_id"])
    if dp is None:
        return  # R-REF 已报
    if a["value_type"] != dp.value_type:
        raise GraphError("GRAPH_SCHEMA_INVALID", f"value_type {a['value_type']} != DataPoint value_type {dp.value_type}",
                         path="/value_type")
    if not dp.boundary_allowed(a["boundary"]):
        raise GraphError("GRAPH_SCHEMA_INVALID", f"boundary {a['boundary']} not in allowed_boundaries {dp.allowed_boundaries}",
                         path="/boundary")
    if a["boundary"] == "organization" and dp.requires_consolidation and not a.get("consolidation"):
        raise GraphError("GRAPH_SCHEMA_INVALID", "consolidation required for organization boundary", path="/consolidation")
    if a.get("consolidation") and dp.consolidation and a["consolidation"] not in dp.consolidation:
        raise GraphError("GRAPH_SCHEMA_INVALID", f"consolidation {a['consolidation']} not allowed", path="/consolidation")
    if ctx.entering("approved") and dp.status in ("deprecated", "retired"):
        raise GraphError("GRAPH_DATAPOINT_DEPRECATED", f"{a['datapoint_id']} is {dp.status}; cannot approve (03 §4.3)",
                         path="/datapoint_id")


def rule_answer_unit(ctx: RuleContext) -> None:
    """R-UNIT：单位与 DataPoint 声明的量纲兼容。"""
    a = ctx.new
    if a.get("value_type") != "number":
        return
    dp = ctx.datapoints.get(a["datapoint_id"])
    unit = a.get("unit")
    if dp is None or not dp.unit:
        return
    if not ctx.units.compatible(unit, dp.unit):
        raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"unit {unit} incompatible with DataPoint unit {dp.unit}", path="/unit")


def _numeric_value(obj: dict[str, Any]) -> tuple[Decimal, str] | None:
    if obj.get("id", "").startswith("calc_"):
        r = obj.get("result") or {}
        return to_decimal(r["value"]), r["unit"]
    if obj.get("value_type") == "number" and obj.get("extracted_value") is not None:
        return to_decimal(obj["extracted_value"]), obj.get("unit")
    return None


def rule_answer_val(ctx: RuleContext) -> None:
    """R-VAL：数字值等于所引 Calculation.result.value，或所引唯一数值 Evidence 的 extracted_value（确定性单位换算后逐位相等）。"""
    a = ctx.new
    if a.get("value_type") != "number":
        return
    value = to_decimal(a["value"])
    unit = a.get("unit")
    if a.get("calculation_ref"):
        calc = ctx.resolve(a["calculation_ref"])
        if calc is None:
            return
        src, path = _numeric_value(calc), "/calculation_ref"
    else:
        nums = []
        for r in a.get("evidence_refs") or []:
            ev = ctx.resolve(r)
            if ev is not None and _numeric_value(ev) is not None:
                nums.append(_numeric_value(ev))
        if len(nums) != 1:
            raise GraphError("GRAPH_NUM_VALUE_MISMATCH",
                             f"without calculation_ref the answer must cite exactly one numeric evidence (found {len(nums)})",
                             path="/evidence_refs")
        src, path = nums[0], "/evidence_refs"
    sval, sunit = src
    try:
        conv = ctx.units.convert(sval, sunit, unit) if sunit != unit else sval
    except Exception:
        raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"cannot convert source unit {sunit} to answer unit {unit}", path="/unit")
    if conv.normalize() != value.normalize():
        raise GraphError("GRAPH_NUM_VALUE_MISMATCH",
                         f"answer value {a['value']} {unit} != referenced value {conv.normalize()} {unit}", path="/value",
                         details={"expected": format(conv.normalize(), "f"), "got": a["value"], "source": path})


def rule_answer_period(ctx: RuleContext) -> None:
    """R-PERIOD：所引 Evidence 的有效期或期间覆盖 ``Answer.period``。"""
    a = ctx.new
    p = a["period"]
    for i, r in enumerate(a.get("evidence_refs") or []):
        ev = ctx.resolve(r)
        if ev is None:
            continue
        validity = {"start": ev.get("valid_from"), "end": ev.get("valid_until") or "9999-12-31"}
        if not (_covers(ev.get("period"), p) or (validity["start"] and _covers(validity, p))):
            raise GraphError("GRAPH_PERIOD_UNCOVERED", f"evidence {r} does not cover answer period {p['start']}..{p['end']}",
                             path=f"/evidence_refs/{i}")


def rule_answer_num_in_text(ctx: RuleContext) -> None:
    a = ctx.new
    note = a.get("drafting_note")
    if not note:
        return
    objs = [a]
    for r in (a.get("evidence_refs") or []) + ([a["calculation_ref"]] if a.get("calculation_ref") else []):
        o = ctx.resolve(r)
        if o:
            objs.append(o)
    bad = unsourced_numbers(note.values(), allowed_numbers(objs))
    if bad:
        raise GraphError("GRAPH_NUM_IN_TEXT", f"drafting_note contains unsourced numbers: {bad}", path="/drafting_note",
                         details={"tokens": bad})


def _supersede_targets(ctx: RuleContext) -> set[str]:
    """本 Answer 任一版本以 ``supersedes`` 指向的其它 Answer 逻辑 ID（跨对象替换，03 §1.4-2）。"""
    out = set()
    for body in ctx.reader.versions_of(ctx.org_id, ctx.new["id"]) + [ctx.new]:
        s = body.get("supersedes")
        if s and not s.startswith(ctx.new["id"] + "#"):
            out.add(s.split("#", 1)[0])
    return out


def rule_answer_one(ctx: RuleContext) -> None:
    """R-ONE（SSOT-5）：同一 answer_key 任一时刻最多一个 approved/submitted 的有效 head。"""
    a = ctx.new
    if a["status"] not in ("approved", "submitted"):
        return
    eff = ctx.reader.effective_answer(ctx.org_id, a["answer_key"])
    if eff is None or eff[0] == a["id"]:
        return
    other = ctx.reader.get_head_body(ctx.org_id, eff[0])
    if other is None or other.get("status") not in ("approved", "submitted"):
        return
    if eff[0] in _supersede_targets(ctx):
        return
    raise GraphError("GRAPH_DUPLICATE_EFFECTIVE_ANSWER",
                     f"answer_key already has effective answer {eff[0]}#v{eff[1]}; supersede it or use a distinct customer_scope",
                     path="/answer_key", details={"effective": f"{eff[0]}#v{eff[1]}"})


def rule_answer_apr(ctx: RuleContext) -> None:
    """R-APR：进入 approved 必须有 decision=approved 的 Approval，审批人角色满足 risk_level（L3 不得批量）。"""
    a = ctx.new
    if not ctx.entering("approved"):
        return
    ref = a.get("approval_ref")
    apr = ctx.resolve(ref) if ref else None
    if apr is None:
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "approved requires approval_ref to an existing Approval", path="/approval_ref")
    if apr.get("decision") != "approved" or apr.get("status") != "approved":
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "referenced Approval is not decision=approved", path="/approval_ref")
    subj = apr.get("subject_ref", "")
    if subj.split("#", 1)[0] != a["id"]:
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "Approval.subject_ref does not point to this answer", path="/approval_ref")
    sv = int(subj.split("#v", 1)[1])
    subj_body = ctx.reader.get_version(ctx.org_id, a["id"], sv)
    if subj_body is None or subj_body.get("status") != "in_review":
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "Approval must pin an in_review answer version", path="/approval_ref")
    if ctx.prev and ctx.prev.get("version") != sv:
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "answer changed after the approved version (stale approval)",
                         path="/approval_ref", details={"approved_version": sv, "head": ctx.prev.get("version")})
    risk = a.get("risk_level")
    if apr.get("risk_level") != risk:
        raise GraphError("GRAPH_APPROVAL_REQUIRED", f"Approval risk_level {apr.get('risk_level')} != answer risk_level {risk}",
                         path="/risk_level")
    if risk == "L3" and apr.get("batch_id"):
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "L3 answers cannot be batch-approved", path="/approval_ref")
    decider = apr.get("decided_by") or ""
    roles = ctx.reader.roles_of(ctx.org_id, decider) if hasattr(ctx.reader, "roles_of") else None
    if roles is not None:
        allowed = APPROVER_ROLES.get(risk, frozenset()) & set(apr.get("required_role") or [])
        if not (set(roles) & allowed):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", f"{decider} lacks an approver role for {risk}",
                             path="/approval_ref", details={"decider_roles": sorted(roles), "allowed": sorted(allowed)})
    if risk == "L3":
        modifiers = {v.get("created_by") for v in ctx.reader.versions_of(ctx.org_id, a["id"]) if
                     str(v.get("created_by", "")).startswith("user:")}
        if decider in modifiers:
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "segregation of duties: L3 approver must not have modified the answer "
                             "(07 §4.1)", path="/approval_ref")
    if ctx.actor.is_user and ctx.actor.id != decider:
        raise GraphError("GRAPH_APPROVAL_REQUIRED", "a human writing approved must be the Approval decider", path="/approval_ref")


def _cited_evidence(ctx: RuleContext, a: dict[str, Any]) -> list[str]:
    refs = list(a.get("evidence_refs") or [])
    seen: set[str] = set()
    stack = [a["calculation_ref"]] if a.get("calculation_ref") else []
    while stack:
        r = stack.pop()
        if r in seen:
            continue
        seen.add(r)
        calc = ctx.resolve(r)
        for inp in (calc or {}).get("inputs") or []:
            (refs if inp["ref"].startswith("ev_") else stack).append(inp["ref"])
    return refs


def rule_answer_submit(ctx: RuleContext) -> None:
    """R-SUBMIT：进入 submitted 时所引 Evidence 非 expired，且无 open 的 blocking Conflict。"""
    a = ctx.new
    if not (ctx.entering("submitted") and ctx.prev is not None):
        return  # 历史导入（origin=imported）不经此路
    ev_refs = _cited_evidence(ctx, a)
    for r in ev_refs:
        head = ctx.get_head(r.split("#", 1)[0])
        if head is not None and head.get("status") in ("expired", "rejected"):
            raise GraphError("GRAPH_SUBMIT_BLOCKED", f"cited evidence {r} is {head.get('status')}", path="/evidence_refs",
                             details={"evidence": r})
    involved = {a["id"]} | {r.split("#", 1)[0] for r in ev_refs}
    if a.get("calculation_ref"):
        involved.add(a["calculation_ref"].split("#", 1)[0])
    for c in ctx.reader.heads_where(ctx.org_id, "Conflict", status="open"):
        if c.get("severity") != "blocking":
            continue
        if {r.split("#", 1)[0] for r in c.get("subject_refs") or []} & involved:
            raise GraphError("GRAPH_SUBMIT_BLOCKED", f"open blocking conflict {c['id']} ({c.get('rule_id')})",
                             details={"conflict": f"{c['id']}#v{c['version']}"})


ANSWER_RULES = (
    rule_answer_datapoint, rule_answer_unit, rule_answer_val, rule_answer_period, rule_answer_num_in_text,
    rule_answer_apr, rule_answer_one, rule_answer_submit,
)

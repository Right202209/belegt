# Spec: 03 §3.2–§3.13 各对象不变量；R-UNIT、R-VAL（Calculation 输入）、R-HASH、R-FS1；04 §2.1 EV-AUTO-0.90-LOC
from __future__ import annotations

from typing import Any

from ..authz import APPROVER_ROLES
from ..canonical import to_decimal
from ..errors import GraphError
from ..hashing import build_input_m, input_hash, output_hash
from .context import RuleContext
from .numtext import allowed_numbers, unsourced_numbers


# ------------------------------------------------------------------ Request（04 §3.1 不变量）
def _request_answers(ctx: RuleContext, req_id: str) -> list[dict[str, Any]]:
    qids = {q["id"] for q in ctx.reader.heads_where(ctx.org_id, "Question", request_id=req_id)}
    out = []
    for a in ctx.reader.heads_where(ctx.org_id, "Answer"):
        if a.get("status") in ("superseded", "rejected"):
            continue
        if set(a.get("question_refs") or []) & qids:
            out.append(a)
    return out


def rule_request(ctx: RuleContext) -> None:
    r = ctx.new
    if ctx.entering("mapped"):
        qs = ctx.reader.heads_where(ctx.org_id, "Question", request_id=r["id"])
        bad = [q["id"] for q in qs if q.get("status") not in ("mapped", "answered")]
        if bad:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "T05 requires every Question to be mapped", path="/status",
                             details={"unmapped": bad[:20]})
    if ctx.entering("approved") or ctx.entering("submitted"):
        answers = _request_answers(ctx, r["id"])
        bad = [a["id"] for a in answers if a.get("status") not in ("approved", "submitted")]
        if bad:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "all linked answers must be approved (T14/T16, 03 §3.2)",
                             path="/status", details={"not_approved": bad[:20]})


# ------------------------------------------------------------------ Question
def rule_question(ctx: RuleContext) -> None:
    q = ctx.new
    if ctx.prev is None:
        for other in ctx.reader.heads_where(ctx.org_id, "Question", request_id=q["request_id"]):
            if other["id"] != q["id"] and other.get("ordinal") == q["ordinal"]:
                raise GraphError("GRAPH_SCHEMA_INVALID", f"(request_id, ordinal) must be unique; {other['id']} has ordinal "
                                 f"{q['ordinal']}", path="/ordinal")
    if ctx.entering("mapped"):
        maps = ctx.reader.heads_where(ctx.org_id, "Mapping", question_id=q["id"], status="confirmed")
        if not maps:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "Question mapped requires a confirmed Mapping (03 §3.3)",
                             path="/status")


# ------------------------------------------------------------------ Mapping
def rule_mapping(ctx: RuleContext) -> None:
    m = ctx.new
    if ctx.entering("confirmed"):
        for other in ctx.reader.heads_where(ctx.org_id, "Mapping", question_id=m["question_id"], status="confirmed"):
            if other["id"] != m["id"]:
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"question already has confirmed mapping {other['id']}",
                                 path="/status")
    dp = ctx.datapoints.get(m["datapoint_id"])
    if dp and dp.unit and m.get("unit_hint") and not ctx.units.compatible(m["unit_hint"], dp.unit):
        if ctx.entering("confirmed") and ctx.actor.id == "agent:guard":
            raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"unit_hint {m['unit_hint']} incompatible with {dp.unit}",
                             path="/unit_hint")


# ------------------------------------------------------------------ Evidence
def rule_evidence(ctx: RuleContext) -> None:
    e = ctx.new
    a = ctx.actor
    # R-UNIT
    if e.get("value_type") == "number":
        unit = e.get("unit")
        if not ctx.units.known(unit):
            raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"unknown unit {unit!r}", path="/unit")
        dp = ctx.datapoints.get(e["datapoint_id"]) if e.get("datapoint_id") else None
        if dp and dp.unit and not ctx.units.compatible(unit, dp.unit):
            raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"unit {unit} incompatible with DataPoint unit {dp.unit}", path="/unit")
    if e.get("valid_until") and e["valid_from"] > e["valid_until"]:
        raise GraphError("GRAPH_SCHEMA_INVALID", "valid_from must be <= valid_until", path="/valid_until")
    if e.get("source_type") == "human_confirmation" and not a.is_user and a.id != "service:graph-api":
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "human_confirmation evidence must be created by a user", rule_id="R-STATE")
    if ctx.entering("verified"):
        v = e.get("verification") or {}
        if a.id != "service:graph-api" and v.get("by") != a.id:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "verification.by must equal the writing actor", path="/verification/by")
        if v.get("method") == "auto_rule":
            if a.id != "agent:evidence-miner":
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "auto_rule verification only by evidence-miner", rule_id="R-STATE")
            if float(e.get("confidence", 0)) < 0.90:
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", "EV-AUTO-0.90-LOC requires confidence >= 0.90", path="/confidence")
            loc = e.get("locator") or {}
            if e.get("source_type") == "document" and not (loc.get("blob_sha256") and any(loc.get(k) for k in ("page", "cell", "bbox"))):
                raise GraphError("GRAPH_LOCATOR_MISSING", "auto_rule verification requires a locator", path="/locator")
            if e.get("source_type") == "supplier_response" and (e.get("plausibility") or {}).get("flag") != "ok":
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", "supplier_response with plausibility.flag != ok cannot be auto_rule "
                                 "verified (03 §3.5)", path="/plausibility/flag")
        if v.get("method") == "closed_question":
            cq = ctx.resolve(v["cq_ref"]) if v.get("cq_ref") else None
            if cq is None or cq.get("status") != "answered":
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", "closed_question verification requires an answered ClosedQuestion",
                                 path="/verification/cq_ref")
        if v.get("method") == "connector_attested" and e.get("source_type") != "erp":
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "connector_attested only for erp evidence", path="/verification/method")
    if ctx.entering("expired"):
        today = ctx.reader.now()[:10]
        if not e.get("valid_until") or e["valid_until"] >= today:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "evidence may expire only after valid_until (03 §3.5)", path="/status")


# ------------------------------------------------------------------ Calculation（R-HASH、R-FS1、R-UNIT、R-VAL）
def rule_calculation(ctx: RuleContext) -> None:
    c = ctx.new
    names = [i["name"] for i in c.get("inputs") or []]
    if len(names) != len(set(names)):
        raise GraphError("GRAPH_SCHEMA_INVALID", "inputs[].name must be unique within a calculation", path="/inputs")
    if not ctx.units.known(c["result"]["unit"]):
        raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"unknown result unit {c['result']['unit']!r}", path="/result/unit")
    # 输入值 = 被引对象版本的值（R-VAL），单位兼容（R-UNIT）
    for i, inp in enumerate(c.get("inputs") or []):
        tgt = ctx.resolve(inp["ref"])
        if tgt is None:
            continue
        if inp["ref"].startswith("calc_"):
            sval, sunit = to_decimal(tgt["result"]["value"]), tgt["result"]["unit"]
        else:
            if tgt.get("value_type") != "number":
                raise GraphError("GRAPH_NUM_VALUE_MISMATCH", f"input {inp['ref']} is not numeric", path=f"/inputs/{i}/ref")
            sval, sunit = to_decimal(tgt["extracted_value"]), tgt.get("unit")
        if not ctx.units.compatible(sunit, inp["unit"]):
            raise GraphError("GRAPH_UNIT_INCOMPATIBLE", f"input unit {inp['unit']} incompatible with source unit {sunit}",
                             path=f"/inputs/{i}/unit")
        conv = ctx.units.convert(sval, sunit, inp["unit"])
        if conv.normalize() != to_decimal(inp["value"]).normalize():
            raise GraphError("GRAPH_NUM_VALUE_MISMATCH", f"input {inp['name']} value {inp['value']} != referenced "
                             f"{format(conv.normalize(), 'f')}", path=f"/inputs/{i}/value")
    fs = ctx.get_head(c["factor_set_version"])
    if fs is None:
        return  # R-REF 已报
    # R-FS1：factors_used 必须全部来自该 FactorSet（行已登记时检查）
    rows = ctx.reader.factor_rows(c["factor_set_version"])
    if rows:
        by_id = {r["source_row_id"]: r for r in rows}
        for j, f in enumerate(c.get("factors_used") or []):
            row = by_id.get(f["source_row_id"])
            if row is None or row["factor_key"] != f["factor_key"] or \
                    to_decimal(row["value"]).normalize() != to_decimal(f["value"]).normalize():
                raise GraphError("GRAPH_SCHEMA_INVALID", f"factor {f['source_row_id']} is not a row of {c['factor_set_version']} "
                                 "(one FactorSet per Calculation)", rule_id="R-FS1", path=f"/factors_used/{j}")
    # R-HASH
    m = build_input_m(c, fs["rows_hash"])
    try:
        expected = input_hash(m)
    except ValueError as e:
        raise GraphError("GRAPH_HASH_MISMATCH", str(e), path="/input_hash")
    if expected != c["input_hash"]:
        raise GraphError("GRAPH_HASH_MISMATCH", "input_hash does not match server recomputation", path="/input_hash",
                         details={"expected": expected, "got": c["input_hash"]})
    exp_out = output_hash(c["result"], c.get("breakdown"))
    if exp_out != c["output_hash"]:
        raise GraphError("GRAPH_HASH_MISMATCH", "output_hash does not match JCS(result + breakdown)", path="/output_hash",
                         details={"expected": exp_out, "got": c["output_hash"]})


# ------------------------------------------------------------------ Approval
def rule_approval(ctx: RuleContext) -> None:
    ap = ctx.new
    subj = ctx.resolve(ap["subject_ref"])
    if ctx.prev is None and subj is not None and ap["subject_ref"].startswith("ans_"):
        if subj.get("status") != "in_review":
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "Approval.subject_ref must pin an in_review Answer version (03 §3.8)",
                             path="/subject_ref")
        if subj.get("risk_level") and subj["risk_level"] != ap["risk_level"]:
            raise GraphError("GRAPH_SCHEMA_INVALID", "Approval.risk_level must be copied from the Answer", path="/risk_level")
    if ctx.prev is not None and ap["subject_ref"] != ctx.prev["subject_ref"]:
        raise GraphError("GRAPH_SCHEMA_INVALID", "subject_ref is immutable; re-create a pending approval instead",
                         path="/subject_ref")
    if ap["status"] != "pending" and ctx.actor.is_user:
        a = ctx.actor
        if ap.get("decided_by") != a.id:
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "decided_by must be the deciding user", path="/decided_by")
        allowed = APPROVER_ROLES.get(ap["risk_level"], frozenset())
        if not (a.roles & set(ap.get("required_role") or [])) or not (a.roles & allowed):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", f"{a.id} lacks a required approver role for {ap['risk_level']}",
                             details={"required_role": ap.get("required_role"), "allowed_for_risk": sorted(allowed)})
        if ap["risk_level"] == "L3" and ap.get("batch_id"):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "L3 cannot be batch approved", path="/batch_id")
    if ap["status"] == "rejected" and ctx.actor.id == "agent:approval-router" and ap.get("comment") != "stale":
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", "approval-router may only reject stale approvals (comment='stale')",
                         path="/comment")


# ------------------------------------------------------------------ Conflict
def rule_conflict(ctx: RuleContext) -> None:
    c = ctx.new
    if ctx.entering("open") or (ctx.prev is None and c["status"] == "open"):
        for other in ctx.reader.heads_where(ctx.org_id, "Conflict", status="open"):
            if other["id"] != c["id"] and other.get("conflict_key") == c.get("conflict_key"):
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"duplicate open conflict {other['id']} (same conflict_key)",
                                 path="/conflict_key")
    res = c.get("resolution")
    if res and ctx.actor.is_user and res.get("resolved_by") != ctx.actor.id:
        raise GraphError("GRAPH_SCHEMA_INVALID", "resolution.resolved_by must be the resolving user", path="/resolution/resolved_by")
    if res and res.get("approval_ref") and c["status"] in ("resolved", "accepted_divergence", "dismissed") and ctx.actor.is_user:
        apr = ctx.resolve(res["approval_ref"])
        if apr is None or apr.get("status") not in ("approved", "modified"):
            raise GraphError("GRAPH_APPROVAL_REQUIRED", "conflict resolution requires a decided Approval",
                             path="/resolution/approval_ref")
    div = (res or {}).get("divergence")
    if div:
        sides = div["sides"]
        scopes = []
        for i, s in enumerate(sides):
            ans = ctx.resolve(s["answer"])
            if ans is not None and ans.get("customer_scope") != s["customer_scope"]:
                raise GraphError("GRAPH_SCHEMA_INVALID", "divergence side customer_scope must match the Answer",
                                 path=f"/resolution/divergence/sides/{i}/customer_scope")
            scopes.append(s["customer_scope"])
        for i in range(len(scopes)):
            for j in range(i + 1, len(scopes)):
                a, b = scopes[i], scopes[j]
                if a == "all" or b == "all" or set(a) & set(b):
                    raise GraphError("GRAPH_SCHEMA_INVALID", "divergence sides must have mutually exclusive customer_scope",
                                     path="/resolution/divergence/sides")
    if c.get("explanation"):
        bad = unsourced_numbers(c["explanation"].values(), allowed_numbers([c.get("detail") or {}]))
        if bad:
            raise GraphError("GRAPH_NUM_IN_TEXT", f"explanation contains unsourced numbers: {bad}", path="/explanation")


# ------------------------------------------------------------------ SupplierRequest
def rule_supplier_request(ctx: RuleContext) -> None:
    s = ctx.new
    if ctx.prev and s["reminder_count"] < ctx.prev.get("reminder_count", 0):
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", "reminder_count may only increase", path="/reminder_count")
    if ctx.prev and s["status"] == "reminded" and s["reminder_count"] <= ctx.prev.get("reminder_count", 0):
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", "each reminder must increment reminder_count", path="/reminder_count")
    if ctx.entering("sent"):
        history = ctx.reader.versions_of(ctx.org_id, s["id"])
        first_send = not any(v.get("status") == "sent" for v in history)
        if first_send:
            ok = any(
                a.get("subject_ref", "").split("#", 1)[0] == s["id"] and a.get("status") == "approved"
                for a in ctx.reader.heads_where(ctx.org_id, "Approval")
            )
            if not ok:
                raise GraphError("GRAPH_APPROVAL_REQUIRED",
                                 "first send requires an approved Approval(subject_ref=sreq_…#vN) (06 §4.4)", path="/status")
        if not ctx.reader.has_audit(ctx.org_id, "egress.send", s["id"]):
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "sent requires an egress-gateway AuditEvent(egress.send)", path="/status")


# ------------------------------------------------------------------ ClosedQuestion
def rule_closed_question(ctx: RuleContext) -> None:
    cq = ctx.new
    if ctx.entering("answered"):
        a = ctx.actor
        ans = cq.get("answer") or {}
        if ans.get("answered_by") != a.id:
            raise GraphError("GRAPH_SCHEMA_INVALID", "answer.answered_by must be the answering user", path="/answer/answered_by")
        if a.roles <= {"contributor", "supplier_user"} and cq.get("assignee") != a.id and cq.get("assignee_role") not in a.roles:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "only the assignee may answer", rule_id="R-STATE")
        if cq["kind"] == "single_choice" and ans.get("choice") not in {o["key"] for o in cq.get("options") or []}:
            raise GraphError("GRAPH_SCHEMA_INVALID", "answer.choice must be one of options[].key", path="/answer/choice")
        if cq["kind"] in ("yes_no", "numeric_confirm") and not isinstance(ans.get("bool"), bool):
            raise GraphError("GRAPH_SCHEMA_INVALID", f"{cq['kind']} answer requires bool", path="/answer/bool")
    if ctx.prev is not None and cq["target_ref"] != ctx.prev["target_ref"]:
        raise GraphError("GRAPH_SCHEMA_INVALID", "target_ref is immutable", path="/target_ref")


# ------------------------------------------------------------------ FactorSet / 主数据
def rule_factor_set(ctx: RuleContext) -> None:
    f = ctx.new
    if f.get("valid_until") and f["valid_from"] > f["valid_until"]:
        raise GraphError("GRAPH_SCHEMA_INVALID", "valid_from must be <= valid_until", path="/valid_until")
    src = f["id"][3:].split("@", 1)[0]
    if src != f["source"]:
        raise GraphError("GRAPH_SCHEMA_INVALID", "FactorSet.source must match the id fs:<source>@<version>", path="/source")
    if ctx.prev is not None:
        for k in ("rows_hash", "row_count", "source", "published_at", "valid_from"):
            if ctx.prev.get(k) != f.get(k):
                raise GraphError("GRAPH_SCHEMA_INVALID", f"FactorSet content is immutable ({k}); publish a new id",
                                 path=f"/{k}")


TYPE_RULES = {
    "Request": (rule_request,),
    "Question": (rule_question,),
    "Mapping": (rule_mapping,),
    "Evidence": (rule_evidence,),
    "Calculation": (rule_calculation,),
    "Approval": (rule_approval,),
    "Conflict": (rule_conflict,),
    "SupplierRequest": (rule_supplier_request,),
    "ClosedQuestion": (rule_closed_question,),
    "FactorSet": (rule_factor_set,),
}

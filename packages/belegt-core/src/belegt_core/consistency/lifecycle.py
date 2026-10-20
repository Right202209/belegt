# Spec: 06 §3（冲突生命周期：open → resolved | accepted_divergence | dismissed；auto_cleared）、§3.2（三种处理选项、
#       block 冲突 B/C 仅 compliance_owner、A 最终仍需对应风险级别的 Approval）、§3.3（ADR/1 resolution.divergence 硬性要求）、
#       03 §3.7（人工修改数字先写 human_confirmation Evidence）、§3.8（Approval subject_ref 可指向 Conflict）、§3.9
"""冲突处理（人工裁决落图）。

``resolve_conflict(store, conflict_id, actor=…, kind=…, note=…, ...)`` 在一个事务内写入：
``Approval(pending, approval-router)`` → ``Approval(approved, 用户)`` → （A：human_confirmation Evidence + 新 Answer）→
``Conflict`` 终态版本（``resolution.approval_ref`` 指向该 Approval）。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from ..authz import Actor
from ..errors import GraphError
from ..ids import datapoint_base, new_id
from ..store import WriteItem
from .context import AuditContext, parse_date, ref_of

ROUTER = "agent:approval-router"
REASON_CODES = ("customer_methodology", "consolidation", "period", "product_scope", "boundary", "timing_correction")
KINDS = ("corrected", "accepted_divergence", "dismissed")
RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


def _add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    import calendar

    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _scopes_disjoint(a: Any, b: Any) -> bool:
    if a == "all" or b == "all":
        return False
    return not (set(a or []) & set(b or []))


def validate_divergence(ctx: AuditContext, conflict: dict[str, Any], div: dict[str, Any]) -> list[dict[str, str]]:
    """06 §3.3 硬性要求 1/4 与 03 §3.9 不变量；返回错误列表（空 = 通过）。"""
    errs: list[dict[str, str]] = []

    def err(path: str, msg: str) -> None:
        errs.append({"path": f"/divergence{path}", "message": msg})

    if div.get("format", "ADR/1") != "ADR/1":
        err("/format", "format must be ADR/1")
    if div.get("reason_code") not in REASON_CODES:
        err("/reason_code", f"reason_code must be one of {REASON_CODES}")
    if not isinstance(div.get("include_in_export"), bool):
        err("/include_in_export", "include_in_export (bool) is required")
    sides = div.get("sides") or []
    if len(sides) < 2:
        err("/sides", "at least two sides required")
    reqs = list(div.get("customer_requirement_refs") or [])
    if not reqs:
        err("/customer_requirement_refs", "each side needs at least one customer requirement reference; otherwise choose A")
    subj_answers = {s for s in conflict.get("subject_refs") or [] if s.startswith("ans_")}
    side_refs = {s.get("answer") for s in sides}
    if subj_answers and side_refs != subj_answers:
        err("/sides", "sides[].answer must be exactly the conflict's pinned answer versions")
    ends: list[dt.date] = []
    for i, s in enumerate(sides):
        ans = ctx.get(s.get("answer") or "")
        if ans is None:
            err(f"/sides/{i}/answer", "answer not found")
            continue
        if ans.get("customer_scope") != s.get("customer_scope"):
            err(f"/sides/{i}/customer_scope", "customer_scope must match the Answer")
        if not s.get("basis"):
            err(f"/sides/{i}/basis", "basis required")
        ends.append(parse_date(ans["period"]["end"]))  # type: ignore[arg-type]
        covered = False
        for r in reqs:
            if r in (ans.get("question_refs") or []):
                covered = True
            q = ctx.head(r)
            if q and q.get("request_id"):
                req = ctx.head(q["request_id"])
                cs = s.get("customer_scope")
                if req and isinstance(cs, list) and req.get("counterparty_id") in cs:
                    covered = True
        if not covered:
            err(f"/sides/{i}", "side has no customer requirement reference (question of that customer)")
    for i in range(len(sides)):
        for j in range(i + 1, len(sides)):
            if not _scopes_disjoint(sides[i].get("customer_scope"), sides[j].get("customer_scope")):
                err("/sides", "sides must have mutually exclusive customer_scope")
    rb = parse_date(div.get("review_by"))
    if rb is None:
        err("/review_by", "review_by required")
    elif ends and rb > _add_months(max(ends), 15):
        err("/review_by", "review_by must be no later than 15 months after the sides' period end")
    return errs


def _approval_level(ctx: AuditContext, conflict: dict[str, Any], kind: str) -> str:
    if kind == "accepted_divergence":
        return "L3"  # 06 §3.3-3 / _baseline F
    lvl = "L2" if conflict.get("severity") == "warning" else "L2"
    for s in conflict.get("subject_refs") or []:
        if s.startswith("ans_"):
            a = ctx.head(s) or {}
            rl = a.get("risk_level")
            if rl and RISK_ORDER[rl] > RISK_ORDER[lvl]:
                lvl = rl
            if a.get("legal_commitment"):
                lvl = "L3"
    if conflict.get("rule_id") in ("CR-001", "CR-007"):
        lvl = "L3"
    return lvl


def resolve_conflict(store: Any, conflict_id: str, *, actor: Actor, kind: str, note: str,
                     base_side: int | None = None, dismiss_reason: str | None = None,
                     divergence: dict[str, Any] | None = None, policy_version: str = "approval-policy@1.0.0",
                     now: str | None = None, context: AuditContext | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise GraphError("GRAPH_SCHEMA_INVALID", f"kind must be one of {KINDS}", path="/kind")
    if not note or not note.strip():
        raise GraphError("GRAPH_SCHEMA_INVALID", "note is required", path="/note")
    if not actor.is_user:
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "conflicts are resolved by a user (06 §3.2)")
    ctx = context or AuditContext.from_store(store, actor.org_id)
    c = ctx.head(conflict_id)
    if c is None or not conflict_id.startswith("cfl_"):
        raise GraphError("GRAPH_NOT_FOUND", f"{conflict_id} not found")
    if c.get("status") != "open":
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"conflict is {c.get('status')}; only open conflicts can be resolved",
                         path="/status")
    if kind in ("accepted_divergence", "dismissed") and not actor.has_role("compliance_owner"):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "only compliance_owner may accept a divergence or dismiss (06 §3.2)",
                         details={"required_role": ["compliance_owner"]})
    if not actor.has_role("compliance_owner"):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "resolving a conflict requires compliance_owner (03 §3.9 state machine)",
                         details={"required_role": ["compliance_owner"]})
    now = now or store.peek_now()
    cref = ref_of(c)
    level = _approval_level(ctx, c, kind)
    req_id = c.get("request_id")
    apr_id = new_id("Approval")
    apr_pending = {"id": apr_id, "subject_ref": cref, "risk_level": level,
                   "required_role": ["compliance_owner", "executive"] if level == "L3" else ["compliance_owner"],
                   "policy_version": policy_version.partition("+")[0], "status": "pending", "trust_zone": "trusted"}
    if "+" in policy_version:  # 03 schema 不接受 +ov<N> 后缀 → 以标签记录覆盖版本
        apr_pending["tags"] = [f"policy_overlay:{policy_version.partition('+')[2]}"]
    if req_id:
        apr_pending["request_id"] = req_id
    apr_ref = f"{apr_id}#v2"
    items: list[WriteItem] = [
        WriteItem("Approval", apr_pending, actor=Actor.of(ROUTER, actor.org_id)),
        WriteItem("Approval", {"id": apr_id, "status": "approved", "decision": "approved", "decided_by": actor.id,
                               "decided_at": now, "comment": note[:4000]}, expected_version=1, patch_mode="merge_from_head",
                  actor=actor),
    ]
    resolution: dict[str, Any] = {"kind": kind, "note": note[:4000], "approval_ref": apr_ref, "resolved_by": actor.id}
    status = {"corrected": "resolved", "accepted_divergence": "accepted_divergence", "dismissed": "dismissed"}[kind]
    followups: list[dict[str, Any]] = []
    created: dict[str, str] = {}

    if kind == "accepted_divergence":
        if not divergence:
            raise GraphError("GRAPH_SCHEMA_INVALID", "divergence (ADR/1) required for accepted_divergence (03 §3.9)",
                             path="/divergence")
        div = {"format": "ADR/1", **divergence}
        errs = validate_divergence(ctx, c, div)
        if errs:
            raise GraphError("GRAPH_SCHEMA_INVALID", errs[0]["message"], path=errs[0]["path"], rule_id="ADR/1",
                             details={"errors": errs})
        resolution["divergence"] = {k: v for k, v in div.items() if v is not None}
    elif kind == "dismissed":
        if dismiss_reason:
            resolution["note"] = f"[{dismiss_reason}] {note}"[:4000]
        recent = [x for x in ctx.heads("Conflict", status="dismissed", rule_id=c["rule_id"])
                  if (parse_date(x.get("created_at")) or ctx.today) >= ctx.today - dt.timedelta(days=30)]
        followups.append({"type": "eval.false_positive_sample", "rule_id": c["rule_id"], "conflict_ref": cref,
                          "dismiss_reason": dismiss_reason})
        if len(recent) + 1 >= 5:
            followups.append({"type": "rule.review_required", "rule_id": c["rule_id"], "dismissed_30d": len(recent) + 1})
    else:  # corrected
        answers = [s for s in c.get("subject_refs") or [] if s.startswith("ans_")]
        if base_side is None:
            if not any(ctx.current_successor(s) is not None for s in c.get("subject_refs") or []):
                raise GraphError("GRAPH_SCHEMA_INVALID", "corrected requires base_side, or a subject already corrected "
                                 "(new version) beforehand", path="/base_side")
        else:
            if not (0 <= base_side < len(answers)):
                raise GraphError("GRAPH_SCHEMA_INVALID", f"base_side must index one of {len(answers)} answer sides",
                                 path="/base_side")
            base = ctx.get(answers[base_side])
            for i, sref in enumerate(answers):
                if i == base_side:
                    continue
                old = ctx.get(sref)
                if old is None or base is None:
                    raise GraphError("GRAPH_NOT_FOUND", f"{sref} not found")
                new_items, info = _correction_items(ctx, old, base, actor, apr_ref, now)
                items.extend(new_items)
                created.update(info)
                if (ctx.head(old["id"]) or {}).get("status") == "submitted":
                    followups.append({"type": "customer_correction", "assignee_role": "sales", "answer_ref": sref,
                                      "new_answer_ref": info.get("answer"), "note": "external send still requires confirmation"})
    items.append(WriteItem("Conflict", {"id": c["id"], "status": status, "resolution": resolution},
                           expected_version=c["version"], patch_mode="merge_from_head", actor=actor))
    refs = store.write_batch(items, actor=actor)
    return {"conflict_ref": refs[-1], "approval_ref": refs[1], "status": status, "created": created,
            "followups": followups}


def _correction_items(ctx: AuditContext, old: dict[str, Any], base: dict[str, Any], actor: Actor, apr_ref: str,
                      now: str) -> tuple[list[WriteItem], dict[str, str]]:
    """选项 A：以基准侧为准，为另一侧生成更正答案（数字先写 human_confirmation Evidence，03 §3.7）。"""
    skip = ("id", "version", "created_at", "content_hash", "answer_key", "created_by", "approval_ref", "risk_level",
            "submission", "provenance", "supersedes", "promotion", "reused_from", "org_id")
    new_ans = {k: v for k, v in old.items() if k not in skip}
    new_ans.update(id=new_id("Answer"), status="draft", origin="human", supersedes=ref_of(old), trust_zone="trusted")
    items: list[WriteItem] = []
    info: dict[str, str] = {}
    if old.get("value_type") == "number":
        b = ctx.numeric(base)
        val = ctx.convert(b[0], b[1], old.get("unit")) if b else None
        if val is None:
            raise GraphError("GRAPH_UNIT_INCOMPATIBLE", "base side value not convertible to the corrected answer's unit")
        sval = format(val.normalize(), "f") if val != val.to_integral() else format(val.quantize(Decimal(1)), "f")
        ev_id = new_id("Evidence")
        ev = {"id": ev_id, "source_type": "human_confirmation", "datapoint_id": old["datapoint_id"],
              "fact_key": f"answer.{datapoint_base(old['datapoint_id'])[3:]}", "extracted_value": sval, "value_type": "number",
              "unit": old.get("unit"), "period": old["period"], "boundary": old["boundary"], "locator": {"approval_ref": apr_ref},
              "valid_from": old["period"]["start"], "confidence": 1, "status": "verified", "trust_zone": "trusted",
              "verification": {"method": "human_review", "by": actor.id}}
        items.append(WriteItem("Evidence", ev, actor=actor))
        new_ans.update(value=sval, evidence_refs=[f"{ev_id}#v1"], calculation_ref=None)
        new_ans.pop("calculation_ref")
        info["evidence"] = f"{ev_id}#v1"
    else:
        new_ans.update(value=base.get("value"), evidence_refs=list(base.get("evidence_refs") or []))
    new_ans.pop("drafting_note", None)
    items.append(WriteItem("Answer", new_ans, actor=actor))
    info["answer"] = f"{new_ans['id']}#v1"
    return items, info


__all__ = ["resolve_conflict", "validate_divergence", "REASON_CODES"]

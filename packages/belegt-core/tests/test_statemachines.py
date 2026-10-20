"""状态机：合法/非法转移（Request T01…T23 含 return_to；SupplierRequest reminded→expired、escalated→responded）。"""
from __future__ import annotations

import pytest

from belegt_core import GraphError, new_id
from belegt_core.statemachines import (
    REQUEST_SM, STATE_MACHINES, SUPPLIER_REQUEST_SM, check_transition, parse_status_reason,
)

from helpers import (
    ADMIN, CHASER, CO, EGRESS, FIX, GUARD, INTAKE, MINER, REVIEWER, ROUTER, WORKFLOW, A, doc, strip_env,
)

FILLER = A("agent:filler")


def ok(type_name, src, dst, actor, **extra):
    prev = {"status": src, **extra.get("prev", {})} if src else None
    new = {"status": dst, **extra.get("new", {})}
    return check_transition(type_name, prev, new, actor)


def bad(type_name, src, dst, actor, code="GRAPH_ILLEGAL_TRANSITION", **extra):
    with pytest.raises(GraphError) as e:
        ok(type_name, src, dst, actor, **extra)
    assert e.value.code == code, e.value
    return e.value


# ---------------------------------------------------------------- Request（04 §3.2）
REQUEST_LEGAL = [
    ("T01", None, "received", INTAKE), ("T02", "received", "triaged", GUARD), ("T02", "received", "triaged", CO),
    ("T03", "received", "cancelled", CO), ("T04", "triaged", "parsed", GUARD), ("T04", "triaged", "parsed", WORKFLOW),
    ("T05", "parsed", "mapped", GUARD), ("T06", "mapped", "evidence_gathering", WORKFLOW),
    ("T07", "evidence_gathering", "awaiting_supplier", CHASER), ("T08", "awaiting_supplier", "evidence_gathering", WORKFLOW),
    ("T09", "evidence_gathering", "drafting", WORKFLOW), ("T10", "drafting", "auditing", A("agent:answer-drafter")),
    ("T11", "auditing", "in_review", ROUTER), ("T12", "in_review", "auditing", ROUTER),
    ("T13", "in_review", "drafting", ROUTER), ("T14", "in_review", "approved", ROUTER),
    ("T15", "approved", "filling", FILLER), ("T16", "filling", "submitted", FILLER),
    ("T17", "submitted", "archived", WORKFLOW), ("T23", "in_review", "cancelled", CO),
]


@pytest.mark.parametrize("tid,src,dst,actor", REQUEST_LEGAL, ids=[f"{t[0]}:{t[1]}->{t[2]}" for t in REQUEST_LEGAL])
def test_request_legal(tid, src, dst, actor):
    t = ok("Request", src, dst, actor)
    assert t.id == tid


@pytest.mark.parametrize("src,dst", [("received", "parsed"), ("triaged", "mapped"), ("drafting", "approved"),
                                     ("submitted", "drafting"), ("archived", "received"), ("approved", "submitted")])
def test_request_illegal(src, dst):
    bad("Request", src, dst, WORKFLOW)


def test_request_wrong_actor():
    bad("Request", "received", "triaged", MINER, code="GRAPH_ACTOR_NOT_ALLOWED")
    bad("Request", "drafting", "halted", WORKFLOW, code="GRAPH_ACTOR_NOT_ALLOWED", new={"status_reason": "halted:budget;return_to=drafting"})


def test_request_terminal_frozen():
    bad("Request", "archived", "archived", WORKFLOW)
    bad("Request", "cancelled", "received", CO)


def test_request_t18_t19_return_to():
    reason = "awaiting_human:cq;return_to=parsed"
    t = ok("Request", "parsed", "awaiting_human", MINER, new={"status_reason": reason})
    assert t.id == "T18"
    # 任一非终态可进入（如 auditing、filling、awaiting_supplier）
    for s in ("auditing", "filling", "awaiting_supplier", "drafting"):
        ok("Request", s, "awaiting_human", ROUTER, new={"status_reason": f"awaiting_human:conflict;return_to={s}"})
    t = ok("Request", "awaiting_human", "parsed", WORKFLOW, prev={"status_reason": reason})
    assert t.id == "T19"
    bad("Request", "awaiting_human", "mapped", WORKFLOW, prev={"status_reason": reason})  # 不是 return_to
    bad("Request", "parsed", "awaiting_human", MINER, new={"status_reason": "waiting"})  # 缺 return_to
    bad("Request", "parsed", "awaiting_human", MINER, new={"status_reason": "awaiting_human:cq;return_to=archived"})
    bad("Request", "submitted", "awaiting_human", MINER, new={"status_reason": "awaiting_human:confirm;return_to=filling"})


def test_request_t20_t21_t22_halted():
    reason = "halted:budget;return_to=evidence_gathering"
    t = ok("Request", "evidence_gathering", "halted", GUARD, new={"status_reason": reason})
    assert t.id == "T20"
    ok("Request", "in_review", "halted", GUARD, new={"status_reason": "halted:loop;return_to=in_review"})  # 任一非终态
    t = ok("Request", "halted", "evidence_gathering", CO, prev={"status_reason": reason})
    assert t.id == "T21"
    ok("Request", "halted", "evidence_gathering", ADMIN, prev={"status_reason": reason})
    bad("Request", "halted", "evidence_gathering", WORKFLOW, code="GRAPH_ACTOR_NOT_ALLOWED", prev={"status_reason": reason})
    bad("Request", "halted", "drafting", CO, prev={"status_reason": reason})  # 非 return_to
    t = ok("Request", "halted", "cancelled", CO, prev={"status_reason": reason})
    assert t.id == "T22"


def test_request_cancel_from_any_pre_submit():
    for s in ("triaged", "parsed", "mapped", "evidence_gathering", "drafting", "auditing", "approved", "filling"):
        ok("Request", s, "cancelled", CO)
    bad("Request", "submitted", "cancelled", CO)


def test_request_all_t_ids_present():
    ids = {t.id for t in REQUEST_SM.transitions}
    assert {f"T{i:02d}" for i in range(1, 24)} <= ids


def test_parse_status_reason():
    assert parse_status_reason("halted:budget;return_to=drafting") == {"kind": "halted", "reason": "budget",
                                                                       "return_to": "drafting"}
    assert parse_status_reason("foo") is None


# ---------------------------------------------------------------- SupplierRequest（03 §3.10 + 08 §2.3）
SR_LEGAL = [
    (None, "draft", CHASER), ("draft", "sent", EGRESS), ("draft", "sent", CO), ("sent", "reminded", CHASER),
    ("reminded", "reminded", CHASER), ("sent", "responded", REVIEWER), ("reminded", "responded", WORKFLOW),
    ("responded", "reviewed", WORKFLOW), ("reviewed", "closed", MINER), ("reviewed", "sent", CHASER),
    ("reminded", "escalated", CHASER), ("sent", "expired", WORKFLOW), ("reminded", "expired", WORKFLOW),
    ("escalated", "closed", CO), ("escalated", "responded", REVIEWER),
]


@pytest.mark.parametrize("src,dst,actor", SR_LEGAL, ids=[f"{s}->{d}" for s, d, _ in SR_LEGAL])
def test_supplier_request_legal(src, dst, actor):
    ok("SupplierRequest", src, dst, actor)


def test_supplier_request_illegal():
    bad("SupplierRequest", "draft", "responded", REVIEWER)
    bad("SupplierRequest", "closed", "sent", CHASER)
    bad("SupplierRequest", "expired", "responded", REVIEWER)
    bad("SupplierRequest", "sent", "escalated", CHASER)
    bad("SupplierRequest", "reminded", "expired", CHASER, code="GRAPH_ACTOR_NOT_ALLOWED")


# ---------------------------------------------------------------- 其它对象（03 §3.3–§3.13）
@pytest.mark.parametrize("type_name,src,dst,actor", [
    ("Question", None, "parsed", A("agent:question-parser")), ("Question", "parsed", "mapped", GUARD),
    ("Question", "parsed", "unmapped", GUARD), ("Question", "unmapped", "mapped", CO),
    ("Question", "mapped", "answered", ROUTER), ("Question", "answered", "mapped", ROUTER),
    ("Mapping", None, "candidate", A("agent:question-parser")), ("Mapping", "candidate", "confirmed", GUARD),
    ("Mapping", "candidate", "rejected", CO), ("Mapping", "confirmed", "rejected", CO),
    ("Evidence", None, "candidate", MINER), ("Evidence", "candidate", "verified", MINER),
    ("Evidence", "candidate", "verified", CO), ("Evidence", "candidate", "rejected", CO),
    ("Evidence", "verified", "expired", A("agent:consistency-auditor")),
    ("Evidence", "verified", "superseded", A("service:graph-api")),
    ("Calculation", None, "computed", A("agent:calc-service")), ("Calculation", "computed", "superseded", A("agent:calc-service")),
    ("Answer", None, "draft", A("agent:calc-service")), ("Answer", None, "submitted", CO),
    ("Answer", "draft", "in_review", ROUTER), ("Answer", "in_review", "approved", CO),
    ("Answer", "in_review", "rejected", CO), ("Answer", "in_review", "draft", CO),
    ("Answer", "rejected", "draft", A("agent:answer-drafter")), ("Answer", "approved", "submitted", FILLER),
    ("Answer", "submitted", "superseded", A("service:graph-api")),
    ("Approval", None, "pending", ROUTER), ("Approval", "pending", "approved", CO),
    ("Approval", "pending", "modified", A("user:u_exec", "executive")), ("Approval", "pending", "rejected", ROUTER),
    ("Conflict", None, "open", A("agent:consistency-auditor")), ("Conflict", "open", "resolved", CO),
    ("Conflict", "open", "accepted_divergence", CO), ("Conflict", "open", "dismissed", CO),
    ("Conflict", "open", "resolved", A("agent:consistency-auditor")),
    ("ClosedQuestion", None, "open", MINER), ("ClosedQuestion", "open", "answered", CO),
    ("ClosedQuestion", "open", "answered", A("user:u_sup", "supplier_user")),
    ("ClosedQuestion", "open", "expired", WORKFLOW),
    ("FactorSet", None, "active", A("service:factor-import")), ("FactorSet", "active", "deprecated", ADMIN),
])
def test_object_legal(type_name, src, dst, actor):
    ok(type_name, src, dst, actor)


@pytest.mark.parametrize("type_name,src,dst,actor,code", [
    ("Question", "unmapped", "answered", ROUTER, "GRAPH_ILLEGAL_TRANSITION"),
    ("Question", "parsed", "unmapped", CO, "GRAPH_ACTOR_NOT_ALLOWED"),
    ("Mapping", "rejected", "confirmed", CO, "GRAPH_ILLEGAL_TRANSITION"),
    ("Evidence", "expired", "verified", MINER, "GRAPH_ILLEGAL_TRANSITION"),
    ("Evidence", "verified", "expired", MINER, "GRAPH_ACTOR_NOT_ALLOWED"),
    ("Calculation", "superseded", "computed", A("agent:calc-service"), "GRAPH_ILLEGAL_TRANSITION"),
    ("Answer", "draft", "approved", CO, "GRAPH_ILLEGAL_TRANSITION"),
    ("Answer", "draft", "submitted", FILLER, "GRAPH_ILLEGAL_TRANSITION"),
    ("Answer", "superseded", "draft", A("agent:calc-service"), "GRAPH_ILLEGAL_TRANSITION"),
    ("Answer", "in_review", "approved", A("user:u_sales", "sales"), "GRAPH_ACTOR_NOT_ALLOWED"),
    ("Approval", "approved", "rejected", CO, "GRAPH_ILLEGAL_TRANSITION"),
    ("Approval", "pending", "approved", ROUTER, "GRAPH_ACTOR_NOT_ALLOWED"),
    ("Conflict", "dismissed", "open", A("agent:consistency-auditor"), "GRAPH_ILLEGAL_TRANSITION"),
    ("Conflict", "open", "dismissed", A("agent:consistency-auditor"), "GRAPH_ACTOR_NOT_ALLOWED"),
    ("ClosedQuestion", "answered", "open", MINER, "GRAPH_ILLEGAL_TRANSITION"),
    ("FactorSet", "deprecated", "active", ADMIN, "GRAPH_ILLEGAL_TRANSITION"),
])
def test_object_illegal(type_name, src, dst, actor, code):
    bad(type_name, src, dst, actor, code=code)


def test_every_type_has_state_machine():
    assert set(STATE_MACHINES) == {"Request", "Question", "Mapping", "Evidence", "Calculation", "Answer", "Approval",
                                   "Conflict", "SupplierRequest", "ClosedQuestion", "FactorSet"}
    assert ("reminded", "expired") in {(t.src, t.dst) for t in SUPPLIER_REQUEST_SM.transitions}
    assert ("escalated", "responded") in {(t.src, t.dst) for t in SUPPLIER_REQUEST_SM.transitions}


# ---------------------------------------------------------------- 经 GraphStore 端到端：SupplierRequest 首发须审批 + egress 审计
def test_supplier_request_first_send_requires_approval_and_egress(store, chain):
    from helpers import ORG

    sup = new_id("Counterparty")
    store.write("Counterparty", {"id": sup, "role": "supplier", "legal_name": "Stahl AG", "domains": ["stahl.example"]},
                actor=ADMIN)
    sr = {"id": new_id("SupplierRequest"), "counterparty_id": sup, "datapoint_ids": ["dp:prod.cbam.see_direct@1.0.0"],
          "period": {"start": "2026-01-01", "end": "2026-03-31"}, "language": "de",
          "template_id": "stpl:cbam_request.de@1.0.0", "channel": "email", "due_date": "2026-12-15",
          "reminder_count": 0, "status": "draft"}
    store.write("SupplierRequest", sr, actor=CHASER)
    with pytest.raises(GraphError) as e:
        store.write("SupplierRequest", {"id": sr["id"], "status": "sent"}, expected_version=1, actor=EGRESS,
                    patch_mode="merge_from_head")
    assert e.value.code == "GRAPH_APPROVAL_REQUIRED"
    apr = {"id": new_id("Approval"), "subject_ref": f"{sr['id']}#v1", "risk_level": "L1",
           "required_role": ["compliance_owner"], "policy_version": "approval-policy@1.0.0", "status": "pending"}
    store.write("Approval", apr, actor=ROUTER)
    store.write("Approval", {"id": apr["id"], "status": "approved", "decision": "approved", "decided_by": CO.id,
                             "decided_at": "2026-11-05T09:00:00Z"}, expected_version=1, actor=CO, patch_mode="merge_from_head")
    with pytest.raises(GraphError) as e:
        store.write("SupplierRequest", {"id": sr["id"], "status": "sent"}, expected_version=1, actor=EGRESS,
                    patch_mode="merge_from_head")
    assert e.value.code == "GRAPH_ILLEGAL_TRANSITION"  # 缺 egress.send 审计
    store.record_audit(EGRESS, "egress.send", object_ref=f"{sr['id']}#v1", details={"channel": "email"})
    assert store.write("SupplierRequest", {"id": sr["id"], "status": "sent", "last_sent_at": "2026-11-05T09:01:00Z"},
                       expected_version=1, actor=EGRESS, patch_mode="merge_from_head").endswith("#v2")
    with pytest.raises(GraphError) as e:
        store.write("SupplierRequest", {"id": sr["id"], "status": "reminded", "reminder_count": 0}, expected_version=2,
                    actor=CHASER, patch_mode="merge_from_head")
    store.write("SupplierRequest", {"id": sr["id"], "status": "reminded", "reminder_count": 1}, expected_version=2,
                actor=CHASER, patch_mode="merge_from_head")
    store.write("SupplierRequest", {"id": sr["id"], "status": "expired"}, expected_version=3, actor=WORKFLOW,
                patch_mode="merge_from_head")
    assert store.head(sr["id"]).status == "expired"


def test_request_store_t05_requires_all_questions_mapped(store, chain):
    from helpers import FIX as F

    req = F["request"]["id"]
    q2 = strip_env(doc("question"))
    q2.update(id=new_id("Question"), ordinal=38, status="parsed", trust_zone="trusted")
    q2.pop("supersedes")
    store.write("Question", q2, actor=CO)
    with pytest.raises(GraphError) as e:
        store.write("Request", {"id": req, "status": "mapped"}, expected_version=3, actor=GUARD,
                    patch_mode="merge_from_head")
    assert e.value.code == "GRAPH_ILLEGAL_TRANSITION" and q2["id"] in e.value.details["unmapped"]

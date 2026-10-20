"""AuditEvent 哈希链：完整性、篡改检测、拒绝也记审计（03 §3.14、§6.8；07 §7）。"""
from __future__ import annotations

import pytest
from sqlalchemy import update

from belegt_core import GraphError, compute_event_hash, verify_events
from belegt_core.canonical import GENESIS_HASH
from belegt_core.models import AUDIT_ACTIONS
from belegt_core.schemas import validate as schema_validate
from belegt_core.store import tables as T

from helpers import CALC, EGRESS, FIX, ORG
from test_rules import answer_body


def test_chain_ok_and_linked(store, chain):
    evs = store.audit_events(ORG)
    assert evs[0]["seq"] == 1 and evs[0]["prev_hash"] == GENESIS_HASH
    for a, b in zip(evs, evs[1:]):
        assert b["seq"] == a["seq"] + 1 and b["prev_hash"] == a["event_hash"]
    for e in evs:
        assert compute_event_hash(e) == e["event_hash"]
        assert schema_validate("audit_event.json", e) == []
    assert store.verify_audit_chain(ORG).ok
    actions = {e["action"] for e in evs}
    assert {"object.create", "object.version", "object.promote"} <= actions
    # 晋升写 object.promote（Request v2、Mapping v2）
    promoted = {e["object_ref"] for e in evs if e["action"] == "object.promote"}
    assert f"{FIX['mapping']['id']}#v2" in promoted
    # 只存 object_ref 与 content_hash，不存业务值
    ans_ev = [e for e in evs if e.get("object_ref") == f"{FIX['answer']['id']}#v3"][0]
    assert ans_ev["content_hash"] == store.get(f"{FIX['answer']['id']}#v3")["content_hash"]
    assert "value" not in str(ans_ev.get("details"))


def test_tamper_detection_field_change(store, chain):
    with store.engine.begin() as conn:
        conn.execute(update(T.audit_event).where(T.audit_event.c.org_id == ORG, T.audit_event.c.seq == 5)
                     .values(object_ref="ans_01JD9B1C2D3E4F5G6H7J8K9M0N#v9"))
    rep = store.verify_audit_chain(ORG)
    assert not rep.ok and rep.first_bad_seq == 5


def test_tamper_detection_rehash_breaks_link(store, chain):
    evs = store.audit_events(ORG)
    forged = dict(evs[3], details={"forged": True})
    forged["event_hash"] = compute_event_hash(forged)  # 攻击者重算本条哈希
    with store.engine.begin() as conn:
        conn.execute(update(T.audit_event).where(T.audit_event.c.org_id == ORG, T.audit_event.c.seq == forged["seq"])
                     .values(details=forged["details"], event_hash=forged["event_hash"]))
    rep = store.verify_audit_chain(ORG)
    assert not rep.ok and rep.first_bad_seq == forged["seq"] + 1  # 下一条 prev_hash 断链


def test_tamper_detection_deletion(store, chain):
    from sqlalchemy import delete

    with store.engine.begin() as conn:
        conn.execute(delete(T.audit_event).where(T.audit_event.c.org_id == ORG, T.audit_event.c.seq == 7))
    rep = store.verify_audit_chain(ORG)
    assert not rep.ok and any("gap" in p for p in rep.problems)


def test_rejections_are_audited(store, chain):
    n = len(store.audit_events(ORG))
    with pytest.raises(GraphError):
        store.write("Answer", answer_body(value="1"), actor=CALC)
    evs = store.audit_events(ORG)
    assert len(evs) == n + 1
    rej = evs[-1]
    assert rej["action"] == "object.create" and rej["details"]["result"] == "rejected"
    assert rej["details"]["code"] == "GRAPH_NUM_VALUE_MISMATCH" and rej["details"]["rule_id"] == "R-VAL"
    assert rej["created_by"] == "agent:calc-service"
    assert store.verify_audit_chain(ORG).ok


def test_record_audit_actions(store, chain):
    store.record_audit(EGRESS, "egress.block", object_ref="sreq_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1",
                       details={"code": "EGRESS_DENIED_QUIET_PERIOD"})
    with pytest.raises(ValueError):
        store.record_audit(EGRESS, "egress.whatever")
    assert {"authz.change", "authz.deny", "key.rotate", "egress.block", "dsr.fulfil"} <= set(AUDIT_ACTIONS)
    assert store.verify_audit_chain(ORG).ok


def test_verify_events_segment(store, chain):
    evs = store.audit_events(ORG)
    seg = evs[3:9]
    assert verify_events(seg, expected_prev=evs[2]["event_hash"]).ok
    assert not verify_events(seg, expected_prev=evs[1]["event_hash"]).ok

"""review_router 端到端（create_app(routers=[review_router])）：形状兼容 web/README.md 端点表。"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from belegt_core.api import create_app
from belegt_core.api.auth import StaticTokenTable
from belegt_core.api.routes_review import configure_review, review_router
from belegt_core.ids import new_id

from review_helpers import BLOB, CHASER, CP_A, CP_B, MINER, ORG, World

S1 = "dp:env.ghg.scope1_total"
ELEC = "dp:energy.consumption.electricity_total"


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.t


@pytest.fixture
def env(store):
    t = StaticTokenTable()
    t.add("co", "user:u_compliance01", ORG, ["compliance_owner"])
    t.add("exec", "user:u_exec01", ORG, ["executive"])
    t.add("contrib", "user:u_contrib01", ORG, ["contributor"])
    t.add("wf", "service:workflow", ORG)
    app = create_app(store, t, routers=[review_router])
    clock = Clock()
    configure_review(app, clock=clock)
    return TestClient(app), World(store), clock


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def _answers(w, n=3, cp=CP_A):
    rq = w.request(cp, due="2026-12-15")
    refs = []
    for i in range(n):
        c = w.counterparty(name=f"K{i}")
        qq = w.question(rq, i + 1, f"Strom {i}")
        refs.append(w.answer(ELEC, "4820", "MWh", evidence=[w.evidence()], questions=[qq], cs=[c]))
    return rq, refs


def test_inbox_shape_and_single_approval(env):
    client, w, _ = env
    rq, refs = _answers(w, 2)
    assert client.get("/inbox", headers=H("co")).json() == {"items": []}
    r = client.post("/approvals/route", json={"request_id": rq}, headers=H("wf"))
    assert r.status_code == 200 and len(r.json()["routed"]) == 2
    assert client.post("/approvals/route", json={"request_id": rq}, headers=H("contrib")).status_code == 403
    items = client.get("/inbox", headers=H("co")).json()["items"]
    assert len(items) == 2
    it = items[0]
    for k in ("approval", "answer", "head_version", "questions", "evidence", "conflicts", "customer_tier", "request_title",
              "request_due_date"):
        assert k in it, k
    assert it["answer"]["version"] == it["head_version"] and it["approval"]["risk_level"] == "L1"
    assert it["request_due_date"] == "2026-12-15" and it["evidence"][0]["extracted_value"] == "4820"
    assert client.get("/inbox", headers=H("exec")).json()["items"] == []  # L1 仅 compliance_owner
    aid = it["approval"]["id"]
    r = client.post("/approvals", json={"approval_id": aid, "decision": "approved", "comment": "ok"}, headers=H("co"))
    assert r.status_code == 200, r.text
    assert client.get(f"/graph/objects/{it['answer']['id']}", headers=H("co")).json()["status"] == "approved"
    r = client.post("/approvals", json={"approval_id": aid, "decision": "approved"}, headers=H("co"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "GRAPH_ILLEGAL_TRANSITION"


def test_batch_stage_undo_commit_over_http(env):
    client, w, clock = env
    rq, refs = _answers(w, 3)
    client.post("/approvals/route", json={"request_id": rq}, headers=H("wf"))
    ids = [i["approval"]["id"] for i in client.get("/inbox", headers=H("co")).json()["items"]]
    chk = client.post("/approvals/batch-check", json={"approval_ids": ids}, headers=H("co")).json()
    assert chk["ok"] and chk["summary"]["levels"] == {"L1": 3}
    bid = "apb_01JDBATCH0000000000000000A"
    r = client.post("/approvals", json={"batch_id": bid, "approval_ids": ids, "decision": "approved"}, headers=H("co"))
    assert r.status_code == 202, r.text
    b = r.json()["batch"]
    assert b["batch_id"] == bid and b["status"] == "staged" and b["undo_until"] == "2026-11-03T09:10:00Z"
    staged = [i for i in client.get("/inbox", headers=H("co")).json()["items"] if i.get("ui_staged_batch") == bid]
    assert len(staged) == 3
    r = client.post("/approvals/undo", json={"batch_id": bid, "approval_ids": [ids[0]]}, headers=H("co"))
    assert r.status_code == 200 and len(r.json()["batch"]["approval_ids"]) == 2
    clock.t += dt.timedelta(minutes=10)
    b = client.get(f"/approvals/batches/{bid}", headers=H("co")).json()["batch"]
    assert b["status"] == "committed" and len(b["committed_refs"]) == 4
    assert client.post("/approvals/undo", json={"batch_id": bid}, headers=H("co")).status_code == 409
    left = client.get("/inbox", headers=H("co")).json()["items"]
    assert [i["approval"]["id"] for i in left] == [ids[0]]


def test_batch_rejected_with_reasons(env, store):
    client, w, _ = env
    rq = w.request(CP_A)
    qq = w.question(rq)
    s1 = w.answer(S1, "1820", "tCO2e", evidence=[w.evidence("1820", "tCO2e", "a.s1")], questions=[qq])
    client.post("/approvals/route", json={"answer_refs": [s1]}, headers=H("wf"))
    aid = client.get("/inbox", headers=H("co")).json()["items"][0]["approval"]["id"]
    r = client.post("/approvals", json={"approval_ids": [aid], "decision": "approved"}, headers=H("co"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "GRAPH_APPROVAL_REQUIRED" and "L3" in err["details"]["check"]["items"][0]["reasons"][0]
    r = client.post("/approvals", json={"approval_id": aid, "decision": "approved"}, headers=H("co"))
    assert r.status_code == 422 and r.json()["error"]["details"]["missing"] == ["step_up_auth", "clause_read_ack"]
    r = client.post("/approvals", json={"approval_id": aid, "decision": "approved",
                                        "confirmations": {"step_up_auth": True, "clause_read_ack": True}}, headers=H("exec"))
    assert r.status_code == 200, r.text


def test_conflicts_list_and_resolve(env, store):
    client, w, _ = env
    ra, rb = w.request(CP_A), w.request(CP_B)
    qa, qb = w.question(ra), w.question(rb)
    a = w.answer(S1, "1820", "tCO2e", evidence=[w.evidence("1820", "tCO2e", "x.a")], questions=[qa], cs=[CP_A],
                 status="submitted")
    b = w.answer(S1, "1790", "tCO2e", evidence=[w.evidence("1790", "tCO2e", "x.b")], questions=[qb], cs=[CP_B])
    r = client.post("/consistency/run", json={"trigger": "answer_write", "answer_refs": [b]}, headers=H("co"))
    assert r.status_code == 200 and r.json()["conflicts_written"]
    items = client.get("/conflicts", headers=H("co")).json()["items"]
    v = next(i for i in items if i["conflict"]["rule_id"] == "CR-001")
    assert {s["customer_name"] for s in v["sides"]} == {"OEM-A AG", "OEM-B SE"}
    assert all(s["evidence_count"] == 1 for s in v["sides"]) and len(v["questions"]) == 2
    cid = v["conflict"]["id"]
    r = client.post(f"/conflicts/{cid}/resolve", json={"kind": "dismissed", "note": "x"}, headers=H("exec"))
    assert r.status_code == 403
    div = {"reason_code": "customer_methodology", "customer_requirement_refs": [qa, qb], "include_in_export": True,
           "review_by": "2026-12-31", "disclosure_text": {"de": "Unterschiedliche Kundenvorgaben."},
           "sides": [{"answer": a, "customer_scope": [CP_A], "basis": "OEM-A Leitfaden"},
                     {"answer": b, "customer_scope": [CP_B], "basis": "OEM-B Leitfaden"}]}
    r = client.post(f"/conflicts/{cid}/resolve", json={"kind": "accepted_divergence", "note": "Kundenvorgaben unterschiedlich",
                                                       "divergence": div}, headers=H("co"))
    assert r.status_code == 200, r.text
    assert client.get("/conflicts?status=accepted_divergence", headers=H("co")).json()["items"][0]["conflict"]["id"] == cid
    assert not [i for i in client.get("/conflicts", headers=H("co")).json()["items"] if i["conflict"]["id"] == cid]


def test_closed_questions(env, store):
    client, w, _ = env
    rq = w.request(CP_A)

    def cand(value="4820"):
        e = {"id": new_id("Evidence"), "source_type": "document", "fact_key": "activity.electricity.purchased",
             "extracted_value": value, "value_type": "number", "unit": "MWh", "boundary": "organization",
             "period": {"start": "2025-01-01", "end": "2025-12-31"},
             "locator": {"blob_sha256": BLOB, "page": 1}, "valid_from": "2025-01-01", "confidence": 0.7, "status": "candidate"}
        return store.write("Evidence", e, actor=MINER)

    def cq(target, kind, **kw):
        body = {"target_ref": target, "request_id": rq, "kind": kind, "prompt": {"de": "Bitte bestätigen"},
                "assignee_role": "compliance_owner", "expires_at": "2026-12-01T00:00:00Z", "status": "open", **kw}
        return store.write("ClosedQuestion", body, actor=MINER).split("#")[0]

    e1 = cand()
    c1 = cq(e1, "yes_no")
    e2 = cand("4790")
    c2 = cq(e2, "numeric_confirm", proposed_value={"value": "4790", "unit": "MWh", "evidence_ref": e2})
    c3 = cq(e1, "file_request")
    items = client.get("/closed-questions", headers=H("co")).json()["items"]
    assert {c1, c2, c3} <= {i["id"] for i in items}
    assert client.get("/closed-questions", headers=H("contrib")).json()["items"] == []  # 未指派给 contributor
    r = client.post(f"/closed-questions/{c1}/answer", json={"bool": True}, headers=H("co"))
    assert r.status_code == 200, r.text
    assert store.get(e1.split("#")[0])["status"] == "verified"
    r = client.post(f"/closed-questions/{c2}/answer", json={"corrected_value": "4820"}, headers=H("co"))
    assert r.status_code == 200, r.text
    new_ev = store.get(r.json()["created"]["evidence"])
    assert new_ev["source_type"] == "human_confirmation" and new_ev["extracted_value"] == "4820"
    assert store.get(e2.split("#")[0])["status"] == "superseded"
    assert store.get(c2)["answer"]["corrected_value"] == "4820"
    r = client.post(f"/closed-questions/{c3}/answer", json={"blob_sha256": ["sha256:" + "e" * 64]}, headers=H("co"))
    assert r.status_code == 404
    up = client.post("/blobs", content=b"%PDF-1.4 cert", headers={**H("co"), "Content-Type": "application/pdf"}).json()
    r = client.post(f"/closed-questions/{c3}/answer", json={"blob_sha256": [up["sha256"]]}, headers=H("co"))
    assert r.status_code == 200 and store.get(c3)["answer"]["blob_sha256"] == [up["sha256"]]
    assert client.post(f"/closed-questions/{c3}/answer", json={"bool": False}, headers=H("co")).status_code == 409


def test_expiry_endpoint_and_gate(env, store):
    client, w, _ = env
    rq = w.request(CP_A)
    qq = w.question(rq)
    e = w.evidence("4820", "MWh", valid_until="2026-11-25")
    w.answer(ELEC, "4820", "MWh", evidence=[e], questions=[qq])
    items = client.get("/expiry?today=2026-11-03", headers=H("co")).json()["items"]
    it = next(i for i in items if i["subject_ref"] == e)
    assert it["tier"] == "d30" and it["level"] == "warn" and "sales" in it["notify_roles"]
    g = client.post(f"/requests/{rq}/gate", json={"submit_date": "2026-11-03"}, headers=H("wf")).json()
    assert g["gate"] == "pass" and not g["gate_blocks"]
    g2 = client.post(f"/requests/{rq}/gate", json={"submit_date": "2026-11-26"}, headers=H("wf")).json()
    assert g2["gate"] == "block" and g2["gate_blocks"]


def test_supplier_first_send_over_http(env, store):
    client, w, _ = env
    sup = w.counterparty(name="Stahl GmbH", role="supplier")
    s = store.write("SupplierRequest", {"counterparty_id": sup, "datapoint_ids": ["dp:prod.cbam.see_direct@1.0.0"],
                                        "period": {"start": "2025-01-01", "end": "2025-12-31"}, "language": "de",
                                        "template_id": "stpl:cbam_see_request.de@1.0.0", "channel": "email",
                                        "due_date": "2026-12-01", "reminder_count": 0, "status": "draft"},
                    actor=CHASER).split("#")[0]
    body = {"subject_id": s, "kind": "supplier_request_first_send", "decision": "approved"}
    assert client.post("/approvals", json=body, headers=H("contrib")).status_code == 422
    r = client.post("/approvals", json=body, headers=H("co"))
    assert r.status_code == 200 and r.json()["risk_level"] == "L1"
    apr = store.get(r.json()["approval_ref"].split("#")[0])
    assert apr["status"] == "approved" and apr["subject_ref"] == f"{s}#v1"


def test_legal_flag_cancel_endpoint(env, store):
    client, w, _ = env
    rq = w.request(CP_A)
    qq = w.question(rq, 1, "Der Lieferant sichert zu …", tags=["legal_commitment", "legal:LC-01"])
    r = client.post(f"/questions/{qq}/legal-flag/cancel", json={"reason": "kurz"}, headers=H("co"))
    assert r.status_code == 400
    r = client.post(f"/questions/{qq}/legal-flag/cancel", json={"reason": "Nur Informationsabfrage, keine Zusicherung"},
                    headers=H("co"))
    assert r.status_code == 200 and r.json()["types"] == ["LC-01"]

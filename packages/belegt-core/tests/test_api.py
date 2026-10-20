"""Graph API 端到端（FastAPI TestClient）：03 §6.8 端点、错误响应、令牌认证、租户隔离。"""
from __future__ import annotations

import io
import zipfile
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from belegt_core.api import create_app
from belegt_core.api.auth import StaticTokenTable
from belegt_core.auditpack import verify_audit_pack

from helpers import CP, FIX, ORG, ORG_B
from test_rules import answer_body

ANS = FIX["answer"]["id"]


@pytest.fixture
def client(store, chain):
    t = StaticTokenTable()
    t.add("tok-calc", "agent:calc-service", ORG)
    t.add("tok-router", "agent:approval-router", ORG)
    t.add("tok-co", "user:u_compliance01", ORG, ["compliance_owner"])
    t.add("tok-auditor", "user:u_auditor", ORG, ["auditor_readonly"])
    t.add("tok-sales", "user:u_sales01", ORG, ["sales"], cp_scope=[CP])
    t.add("tok-b-admin", "user:u_b_admin", ORG_B, ["org_admin"])
    t.add("tok-b-calc", "agent:calc-service", ORG_B)
    return TestClient(create_app(store, t))


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def test_auth_required(client):
    r = client.get(f"/graph/objects/{ANS}")
    assert r.status_code == 401 and r.json()["error"]["code"] == "GRAPH_UNAUTHENTICATED"
    assert client.get(f"/graph/objects/{ANS}", headers=H("nope")).status_code == 401


def test_post_and_get_object(client):
    b = answer_body()
    r = client.post("/graph/objects", json={"type": "Answer", "object": b}, headers=H("tok-calc"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["object_ref"] == f"{b['id']}#v1" and body["content_hash"].startswith("sha256:")
    g = client.get(f"/graph/objects/{b['id']}", headers=H("tok-co"))
    assert g.status_code == 200 and g.json()["created_by"] == "agent:calc-service"
    # merge_from_head + expected_version
    r = client.post("/graph/objects", json={"type": "Answer", "expected_version": 1, "patch_mode": "merge_from_head",
                                            "object": {"id": b["id"], "status": "in_review", "risk_level": "L2"}},
                    headers=H("tok-router"))
    assert r.status_code == 201 and r.json()["object_ref"].endswith("#v2")
    assert client.get(f"/graph/objects/{b['id']}?version=1", headers=H("tok-co")).json()["status"] == "draft"
    hist = client.get(f"/graph/objects/{b['id']}/history", headers=H("tok-co")).json()["versions"]
    assert len(hist) == 2


@pytest.mark.parametrize("mut,status,code", [
    (dict(evidence_refs=[], calculation_ref=None), 422, "GRAPH_NUM_UNREFERENCED"),
    (dict(value="9999"), 422, "GRAPH_NUM_VALUE_MISMATCH"),
    (dict(unit="MWh"), 400, "GRAPH_UNIT_INCOMPATIBLE"),
    (dict(origin="invented"), 400, "GRAPH_SCHEMA_INVALID"),
    (dict(period={"start": "2024-01-01", "end": "2025-12-31"}), 422, "GRAPH_PERIOD_UNCOVERED"),
])
def test_error_responses(client, mut, status, code):
    r = client.post("/graph/objects", json={"type": "Answer", "object": answer_body(**mut)}, headers=H("tok-calc"))
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code and set(err) == {"code", "message", "rule_id", "path", "details"}


def test_version_conflict_and_idempotency(client):
    r = client.post("/graph/objects", json={"type": "Answer", "expected_version": 1, "patch_mode": "merge_from_head",
                                            "object": {"id": ANS, "status": "submitted"}}, headers=H("tok-calc"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "GRAPH_VERSION_CONFLICT"
    b = answer_body()
    req = {"type": "Answer", "object": b, "idempotency_key": "wf:req_1:act:draft:1"}
    assert client.post("/graph/objects", json=req, headers=H("tok-calc")).status_code == 201
    again = client.post("/graph/objects", json=req, headers=H("tok-calc"))
    assert again.status_code == 201 and again.json()["object_ref"] == f"{b['id']}#v1"
    req["object"] = dict(b, value="2027200", unit="kgCO2e")
    r = client.post("/graph/objects", json=req, headers=H("tok-calc"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "GRAPH_IDEMPOTENCY_MISMATCH"


def test_batch_and_validate(client):
    a1, a2 = answer_body(), answer_body()
    r = client.post("/graph/objects:batch", json={"items": [{"type": "Answer", "object": a1},
                                                            {"type": "Answer", "object": a2}]}, headers=H("tok-calc"))
    assert r.status_code == 201 and len(r.json()["results"]) == 2
    a3 = answer_body()
    r = client.post("/graph/validate", json={"type": "Answer", "object": a3}, headers=H("tok-calc"))
    assert r.status_code == 200 and r.json()["valid"]
    assert client.get(f"/graph/objects/{a3['id']}", headers=H("tok-co")).status_code == 404
    r = client.post("/graph/validate", json={"type": "Answer", "object": answer_body(value="1")}, headers=H("tok-calc"))
    assert r.status_code == 422


def test_as_of_query(client, store):
    v2 = store.history(ANS)[1]
    r = client.get(f"/graph/objects/{ANS}", params={"as_of_tx": v2["created_at"]}, headers=H("tok-co"))
    assert r.json()["status"] == "in_review"
    r = client.get(f"/graph/objects/{ANS}", params={"as_of_valid": "2026-06-30"}, headers=H("tok-co"))
    assert r.status_code == 404
    r = client.get("/graph/answers", params={"datapoint_id": "dp:env.ghg.scope2_market_based@1.1.0",
                                             "as_of_valid": "2025-12-31"}, headers=H("tok-co"))
    assert [a["id"] for a in r.json()["items"]] == [ANS]


def test_lineage_impact_replay(client, chain):
    r = client.get(f"/graph/lineage/{quote(chain['answer'])}", headers=H("tok-co"))
    assert r.status_code == 200 and chain["calculation"] in r.json()["nodes"]
    r = client.get(f"/graph/impact/{quote(chain['evidence_bill'])}", headers=H("tok-co"))
    assert chain["answer"] in r.json()["answers"]
    r = client.post("/graph/replay/calculation", json={"calc_ref": chain["calculation"]}, headers=H("tok-co"))
    assert r.status_code == 200 and r.json()["hash_integrity_ok"] and r.json()["status"] in ("ok", "drift")
    r = client.post("/graph/replay/answer", json={"answer_ref": chain["answer"]}, headers=H("tok-co"))
    assert r.json()["hash_integrity_ok"]


def test_audit_pack_endpoint(client):
    req = FIX["request"]["id"]
    r = client.post(f"/requests/{req}/audit-pack", headers=H("tok-auditor"))
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "belegt-auditpack_" in r.headers["content-disposition"]
    assert verify_audit_pack(r.content)["ok"]
    assert zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json")
    r = client.post(f"/requests/{req}/audit-pack", headers=H("tok-sales"))
    assert r.status_code == 403


def test_tenant_isolation(client, chain):
    # 租户 B 看不到租户 A 的对象（RLS 语义：不可见 = 404）
    assert client.get(f"/graph/objects/{ANS}", headers=H("tok-b-admin")).status_code == 404
    assert client.get(f"/graph/lineage/{quote(chain['answer'])}", headers=H("tok-b-admin")).status_code == 404
    assert client.get("/requests", headers=H("tok-b-admin")).json()["items"] == []
    # 租户 B 引用 A 的证据 → GRAPH_REF_CROSS_TENANT
    client.post("/graph/objects", json={"type": "Organization", "object": {"id": ORG_B, "legal_name": "B", "country": "DE"}},
                headers=H("tok-b-admin"))
    r = client.post("/graph/objects", json={"type": "Answer", "object": answer_body()}, headers=H("tok-b-calc"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "GRAPH_REF_CROSS_TENANT"
    # 冒充 org_id 写入
    r = client.post("/graph/objects", json={"type": "Answer", "object": answer_body(org_id=ORG)}, headers=H("tok-b-calc"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "GRAPH_REF_CROSS_TENANT"
    # 对 A 的对象写新版本
    r = client.post("/graph/objects", json={"type": "Answer", "expected_version": 3, "patch_mode": "merge_from_head",
                                            "object": {"id": ANS, "status": "submitted"}}, headers=H("tok-b-calc"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "GRAPH_REF_CROSS_TENANT"
    # 审计包
    assert client.post(f"/requests/{FIX['request']['id']}/audit-pack", headers=H("tok-b-admin")).status_code == 404


def test_customer_scope_filter_via_api(client):
    r = client.get(f"/graph/objects/{ANS}", headers=H("tok-sales"))
    assert r.status_code == 200  # customer_scope=all 且 approved
    b = answer_body(customer_scope=[CP])
    client.post("/graph/objects", json={"type": "Answer", "object": b}, headers=H("tok-calc"))
    assert client.get(f"/graph/objects/{b['id']}", headers=H("tok-sales")).status_code == 404  # draft 不可见


def test_ui_endpoints_and_blobs(client):
    assert len(client.get("/requests", headers=H("tok-co")).json()["items"]) == 1
    rr = client.get(f"/requests/{FIX['request']['id']}", headers=H("tok-co")).json()
    assert rr["request"]["status"] == "parsed" and len(rr["questions"]) == 1
    assert client.get("/approvals?status=approved", headers=H("tok-co")).json()["items"][0]["id"] == FIX["approval"]["id"]
    assert client.get("/closed-questions?status=answered", headers=H("tok-co")).json()["items"]
    assert client.get("/conflicts", headers=H("tok-co")).json()["items"] == []
    assert client.get("/inbox", headers=H("tok-co")).json() == {"closed_questions": [], "approvals": []}
    r = client.post("/blobs", content=b"%PDF-1.7 test", headers={**H("tok-co"), "content-type": "application/pdf"})
    assert r.status_code == 201 and r.json()["sha256"].startswith("sha256:")
    v = client.get("/graph/audit/verify", headers=H("tok-co")).json()
    assert v["ok"]


def test_request_validation_error_shape(client):
    r = client.post("/graph/objects", json={"object": {}}, headers=H("tok-calc"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "GRAPH_SCHEMA_INVALID"

# Spec: 04 §6.3（A2A Agent Card、belegt.subscribe_datapoints、同意按 DataPoint × 客户 × 期限、撤回停止推送、egress.send 审计）
"""A2A：Agent Card、订阅（同意检查、cp_scope）、HMAC 签名 webhook 推送（httpx.MockTransport）、JSON-RPC。离线。"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from belegt_core.a2a.subscriptions import SubscriptionService, grant_allows, sign, verify_signature
from belegt_core.app_factory import Settings, create_full_app
from belegt_core.demo import build_env


class Hook:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        return httpx.Response(self.status)


@pytest.fixture
def w():
    env = build_env()
    ids = env.ids
    env.tokens.add("buyer-b", "user:u_buyer_b", env.org_id, ["customer_buyer"], cp_scope=[ids["other_customer"]])
    env.tokens.add("buyer-a", "user:u_buyer_a", env.org_id, ["customer_buyer"], cp_scope=[ids["customer"]])
    env.tokens.add("buyer-x", "user:u_buyer_x", env.org_id, ["customer_buyer"])
    hook = Hook()
    app = create_full_app(Settings(services_factory="none", public_base_url="https://musterteile.belegt.example"), services=env)
    app.state.a2a.subs.http = httpx.Client(transport=httpx.MockTransport(hook))
    submitted = [a for a in env.store.query("Answer", org_id=env.org_id) if a["status"] == "submitted"]
    return {"env": env, "client": TestClient(app), "hook": hook, "app": app, "submitted": submitted,
            "dps": sorted({a["datapoint_id"].split("@")[0] for a in submitted})}


def _grant(env, cp_id, **grant):
    cp = env.store.get(cp_id, org_id=env.org_id)
    env.store.write("Counterparty", {"id": cp_id, "data_sharing_grant": grant}, expected_version=cp["version"],
                    actor=env.user("owner"), patch_mode="merge_from_head")


B = {"Authorization": "Bearer buyer-b"}
OWNER = {"Authorization": "Bearer demo-owner"}


def test_agent_card(w):
    c = w["client"]
    for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
        r = c.get(path)
        assert r.status_code == 200
        card = r.json()
        assert card["name"] == "Musterteile GmbH – Belegt Supplier Data Agent"
        assert card["url"] == "https://musterteile.belegt.example/a2a"
        assert card["capabilities"]["pushNotifications"] is True
        assert {s["id"] for s in card["skills"]} == {"belegt.subscribe_datapoints", "belegt.request_pcf", "belegt.request_auditpack"}
        assert card["securitySchemes"]["oauth"]["flows"]["clientCredentials"]["scopes"]["belegt.subscribe"]


def test_subscribe_requires_consent_and_scope(w):
    env, c, dps = w["env"], w["client"], w["dps"]
    body = {"datapoint_ids": dps[:2], "webhook_url": "https://buyer.oem-b.example/hooks/belegt"}
    r = c.post("/a2a/subscriptions", headers=B, json=body)
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "consent_required"
    _grant(env, env.ids["other_customer"], datapoints=[dps[0]], valid_until="2099-12-31")
    r = c.post("/a2a/subscriptions", headers=B, json=body)
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"].startswith("datapoints_not_granted:")
    _grant(env, env.ids["other_customer"], datapoints=dps, valid_until="2099-12-31")
    # 客户 A 不能订阅客户 B；无 cp_scope 的 customer_buyer 被拒
    r = c.post("/a2a/subscriptions", headers={"Authorization": "Bearer buyer-a"},
               json={**body, "counterparty_id": env.ids["other_customer"]})
    assert r.status_code == 403
    assert c.post("/a2a/subscriptions", headers={"Authorization": "Bearer buyer-x"}, json=body).status_code == 403
    # 只允许 https webhook
    assert c.post("/a2a/subscriptions", headers=B, json={**body, "webhook_url": "http://buyer.example/h"}).status_code == 400
    r = c.post("/a2a/subscriptions", headers=B, json=body)
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["counterparty_id"] == env.ids["other_customer"] and sub["status"] == "active" and len(sub["secret"]) >= 32
    lst = c.get("/a2a/subscriptions", headers=B).json()["items"]
    assert [s["id"] for s in lst] == [sub["id"]] and "secret" not in lst[0]
    assert c.get("/a2a/subscriptions", headers={"Authorization": "Bearer buyer-a"}).json()["items"] == []
    assert len(c.get("/a2a/subscriptions", headers=OWNER).json()["items"]) == 1


def test_dispatch_signed_webhooks_idempotent_and_revocation(w):
    env, c, hook, dps = w["env"], w["client"], w["hook"], w["dps"]
    _grant(env, env.ids["other_customer"], datapoints=["*"], valid_until="2099-12-31")
    sub = c.post("/a2a/subscriptions", headers=B, json={"datapoint_ids": dps, "webhook_url": "https://buyer.oem-b.example/h",
                                                        "secret": "s3cret-s3cret-s3cret"}).json()
    assert "secret" not in sub  # 客户自带的 secret 不回显
    assert c.post("/a2a/subscriptions/dispatch", headers=B).status_code == 403  # 只能由供应商侧/服务触发
    r = c.post("/a2a/subscriptions/dispatch", headers=OWNER, json={})
    assert r.status_code == 200, r.text
    n = len(w["submitted"])
    assert r.json()["delivered"] == n and len(hook.calls) == n
    req = hook.calls[0]
    body = req.content
    assert verify_signature("s3cret-s3cret-s3cret", body, req.headers["X-Belegt-Timestamp"], req.headers["X-Belegt-Signature"])
    assert not verify_signature("wrong-secret-wrong", body, req.headers["X-Belegt-Timestamp"], req.headers["X-Belegt-Signature"])
    p = json.loads(body)
    assert p["event"] == "answer.approved" and p["subscription_id"] == sub["id"] and p["approval_ref"].startswith("apr_")
    assert set(p) >= {"answer_ref", "value", "unit", "period", "approval_ref", "datapoint_id"}
    # 幂等：第二次 dispatch 不重复推送
    r = c.post("/a2a/subscriptions/dispatch", headers=OWNER)
    assert r.json()["delivered"] == 0 and r.json()["skipped"] == n and len(hook.calls) == n
    # 每次交付写 egress.send 审计
    ev = [e for e in env.store.audit_events(env.org_id) if e["action"] == "egress.send"]
    assert len([e for e in ev if (e.get("details") or {}).get("event") == "a2a.push"]) == n
    d = c.get(f"/a2a/subscriptions/{sub['id']}", headers=B).json()
    assert len(d["deliveries"]) == n and all(x["status"] == "delivered" for x in d["deliveries"])
    # 撤回同意 → 订阅暂停、不再推送
    _grant(env, env.ids["other_customer"], datapoints=["*"], revoked=True)
    r = c.post("/a2a/subscriptions/dispatch", headers=OWNER).json()
    assert r["revoked"] == [sub["id"]] and r["delivered"] == 0
    assert c.get(f"/a2a/subscriptions/{sub['id']}", headers=B).json()["status"] == "suspended"


def test_failed_delivery_retried(w):
    env, c = w["env"], w["client"]
    _grant(env, env.ids["other_customer"], datapoints=["*"])
    hook = Hook(status=503)
    w["app"].state.a2a.subs.http = httpx.Client(transport=httpx.MockTransport(hook))
    sub = c.post("/a2a/subscriptions", headers=B, json={"datapoint_ids": w["dps"][:1], "webhook_url": "https://h.example/x"}).json()
    r = c.post("/a2a/subscriptions/dispatch", headers=OWNER).json()
    assert r["failed"] >= 1 and r["delivered"] == 0
    hook.status = 200
    r = c.post("/a2a/subscriptions/dispatch", headers=OWNER, json={"subscription_id": sub["id"]}).json()
    assert r["delivered"] >= 1 and r["failed"] == 0
    d = c.get(f"/a2a/subscriptions/{sub['id']}", headers=B).json()["deliveries"]
    assert all(x["status"] == "delivered" and x["attempts"] == 2 for x in d)
    assert c.delete(f"/a2a/subscriptions/{sub['id']}", headers=B).json()["status"] == "cancelled"


def test_jsonrpc_message_send(w):
    env, c = w["env"], w["client"]
    _grant(env, env.ids["other_customer"], datapoints=["*"])
    msg = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "m1", "kind": "message",
        "parts": [{"kind": "data", "data": {"skill": "belegt.subscribe_datapoints", "datapoint_ids": w["dps"][:1],
                                            "webhook_url": "https://buyer.oem-b.example/a2a"}}]}}}
    r = c.post("/a2a", headers=B, json=msg).json()
    task = r["result"]
    assert task["status"]["state"] == "completed"
    art = task["artifacts"][0]["parts"][0]["data"]
    assert art["subscription_id"].startswith("a2s_") and art["secret"]
    got = c.post("/a2a", headers=B, json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}}).json()
    assert got["result"]["id"] == task["id"] and "secret" not in got["result"]["artifacts"][0]["parts"][0]["data"]
    assert "error" in c.post("/a2a", headers={"Authorization": "Bearer buyer-a"},
                             json={"jsonrpc": "2.0", "id": 3, "method": "tasks/get", "params": {"id": task["id"]}}).json()
    msg["params"]["message"]["parts"][0]["data"]["skill"] = "belegt.request_pcf"
    assert c.post("/a2a", headers=B, json=msg).json()["result"]["status"]["state"] == "failed"
    assert c.post("/a2a", headers=B, json={"jsonrpc": "2.0", "id": 4, "method": "nope"}).json()["error"]["code"] == -32601
    assert c.post("/a2a", json=msg).status_code == 401


def test_grant_and_signature_helpers():
    assert grant_allows(None, ["dp:a"], today="2026-01-01") == (False, "consent_required")
    assert grant_allows({"datapoints": ["dp:a"], "valid_until": "2025-12-31"}, ["dp:a"], today="2026-01-01")[1] == "consent_expired"
    assert grant_allows({"datapoints": ["dp:a"]}, ["dp:a@1.0.0"], today="2026-01-01") == (True, "ok")
    ts = int(time.time())
    s = sign("k" * 20, b"{}", ts)
    assert s.startswith("sha256=") and verify_signature("k" * 20, b"{}", ts, s)
    assert not verify_signature("k" * 20, b"{}", ts - 3600, sign("k" * 20, b"{}", ts - 3600))  # 过期防重放
    assert not verify_signature("k" * 20, b"{ }", ts, s)


def test_service_allows_http_when_configured():
    env = build_env()
    svc = SubscriptionService(env.store, allow_http=True, allowed_hosts=["localhost"])
    _grant(env, env.ids["other_customer"], datapoints=["*"])
    sub = svc.subscribe(env.user("owner"), datapoint_ids=["dp:x"], webhook_url="http://localhost:9000/h",
                        counterparty_id=env.ids["other_customer"])
    assert sub["status"] == "active"
    from belegt_core.errors import GraphError

    with pytest.raises(GraphError):
        svc.subscribe(env.user("owner"), datapoint_ids=["dp:x"], webhook_url="http://evil.example/h",
                      counterparty_id=env.ids["other_customer"])

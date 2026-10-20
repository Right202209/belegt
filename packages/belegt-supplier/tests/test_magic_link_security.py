# Spec: 08 §7.1–§7.2（token、时效、范围、设备绑定、吊销、补发、速率限制）、07 RT-PI-14 / T-05 / T-06
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from belegt_supplier import tokens
from belegt_supplier.web.app import create_app

from conftest import make_manifest

API = "/supplier-api/v1"


@pytest.fixture
def client(svc, settings):
    return TestClient(create_app(settings, service=svc), base_url="https://testserver")


def test_token_format_and_hash_only_storage(svc, issued):
    m, token = issued
    assert token.startswith("slk_") and len(token) > 60
    sreq, _, _ = svc.get_request(m.sreq_id)
    assert sreq.portal_token_hash == tokens.token_hash(token)
    import sqlite3  # noqa: F401 —— 确认明文不在任何表中
    with svc.engine.connect() as c:
        dump = "\n".join(str(r) for t in ("supplier_requests", "magic_links", "audit_events", "webhook_outbox")
                         for r in c.exec_driver_sql(f"SELECT * FROM {t}").fetchall())
    assert token not in dump


def test_valid_token_opens(client, issued):
    _, token = issued
    r = client.get(f"{API}/requests/{token}")
    assert r.status_code == 200 and r.json()["customer_name"] == "Müller Stanztechnik GmbH"
    assert r.headers["x-robots-tag"].startswith("noindex") and "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),          # 篡改签名
    lambda t: t[:10] + ("x" if t[10] != "x" else "y") + t[11:],  # 篡改正文
    lambda t: "slk_" + "A" * 48 + ".AAAAAAAAAAAAAAAAAAAAAA",     # 伪造
    lambda t: t.replace("slk_", "tok_"),
])
def test_tampered_token_404(client, issued, mutate):
    _, token = issued
    r = client.get(f"{API}/requests/{mutate(token)}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "LINK_INVALID"


def test_validly_signed_but_unknown_token_404(client, svc, issued, clock):
    other = tokens.derive(svc.settings.instance_secret, "sreq_01JD8Y3K5M2N4P6Q8R0S2T4V6Z", 1, clock() + timedelta(days=10))
    assert client.get(f"{API}/requests/{other.token}").status_code == 404


def test_token_bound_to_single_request(client, svc, issued):
    m, token = issued
    m2 = make_manifest(supplier_name="Other Supplier")
    res2 = svc.ingest_manifest(m2, status="sent", approval_ref="apr_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1")
    token2 = res2["portal_link"].rsplit("/", 1)[1]
    a = client.get(f"{API}/requests/{token}").json()
    b = TestClient(client.app, base_url="https://testserver").get(f"{API}/requests/{token2}").json()
    assert a["sreq_ref"] == m.sreq_ref and b["sreq_ref"] == m2.sreq_ref and a["sreq_ref"] != b["sreq_ref"]
    assert "Other Supplier" not in str(a)


def test_expired_token_410_and_reissue(client, svc, issued, clock, transport):
    m, token = issued
    clock.advance(days=40)  # due 2026-11-23 + 14 天之后
    r = client.get(f"{API}/requests/{token}")
    assert r.status_code == 410 and r.json()["error"]["code"] == "LINK_EXPIRED"
    # 补发：邮箱必须是原收件人
    clock.now = clock.now - timedelta(days=25)  # 回到请求仍开放的时间点
    assert client.post(f"{API}/auth/magic-link", json={"email": "evil@x.example", "token": token}).status_code == 202
    assert len(transport.sent) == 0
    assert client.post(f"{API}/auth/magic-link", json={"email": m.contact_email.upper(), "token": token}).status_code == 202
    assert len(transport.sent) == 1
    body = transport.sent[-1].get_content()
    new_token = body.split("/r/")[1].split()[0]
    assert new_token != token
    # 旧 token 立即失效（重放）
    assert TestClient(client.app, base_url="https://testserver").get(f"{API}/requests/{token}").status_code == 404
    assert TestClient(client.app, base_url="https://testserver").get(f"{API}/requests/{new_token}").status_code == 200


def test_magic_link_rate_limit(client, issued):
    m, token = issued
    for _ in range(3):
        client.post(f"{API}/auth/magic-link", json={"email": "nobody@x.example", "token": token})
    r = client.post(f"{API}/auth/magic-link", json={"email": "nobody@x.example", "token": token})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_revoked_token(client, svc, issued):
    m, token = issued
    svc.revoke_links(m.sreq_id)
    assert client.get(f"{API}/requests/{token}").status_code == 404


def test_unknown_token_enumeration_rate_limit_with_backoff(client):
    codes = [client.get(f"{API}/requests/slk_guess{i}.x").status_code for i in range(12)]
    assert codes[:10] == [404] * 10 and codes[10] == 429
    r = client.get(f"{API}/requests/slk_guess99.x")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_token_rate_limit_60_per_minute(client, issued):
    _, token = issued
    for _ in range(60):
        assert client.get(f"{API}/requests/{token}").status_code == 200
    assert client.get(f"{API}/requests/{token}").status_code == 429


def test_new_device_requires_otp_and_lockout(client, svc, issued, transport):
    _, token = issued
    assert client.get(f"{API}/requests/{token}").status_code == 200   # 首次打开 → 绑定设备
    other = TestClient(client.app, base_url="https://testserver")
    r = other.get(f"{API}/requests/{token}")
    assert r.status_code == 401 and r.json()["error"]["code"] == "OTP_REQUIRED"
    assert other.post(f"{API}/requests/{token}/otp").status_code == 200
    code = transport.sent[-1].get_content().split(": ")[1][:6]
    assert transport.sent[-1]["To"] == "ayse@anadolu-celik.example"   # 只发原收件人
    for _ in range(4):
        assert other.post(f"{API}/requests/{token}/otp/verify", json={"code": "000000" if code != "000000" else "111111"}).status_code == 401
    r = other.post(f"{API}/requests/{token}/otp/verify", json={"code": "999999" if code != "999999" else "888888"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "OTP_LOCKED"
    # 锁定期内正确码也不行
    assert other.post(f"{API}/requests/{token}/otp/verify", json={"code": code}).status_code in (400, 429)


def test_otp_success_binds_new_device(client, issued, transport):
    _, token = issued
    client.get(f"{API}/requests/{token}")
    other = TestClient(client.app, base_url="https://testserver")
    other.post(f"{API}/requests/{token}/otp")
    code = transport.sent[-1].get_content().split(": ")[1][:6]
    assert other.post(f"{API}/requests/{token}/otp/verify", json={"code": code}).status_code == 200
    assert other.get(f"{API}/requests/{token}").status_code == 200


def test_closed_request_read_only_then_invalid(client, svc, issued, clock):
    m, token = issued
    client.get(f"{API}/requests/{token}")
    for st in ("responded", "reviewed", "closed"):
        svc.apply_core_event("request.status", {"sreq_id": m.sreq_id, "status": st})
    r = client.get(f"{API}/requests/{token}")
    assert r.status_code == 200 and r.json()["readonly"] is True
    assert client.put(f"{API}/requests/{token}/draft", json={"values": {}}).status_code == 410
    clock.advance(days=8)
    assert client.get(f"{API}/requests/{token}").status_code == 410


def test_draft_request_not_accessible(client, svc):
    m = make_manifest()
    svc.create_draft(m)
    link = svc.current_link(m.sreq_id)
    assert client.get(f"{API}/requests/{link['token']}").status_code == 404


def test_many_origins_freeze_link(svc, issued, settings):
    _, token = issued
    for i in range(9):
        svc.resolve(token, ip=f"10.0.0.{i}", check_device=False)
    with pytest.raises(Exception):
        svc.resolve(token, ip="10.0.0.99", check_device=False)
    with pytest.raises(Exception):
        svc.resolve(token, ip="10.0.0.1", check_device=False)


def test_manifest_requires_approval_ref_for_non_draft(svc):
    from belegt_supplier.service import PortalError

    with pytest.raises(PortalError) as e:
        svc.ingest_manifest(make_manifest(), status="sent")
    assert e.value.code == "GRAPH_APPROVAL_REQUIRED"


def test_expiry_default_due_plus_14_capped_90(clock):
    from datetime import date
    exp = tokens.default_expiry(clock(), date(2026, 11, 23))
    assert exp.date() == date(2026, 12, 7)
    assert tokens.default_expiry(clock(), date(2027, 12, 31)) == clock() + timedelta(days=90)

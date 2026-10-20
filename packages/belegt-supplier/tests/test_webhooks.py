# Spec: 08 §6.5–§6.6（签名请求清单 JWS、提交包签名、Webhook HMAC）
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from belegt_supplier.core_client import HttpCoreClient
from belegt_supplier.signing import (
    WEBHOOK_HEADER, SignatureError, canonical_json, jws_sign, jws_sign_detached, jws_verify, jws_verify_detached,
    verify_webhook, webhook_signature,
)
from belegt_supplier.web.app import create_app

SECRET = b"w" * 32


def test_webhook_roundtrip():
    body = canonical_json({"event": "x", "payload": {"a": "1"}})
    h = webhook_signature(SECRET, body, timestamp=1_700_000_000)
    assert h.startswith("t=1700000000,v1=")
    assert verify_webhook(SECRET, body, h, now=1_700_000_100) == 1_700_000_000


@pytest.mark.parametrize("case", ["tampered_body", "wrong_secret", "expired", "missing", "malformed", "replay_old"])
def test_webhook_rejections(case):
    body = b'{"a":"1"}'
    now = time.time()
    h = webhook_signature(SECRET, body, timestamp=int(now))
    with pytest.raises(SignatureError) as e:
        if case == "tampered_body":
            verify_webhook(SECRET, b'{"a":"2"}', h, now=now)
        elif case == "wrong_secret":
            verify_webhook(b"x" * 32, body, h, now=now)
        elif case == "expired":
            verify_webhook(SECRET, body, h, now=now + 301)
        elif case == "missing":
            verify_webhook(SECRET, body, None)
        elif case == "malformed":
            verify_webhook(SECRET, body, "v1=abc")
        else:
            old = webhook_signature(SECRET, body, timestamp=int(now) - 3600)
            verify_webhook(SECRET, body, old, now=now)
    assert e.value.code.startswith("SIG_")


def test_multiple_v1_signatures_for_rotation():
    body = b"{}"
    t = int(time.time())
    good = webhook_signature(SECRET, body, t).split("v1=")[1]
    assert verify_webhook(SECRET, body, f"t={t},v1=deadbeef,v1={good}") == t


def test_jws_compact_and_detached():
    payload = {"sreq_ref": "sreq_X#v1", "n": "1.85"}
    tok = jws_sign(payload, SECRET, kid="tenant-1")
    assert jws_verify(tok, SECRET) == payload
    with pytest.raises(SignatureError):
        jws_verify(tok, b"other")
    h, p, s = tok.split(".")
    with pytest.raises(SignatureError):
        jws_verify(f"{h}.{p}x.{s}", SECRET)
    det = jws_sign_detached(payload, SECRET)
    assert ".." in det
    jws_verify_detached(det, payload, SECRET)
    with pytest.raises(SignatureError):
        jws_verify_detached(det, {**payload, "n": "1.86"}, SECRET)


def test_jws_alg_none_rejected():
    import base64
    h = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(b'{}').rstrip(b"=").decode()
    with pytest.raises(SignatureError) as e:
        jws_verify(f"{h}.{p}.", SECRET)
    assert e.value.code == "SIG_ALG_UNSUPPORTED"


def test_core_to_supplier_webhook_endpoint(svc, settings, issued):
    m, _ = issued
    client = TestClient(create_app(settings, service=svc), base_url="https://testserver")
    body = canonical_json({"event": "request.status", "payload": {"sreq_id": m.sreq_id, "status": "responded"}})
    r = client.post("/supplier-api/v1/webhooks/core", content=body, headers={WEBHOOK_HEADER: "t=1,v1=00"})
    assert r.status_code == 401
    r = client.post("/supplier-api/v1/webhooks/core", content=body, headers={WEBHOOK_HEADER: webhook_signature(settings.webhook_secret, body)})
    assert r.status_code == 200 and r.json()["status"] == "responded"
    bad = canonical_json({"event": "request.status", "payload": {"sreq_id": m.sreq_id, "status": "draft"}})
    r = client.post("/supplier-api/v1/webhooks/core", content=bad, headers={WEBHOOK_HEADER: webhook_signature(settings.webhook_secret, bad)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "GRAPH_STATE_INVALID"


def test_http_core_client_signs_requests(monkeypatch):
    captured = {}

    class Resp:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["sig"] = req.get_header("Belegt-signature")
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c = HttpCoreClient("https://core.example/", SECRET)
    c.push_submission({"format": "belegt-supplier-submission/1", "signature": "abc"})
    assert captured["url"] == "https://core.example/supplier-inbound/v1/submissions"
    verify_webhook(SECRET, captured["body"], captured["sig"])
    c.push_question({"text": "?"})
    assert captured["url"].endswith("/supplier-inbound/v1/questions")
    assert json.loads(captured["body"]) == {"text": "?"}

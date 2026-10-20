# Spec: 08 §6.5–§6.6（supplier-api v1 与提交包 belegt-supplier-submission/1）、§6.2–§6.4（门户）、FR-301/FR-703
from __future__ import annotations

import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from belegt_supplier import io_bridge
from belegt_supplier.signing import canonical_json, jws_sign, sha256_hex
from belegt_supplier.web.app import create_app

from conftest import make_manifest, signed_manifest

API = "/supplier-api/v1"
PID = "prod_01JD8Y3K5M2N4P6Q8R0S2T4V6W"
K_SD = f"dp:prod.cbam.see_direct|{PID}"
K_SI = f"dp:prod.cbam.see_indirect|{PID}"
K_CN = f"dp:prod.identity.cn_code|{PID}"
K_RT = f"dp:prod.cbam.production_route|{PID}"
K_IN = f"dp:prod.cbam.installation_id|{PID}"


@pytest.fixture
def client(svc, settings):
    return TestClient(create_app(settings, service=svc), base_url="https://testserver")


@pytest.fixture
def token(client, manifest):
    r = client.post(f"{API}/manifests", json={"manifest_jws": signed_manifest(manifest), "status": "sent",
                                              "approval_ref": "apr_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1"})
    assert r.status_code == 201, r.text
    assert r.json()["portal_link"].startswith("https://supplier.example.org/r/slk_")
    return r.json()["portal_link"].rsplit("/", 1)[1]


def test_manifest_signature_required(client, manifest):
    bad = jws_sign(manifest.model_dump(mode="json"), b"wrong-secret")
    r = client.post(f"{API}/manifests", json={"manifest_jws": bad, "approval_ref": "apr_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "MANIFEST_SIGNATURE_INVALID"


def test_manifest_repush_returns_same_link(client, manifest, token):
    r = client.post(f"{API}/manifests", json={"manifest_jws": signed_manifest(manifest), "approval_ref": "apr_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1"})
    assert r.json()["portal_link"].endswith(token)  # 派生 token：可重复渲染而不存明文


def test_get_request_view(client, token):
    d = client.get(f"{API}/requests/{token}").json()
    assert d["locale"] == "tr" and d["due_date"] == "2026-11-23" and d["draft_version"] == 0
    assert K_SD in d["fields"] and len(d["fields"]) == 5
    sd = next(x for x in d["datapoints"] if x["datapoint_id"] == "dp:prod.cbam.see_direct")
    assert sd["allowed_units"] == ["tCO2e/t", "kgCO2e/t"]


def test_full_submission_flow(client, svc, core, token, manifest):
    # 1) 草稿（逗号小数、单位、提示）
    r = client.put(f"{API}/requests/{token}/draft", json={"draft_version": 0, "values": {
        K_SD: {"value": "1,85", "unit": "tCO2e/t"}, K_SI: {"value": "210", "unit": "kgCO2e/t"},
        K_CN: {"value": "72083900"}, K_RT: {"value": "bf_bof"}, K_IN: {"value": "TR-INST-000123", "note": "Werk 1"}}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["draft_version"] == 1 and d["issues"] == []
    assert isinstance(d["hints"], list) and not any(h["rule_id"] == "PLAUS-004" for h in d["hints"])  # bf_bof 且 1.85 ≥ 1.2
    # 乐观锁
    r = client.put(f"{API}/requests/{token}/draft", json={"draft_version": 0, "values": {}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DRAFT_CONFLICT"
    # 2) 上传核查报告
    pdf = b"%PDF-1.7\n% verification report\n"
    r = client.post(f"{API}/requests/{token}/uploads", files={"file": ("pruefbericht.pdf", pdf, "application/pdf")})
    assert r.status_code == 201
    up = r.json()
    assert up["sha256"] == "sha256:" + sha256_hex(pdf) and up["scan"]["status"] == "accepted"
    # 3) 未勾选声明 → 422
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ATTESTATION_REQUIRED"
    # 4) 提交
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True, "draft_version": 1},
                    headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["package_hash"].startswith("sha256:") and rec["seq"] == 1 and rec["replayed"] is False
    # 客户侧收到提交包（Webhook → CoreClient）
    pkg = core.submissions[-1]
    assert pkg["format"] == "belegt-supplier-submission/1" and pkg["sreq_ref"] == manifest.sreq_ref
    assert "sha256:" + sha256_hex(canonical_json(pkg)) == rec["package_hash"]
    vals = {v["datapoint_id"]: v for v in pkg["values"]}
    assert vals["dp:prod.cbam.see_direct"]["value"] == "1.85" and vals["dp:prod.cbam.see_direct"]["unit"] == "tCO2e/t"
    assert vals["dp:prod.cbam.see_indirect"]["value"] == "210" and vals["dp:prod.cbam.see_indirect"]["unit"] == "kgCO2e/t"
    assert vals["dp:prod.cbam.see_direct"]["boundary"] == f"product:{PID}"
    assert vals["dp:prod.cbam.see_direct"]["period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert vals["dp:prod.cbam.installation_id"]["note"] == "Werk 1"
    assert pkg["blobs"] == [{"sha256": up["sha256"], "mime": "application/pdf", "filename": "pruefbericht.pdf"}]
    assert pkg["attestation"] == {"by": "ayse@anadolu-celik.example", "statement_version": "belegt-supplier-attestation@1.0.0"}
    assert pkg["request_manifest_hash"].startswith("sha256:")
    svc.verify_package(pkg)  # 签名可复核
    tampered = dict(pkg, values=pkg["values"][:-1])
    with pytest.raises(Exception):
        svc.verify_package(tampered)
    # 本地状态 → responded
    sreq, _, _ = svc.get_request(manifest.sreq_id)
    assert sreq.status == "responded"
    # 5) 幂等重放
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True}, headers={"Idempotency-Key": "k-1"})
    assert r.json()["replayed"] is True and r.json()["package_hash"] == rec["package_hash"] and len(core.submissions) == 1
    # 6) 再次提交（新版本）
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True, "values": {K_SD: {"value": "1.86", "unit": "tCO2e/t"}}})
    assert r.status_code == 201 and r.json()["seq"] == 2


def test_submit_validation_errors(client, token):
    client.put(f"{API}/requests/{token}/draft", json={"values": {K_SD: {"value": "abc", "unit": "tCO2e/t"},
                                                                 K_SI: {"value": "1", "unit": "MWh"}}})
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True})
    assert r.status_code == 422
    codes = {d["field"]: d["code"] for d in r.json()["error"]["details"]}
    assert codes == {K_SD: "INVALID_NUMBER", K_SI: "UNIT_NOT_ALLOWED"}


def test_empty_submission_rejected(client, token):
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EMPTY_SUBMISSION"


def test_submit_limit_10(client, token):
    for i in range(10):
        assert client.post(f"{API}/requests/{token}/submit", json={"attestation": True, "values": {K_CN: {"value": "72083900"}}}).status_code == 201
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True})
    assert r.status_code == 429 and r.json()["error"]["code"] == "SUBMIT_LIMIT"


def test_unknown_field_ignored_with_issue(client, token):
    r = client.put(f"{API}/requests/{token}/draft", json={"values": {"dp:other|prod_X": {"value": "1"}}})
    assert r.json()["issues"] == [{"field": "dp:other|prod_X", "code": "UNKNOWN_FIELD"}]


def test_instant_hints_plaus_003(client, token):
    r = client.put(f"{API}/requests/{token}/draft", json={"values": {K_SD: {"value": "1850", "unit": "tCO2e/t"}}})
    h = [x for x in r.json()["hints"] if x["rule_id"] == "PLAUS-003"]
    assert h and h[0]["field"] == K_SD and "1000" in h[0]["message"]  # 土耳其语提示文案


def test_upload_rejections(client, token):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/vbaProject.bin", b"x")
    r = client.post(f"{API}/requests/{token}/uploads", files={"file": ("cbam.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["rule_id"] == "PLAUS-032"
    big = b"%PDF-" + b"0" * (25 * 1024 * 1024)
    r = client.post(f"{API}/requests/{token}/uploads", files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413


def test_upload_daily_limit(client, token):
    for i in range(20):
        assert client.post(f"{API}/requests/{token}/uploads", files={"file": (f"f{i}.txt", b"x%d" % i, "text/plain")}).status_code == 201
    assert client.post(f"{API}/requests/{token}/uploads", files={"file": ("f.txt", b"y", "text/plain")}).status_code == 429


def test_parse_cbam_unavailable_then_custom_parser(client, token):
    xl = io.BytesIO()
    with zipfile.ZipFile(xl, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook><sheets><sheet name='Summary_Products'/></sheets></workbook>")
    up = client.post(f"{API}/requests/{token}/uploads", files={"file": ("c.xlsx", xl.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}).json()
    io_bridge.set_cbam_parser(None)
    import builtins
    real_import = builtins.__import__

    def no_io(name, *a, **k):
        if name == "belegt_io":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    builtins.__import__ = no_io
    try:
        r = client.post(f"{API}/requests/{token}/parse/cbam-communication", json={"sha256": up["sha256"]})
    finally:
        builtins.__import__ = real_import
    assert r.status_code == 501 and r.json()["error"]["code"] == "PARSER_UNAVAILABLE"
    io_bridge.set_cbam_parser(lambda path: [
        {"datapoint_id": "dp:prod.cbam.see_direct", "value": "1.92", "unit": "tCO2e/t", "sheet": "Summary_Products", "cell": "F4", "cn_code": "72083900"}])
    try:
        r = client.post(f"{API}/requests/{token}/parse/cbam-communication", json={"sha256": up["sha256"]})
    finally:
        io_bridge.set_cbam_parser(None)
    d = r.json()
    assert r.status_code == 200 and d["found"][0]["field"] == K_SD and d["found"][0]["source"]["cell"] == "F4"
    assert d["missing_products"] == []


def test_question_and_decline(client, core, token):
    r = client.post(f"{API}/requests/{token}/questions", json={"text": "Welche Systemgrenze?\u202e", "datapoint_id": "dp:prod.cbam.see_direct"})
    assert r.status_code == 201
    q = core.questions[-1]
    assert q["text"] == "Welche Systemgrenze?" and q["datapoint_id"] == "dp:prod.cbam.see_direct"
    assert client.post(f"{API}/requests/{token}/decline", json={"reason": "bogus"}).status_code == 422
    assert client.post(f"{API}/requests/{token}/decline", json={"reason": "confidential", "text": "NDA"}).status_code == 201
    assert core.events[-1][0] == "decline.created" and core.events[-1][1]["reason"] == "confidential"


def test_link_opened_event(client, core, token):
    client.get(f"{API}/requests/{token}")
    assert any(e == "link.opened" for e, _ in core.events)


def test_me_endpoints_not_implemented(client):
    assert client.get(f"{API}/me/submissions").status_code == 501
    assert client.post(f"{API}/me/shares").status_code == 501


def test_webhook_outbox_retries_when_core_down(client, svc, core, token):
    core.fail = True
    r = client.post(f"{API}/requests/{token}/submit", json={"attestation": True, "values": {K_CN: {"value": "72083900"}}})
    assert r.status_code == 201 and svc.pending_webhooks() >= 1
    core.fail = False
    assert svc.deliver_webhooks()["pending"] == 0 and len(core.submissions) == 1


# ---------------------------------------------------------------- 服务端渲染门户
@pytest.mark.parametrize("lang,needle", [("de", "bittet"), ("en", "requests data"), ("tr", "veri talep ediyor"), ("zh", "请求数据")])
def test_landing_page_languages(client, token, lang, needle):
    r = client.get(f"/r/{token}?lang={lang}")
    assert r.status_code == 200 and needle in r.text and f'lang="{lang}"' in r.text
    assert "<script src=\"/static/app.js\"" in r.text and "http" not in re.sub(r"https://(supplier\.example\.org|mueller\.example)[^\"]*", "", r.text.split("<footer>")[0]).replace("http-equiv", "")


def test_language_negotiation_from_header_and_cookie(client, token):
    r = client.get(f"/r/{token}", headers={"accept-language": "zh-CN,zh;q=0.9,en;q=0.5"})
    assert 'lang="zh"' in r.text
    client.get(f"/r/{token}?lang=de")
    assert 'lang="de"' in client.get(f"/r/{token}").text


def test_form_page_dynamic_cards_and_no_js_flow(client, core, token):
    r = client.get(f"/r/{token}/form?lang=de")
    assert r.status_code == 200
    for k in (K_SD, K_SI, K_CN, K_RT, K_IN):
        assert f'name="v__{k}"' in r.text
    assert '<option value="kgCO2e/t"' in r.text and "Direkte spezifische graue Emissionen" in r.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    # CSRF 必需
    assert client.post(f"/r/{token}/form", data={"csrf": "x", f"v__{K_SD}": "1.9"}).status_code == 403
    r = client.post(f"/r/{token}/form", data={"csrf": csrf, "draft_version": "0", f"v__{K_SD}": "1.9", f"u__{K_SD}": "tCO2e/t",
                                              f"v__{K_CN}": "72083900", "action": "review"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/review")
    r = client.get(f"/r/{token}/review?lang=de")
    assert "1.9 tCO2e/t" in r.text
    idem = re.search(r'name="idem" value="([^"]+)"', r.text).group(1)
    r = client.post(f"/r/{token}/submit", data={"csrf": csrf, "idem": idem, "draft_version": "1"}, follow_redirects=False)
    assert "ATTESTATION_REQUIRED" in r.headers["location"]
    r = client.post(f"/r/{token}/submit", data={"csrf": csrf, "idem": idem, "draft_version": "1", "attest": "on"}, follow_redirects=True)
    assert r.status_code == 200 and "Vielen Dank" in r.text and "sha256:" in r.text
    assert len(core.submissions) == 1


def test_form_page_upload_via_html(client, token):
    r = client.get(f"/r/{token}/form")
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    r = client.post(f"/r/{token}/upload", data={"csrf": csrf}, files={"file": ("plots.geojson", b'{"type":"Point","coordinates":[1.123456,2.123456]}', "application/geo+json")}, follow_redirects=True)
    assert "plots.geojson" in r.text


def test_expired_link_page_offers_renewal(client, token, clock):
    clock.advance(days=40)
    r = client.get(f"/r/{token}?lang=en")
    assert r.status_code == 410 and "Request a new link" in r.text


def test_invalid_link_page(client):
    r = client.get("/r/slk_nope.x?lang=de")
    assert r.status_code == 404 and "ungültig" in r.text


def test_new_device_redirects_to_otp_page(client, token):
    client.get(f"/r/{token}")
    other = TestClient(client.app, base_url="https://testserver")
    r = other.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/verify")
    assert "6" in other.get(f"/r/{token}/verify?lang=en").text


def test_pause_link_get_is_safe_post_pauses(client, svc, core, token, manifest):
    r = client.get(f"/pause/{token}?lang=en")
    assert r.status_code == 200
    sreq, _, _ = svc.get_request(manifest.sreq_id)
    assert not sreq.reminders_paused
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    r = client.post(f"/pause/{token}?lang=en", data={"csrf": csrf})
    assert r.status_code == 200 and "Reminders paused" in r.text
    sreq, _, _ = svc.get_request(manifest.sreq_id)
    assert sreq.reminders_paused and core.events[-1][0] == "reminders.paused"


def test_healthz_and_openapi(client):
    assert client.get("/healthz").json()["status"] == "ok"
    paths = client.get(f"{API}/openapi.json").json()["paths"]
    for p in ("/supplier-api/v1/requests/{token}", "/supplier-api/v1/requests/{token}/draft", "/supplier-api/v1/requests/{token}/uploads",
              "/supplier-api/v1/requests/{token}/parse/cbam-communication", "/supplier-api/v1/requests/{token}/questions",
              "/supplier-api/v1/requests/{token}/decline", "/supplier-api/v1/requests/{token}/submit",
              "/supplier-api/v1/auth/magic-link", "/supplier-api/v1/me/submissions", "/supplier-api/v1/manifests"):
        assert p in paths


def test_eudr_request_form(client, svc):
    m = make_manifest(datapoints=["dp:supply.eudr.geolocation", "dp:supply.eudr.country_of_production", "dp:supply.eudr.production_date",
                                  "dp:supply.eudr.dds_reference", "dp:supply.eudr.dds_verification_number"], locale="en", template_id=None)
    res = svc.ingest_manifest(m, status="sent", approval_ref="apr_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1")
    tok = res["portal_link"].rsplit("/", 1)[1]
    sreq, _, _ = svc.get_request(m.sreq_id)
    assert sreq.template_id == "stpl:eudr_geo_request.en@1.0.0"
    geo = json.dumps({"type": "Point", "coordinates": [-30.123456, -15.123456]})
    r = client.put(f"{API}/requests/{tok}/draft", json={"values": {
        f"dp:supply.eudr.geolocation|{PID}": {"value": geo}, f"dp:supply.eudr.country_of_production|{PID}": {"value": "br"},
        f"dp:supply.eudr.dds_reference|{PID}": {"value": "26DEABCDEFGH1234"}}})
    rules = {h["rule_id"] for h in r.json()["hints"]}
    assert {"PLAUS-016", "PLAUS-021"} <= rules

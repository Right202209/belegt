# Spec: 08 §3（模板 ID、变量、四语 CBAM、EUDR/PCF、催办、lint、审校）
from __future__ import annotations

import re

import pytest

from belegt_supplier.messages import (
    FAMILY_EXTRA_VARIABLES, STPL_RE, TemplateError, TemplateRegistry, parse_template_yaml,
)


@pytest.fixture
def reg():
    return TemplateRegistry.load_builtin()


CTX = {
    "supplier_name": "Anadolu Çelik A.Ş.", "contact_name": "Ayşe Yılmaz", "customer_name": "Müller & Söhne GmbH",
    "products": [{"name": "Warmband", "cn_code": "72083900", "net_mass": "1200.5"}],
    "datapoints": ["dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect", "dp:prod.cbam.installation_id"],
    "period": {"start": "2026-01-01", "end": "2026-12-31"}, "due_date": "2027-03-15", "link_expires_at": "2027-03-29",
    "portal_link": "https://supplier.example.org/r/slk_x", "sender_name": "Anna Weber", "sender_role": "Einkauf",
    "sreq_id": "sreq_01JD8Y3K5M2N4P6Q8R0S2T4V6W", "privacy_note_link": "https://mueller.example/privacy",
    "reminder_count": 3, "calc": {"result": "12300", "low": "9800", "high": "14900", "ref": "calc_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1"},
    "evidence_label": "Prüfbericht", "valid_until": "2027-01-31",
}

EXPECTED_FAMILIES = {
    "cbam_see_request": {"de", "en", "tr", "zh"}, "cbam_see_reminder": {"de", "en", "tr", "zh"},
    "supplier_followup_incomplete": {"de", "en", "tr", "zh"}, "eudr_geo_request": {"en"}, "pcf_request": {"en", "de"},
    "chase_escalation_internal": {"de", "en"}, "certificate_renewal": {"de", "en", "tr", "zh"},
}


def test_catalog_complete(reg):
    fams: dict[str, set] = {}
    for t in reg.all():
        assert STPL_RE.match(t.id)
        fams.setdefault(t.family, set()).add(t.locale)
    assert fams == EXPECTED_FAMILIES


def test_lint_clean(reg):
    errors = [i for i in reg.lint() if i.severity == "error"]
    assert errors == []


@pytest.mark.parametrize("tid", [t.id for t in TemplateRegistry.load_builtin().all()])
def test_render_every_template_and_variant(reg, tid):
    t = reg.get(tid)
    for variant in t.variants:
        m = reg.render(tid, CTX, variant=variant)
        assert m.subject and m.body_text
        assert "{{" not in m.body_text and "{%" not in m.body_text
        assert len(m.body_text) <= 6000
        assert m.body_sha256.startswith("sha256:") and m.template_hash == t.template_hash
        assert "slk_x" not in str(m.variables_sha256)
        if t.external:
            assert "https://supplier.example.org/r/slk_x" in m.body_text
            assert "01JD8Y3K5M2N4P6Q8R0S2T4V6W"[-8:] in m.body_text  # request_ref = 短码


@pytest.mark.parametrize("loc,greeting,deadline,markup", [
    ("de", "Guten Tag Ayşe Yılmaz,", "30. September 2027", "10 % für 2026"),
    ("en", "Dear Ayşe Yılmaz,", "30 September 2027", "10% for 2026"),
    ("tr", "Sayın Ayşe Yılmaz,", "30 Eylül 2027", "2026 için %10"),
    ("zh", "Ayşe Yılmaz 您好：", "2027年9月30日", "2026 年 10%"),
])
def test_cbam_request_four_locales(reg, loc, greeting, deadline, markup):
    m = reg.render(f"stpl:cbam_see_request.{loc}@1.0.0", CTX)
    assert m.body_text.startswith(greeting)
    assert deadline in m.body_text and markup in m.body_text
    assert "CN 72083900" in m.body_text
    assert reg.effective_status(m.template_id) == "reviewed"


def test_period_and_due_localised(reg):
    assert "Kalenderjahr 2026" in reg.render("stpl:cbam_see_request.de@1.0.0", CTX).body_text
    assert "15 Mart 2027" in reg.render("stpl:cbam_see_request.tr@1.0.0", CTX).subject
    assert "2026 日历年度" in reg.render("stpl:cbam_see_request.zh@1.0.0", CTX).body_text
    assert "calendar year 2026" in reg.render("stpl:cbam_see_request.en@1.0.0", CTX).body_text


def test_contact_name_branch(reg):
    ctx = dict(CTX, contact_name=None)
    assert reg.render("stpl:cbam_see_request.en@1.0.0", ctx).body_text.startswith("Dear Sir or Madam,")
    assert reg.render("stpl:cbam_see_request.zh@1.0.0", ctx).body_text.startswith("您好：")


def test_datapoint_labels_localised_and_fallback_marker(reg):
    body = reg.render("stpl:cbam_see_request.tr@1.0.0", dict(CTX, datapoints=["dp:prod.cbam.see_direct", "dp:custom.unknown"])).body_text
    assert "Doğrudan spesifik gömülü emisyonlar" in body
    assert "dp:custom.unknown [EN]" in body


def test_product_list_folds_after_20(reg):
    prods = [{"name": f"P{i}", "cn_code": "72083900"} for i in range(25)]
    body = reg.render("stpl:cbam_see_request.de@1.0.0", dict(CTX, products=prods)).body_text
    assert body.count("- P") == 20 and "insgesamt 25 Positionen" in body


def test_product_summary_max_60(reg):
    m = reg.render("stpl:cbam_see_request.en@1.0.0", dict(CTX, products=[{"name": "X" * 80}]))
    assert "X" * 59 + "…" in m.subject


def test_missing_required_variable(reg):
    with pytest.raises(TemplateError) as e:
        reg.render("stpl:cbam_see_request.en@1.0.0", {k: v for k, v in CTX.items() if k != "supplier_name"})
    assert e.value.code == "TEMPLATE_VARIABLES_INVALID" and "supplier_name" in str(e.value)


def test_url_must_be_https(reg):
    with pytest.raises(TemplateError):
        reg.render("stpl:cbam_see_request.en@1.0.0", dict(CTX, portal_link="ftp://x"))


def test_custom_note_rules(reg):
    tid = "stpl:cbam_see_reminder.en@1.0.0"
    with pytest.raises(TemplateError, match="human confirmation"):
        reg.render(tid, dict(CTX, custom_note="Bitte bis Freitag"), variant="r1")
    with pytest.raises(TemplateError, match="links not allowed"):
        reg.render(tid, dict(CTX, custom_note="see https://evil.example", custom_note_confirmed_by="user:anna"), variant="r1")
    m = reg.render(tid, dict(CTX, custom_note="Danke!", custom_note_confirmed_by="user:anna"), variant="r1")
    assert "Danke!" in m.body_text


def test_escalation_requires_calculation(reg):
    with pytest.raises(TemplateError, match="calc"):
        reg.render("stpl:chase_escalation_internal.en@1.0.0", dict(CTX, calc=None))
    m = reg.render("stpl:chase_escalation_internal.de@1.0.0", CTX)
    assert "€12300" in m.body_text and "10 % Aufschlag" in m.body_text


def test_html_variant_escapes(reg):
    m = reg.render("stpl:cbam_see_request.en@1.0.0", CTX)
    assert "Müller &amp; Söhne" in m.body_html and "Müller & Söhne" in m.body_text


def test_link_domain_whitelist(reg):
    with pytest.raises(TemplateError) as e:
        reg.render("stpl:cbam_see_request.en@1.0.0", CTX, allowed_link_domains=["supplier.example.org"])
    assert e.value.code == "TEMPLATE_LINK_DOMAIN"
    reg.render("stpl:cbam_see_request.en@1.0.0", CTX, allowed_link_domains=["supplier.example.org", "mueller.example"])


def test_unreviewed_templates_not_sendable_until_reviewed(reg):
    tid = "stpl:cbam_see_reminder.de@1.0.0"
    assert reg.effective_status(tid) == "draft" and not reg.is_sendable(tid)
    reg.review(tid, "user:native-de")
    assert reg.is_sendable(tid) and reg.reviewed_by(tid) == "user:native-de"
    reg.deprecate(tid)
    assert not reg.is_sendable(tid)


def test_review_requires_reviewer(reg):
    with pytest.raises(TemplateError):
        reg.review("stpl:cbam_see_reminder.de@1.0.0", "")


def test_en_major_bump_downgrades_other_locales(reg):
    src = reg.get("stpl:cbam_see_request.en@1.0.0")
    yaml_text = f"""
id: "stpl:cbam_see_request.en@2.0.0"
family: cbam_see_request
locale: en
version: "2.0.0"
status: reviewed
reviewed_by: x
variables: {{required: {list(src.required)}, optional: [contact_name]}}
subject: {src.variants['default']['subject']!r}
body: {src.variants['default']['body']!r}
"""
    reg.add(parse_template_yaml(yaml_text))
    assert reg.effective_status("stpl:cbam_see_request.de@1.0.0") == "draft"
    assert reg.effective_status("stpl:cbam_see_request.en@2.0.0") == "reviewed"


def test_resolve_locale_fallback(reg):
    t, fb = reg.resolve("cbam_see_request", "it")
    assert t.locale == "en" and fb == "it"
    t, fb = reg.resolve("cbam_see_request", "de-AT")
    assert t.locale == "de" and fb is None
    t, fb = reg.resolve("cbam_see_reminder", "de")  # draft → 回退 en（也是 draft → 仍返回 en）
    assert t.locale == "en"


def test_lint_detects_problems():
    bad = parse_template_yaml("""
id: "stpl:cbam_see_request.xx@9.0.0"
family: cbam_see_request
locale: xx
version: "9.0.0"
status: draft
variables: {required: [supplier_name, portal_link, foo], optional: []}
subject: "Hi {{ portal_link }}"
body: "Pay 10 % by 2027-09-30 {{ supplier_name }} {{ undeclared }}"
""")
    reg = TemplateRegistry([bad])
    codes = {i.code for i in reg.lint()}
    assert {"VAR_NOT_IN_SPEC", "VAR_UNDECLARED", "REG_HARDCODED", "PORTAL_LINK_MISSING", "VAR_POSITION"} <= codes
    with pytest.raises(TemplateError) as e:
        reg.review(bad.id, "someone")
    assert e.value.code == "TEMPLATE_LINT_FAILED"


def test_template_id_validation():
    with pytest.raises(TemplateError):
        parse_template_yaml('id: "bad"\nfamily: f\nlocale: en\nversion: "1.0.0"\nstatus: draft\nsubject: s\nbody: b\n')


def test_family_extras_only_where_declared():
    assert "calc" in FAMILY_EXTRA_VARIABLES["chase_escalation_internal"]
    assert re.match(STPL_RE, "stpl:cbam_see_reminder.de@1.0.0")

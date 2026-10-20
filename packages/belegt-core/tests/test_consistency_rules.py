"""06 §2.2 规则目录：CR-001…CR-043 每条至少一个正例（fail）与一个反例（pass/不触发），多数含边界例。"""
from __future__ import annotations

import pytest

from belegt_core.consistency import AuditConfig, load_ruleset
from belegt_core.consistency.cel import CelError, check_no_double, compile_expr
from belegt_core.consistency.plugins import compile_plausibility

from review_helpers import CP_A, CP_B, ORG, Y24, ans, calc, conflict, ev, fails, make_ctx, outcomes, q, req, run_rule

S1 = "dp:env.ghg.scope1_total"
ELEC = "dp:energy.consumption.electricity_total"


# ------------------------------------------------------------------ 规则集与 DSL
def test_ruleset_complete_and_compiled():
    rs = load_ruleset()
    assert rs.ref == "consistency-rules@1.0.0"
    assert sorted(rs.rules) == [f"CR-{i:03d}" for i in range(1, 44)]
    for r in rs.rules.values():
        assert r.severity in ("info", "warn", "block") and r.triggers and r.assert_


def test_cel_rejects_double_literals():
    with pytest.raises(CelError):
        check_no_double("x * 1.2 > 3")
    check_no_double("dec(x) * dec('1.2') > dec('3')")
    assert compile_expr("dec_eq(dec('2300'), dec('1820') + dec('520'), dec('0.5'), dec('0.001'))").evaluate_bool({}) is False
    assert compile_expr("dec_eq(dec(a), dec(b), dec('0.5'), dec('0'))").evaluate_bool({"a": "980.4", "b": "980"}) is True


def test_plausibility_compiler():
    src, refs = compile_plausibility("value <= dp:energy.consumption.electricity_total * 1.2", True, {ELEC: "number"})
    assert src == "dec(value) <= dec(p0) * dec('1.2')" and refs == [ELEC]


# ------------------------------------------------------------------ 跨客户
def test_cr_001():
    a = ans(S1, "1820", "tCO2e", cs=[CP_A], status="submitted")
    b = ans(S1, "1790", "tCO2e", cs=[CP_B], status="draft")
    r = fails(run_rule("CR-001", [a, b]), "block")
    assert len(r) == 1 and r[0].detail["delta"] == "30"
    assert outcomes(run_rule("CR-001", [a, ans(S1, "1820.0", "tCO2e", cs=[CP_B])])) == ["pass"]
    # 单位换算后相等：1820 tCO2e == 1820000 kgCO2e
    assert outcomes(run_rule("CR-001", [a, ans(S1, "1820000", "kgCO2e", cs=[CP_B])])) == ["pass"]
    # 有 accepted_divergence 且钉住当前版本 → 通过
    div = conflict("CR-001", [a, b], status="accepted_divergence", resolution={"kind": "accepted_divergence", "note": "x",
        "resolved_by": "user:u_co", "divergence": {"format": "ADR/1", "reason_code": "consolidation", "customer_requirement_refs": ["q_x"],
        "sides": [{"answer": f"{a['id']}#v1", "customer_scope": [CP_A], "basis": "oc"},
                  {"answer": f"{b['id']}#v1", "customer_scope": [CP_B], "basis": "es"}],
        "include_in_export": True, "review_by": "2027-12-31"}})
    assert outcomes(run_rule("CR-001", [a, b, div])) == ["pass"]
    # 任一侧出新版本 → 差异记录不再沿用
    b2 = {**b, "version": 2}
    assert outcomes(run_rule("CR-001", [a, b, b2, div])) == ["fail"]


def test_cr_007():
    a = ans("dp:mat.reach.svhc_present", False, vt="bool", cs=[CP_A], consolidation=None)
    b = ans("dp:mat.reach.svhc_present", True, vt="bool", cs=[CP_B], consolidation=None, status="draft")
    r = fails(run_rule("CR-007", [a, b]), "block")
    assert r and r[0].detail["value_encoding"] == "ordinal"
    assert outcomes(run_rule("CR-007", [a, {**b, "value": False}])) == ["pass"]


def test_cr_008():
    rq = req()
    q3, q17 = q(rq, 3), q(rq, 17)
    a = ans("dp:org.employees.headcount", "212", "1", consolidation=None, period=("2025-12-31", "2025-12-31"),
            status="draft", questions=[q3])
    b = ans("dp:org.employees.headcount", "208", "1", consolidation=None, period=("2025-12-31", "2025-12-31"),
            status="draft", questions=[q17])
    assert outcomes(run_rule("CR-008", [rq, q3, q17, a, b])) == ["fail"]
    assert outcomes(run_rule("CR-008", [rq, q3, q17, a, {**b, "value": "212"}])) == ["pass"]


def test_cr_009():
    last = ans(S1, "1200", "tCO2e", period=Y24, cs=[CP_A], status="submitted", consolidation="operational_control")
    cur = ans(S1, "1250", "tCO2e", cs=[CP_A], status="draft", consolidation="equity_share")
    assert fails(run_rule("CR-009", [last, cur]), "warn")
    assert outcomes(run_rule("CR-009", [last, {**cur, "consolidation": "operational_control"}])) == ["pass"]
    # Scope 2 方法变化（location → market）
    s2l = ans("dp:env.ghg.scope2_location_based", "500", "tCO2e", period=Y24, cs=[CP_A], status="submitted")
    s2m = ans("dp:env.ghg.scope2_market_based", "300", "tCO2e", cs=[CP_A], status="draft")
    assert outcomes(run_rule("CR-009", [s2l, s2m])) == ["fail"]


# ------------------------------------------------------------------ 跨时间
def test_cr_002():
    prev = ans(S1, "1200", "tCO2e", period=Y24)
    cur = ans(S1, "800", "tCO2e", status="draft")
    r = fails(run_rule("CR-002", [prev, cur]), "warn")
    assert r and r[0].detail["threshold"] == "0.3"
    assert outcomes(run_rule("CR-002", [prev, {**cur, "value": "1100"}])) == ["pass"]
    # 边界：恰好 30 % 不触发
    assert outcomes(run_rule("CR-002", [prev, {**cur, "value": "840"}])) == ["pass"]
    # 按 DataPoint 覆盖 θ
    cfg = AuditConfig(yoy_overrides={S1: __import__("decimal").Decimal("0.5")})
    assert outcomes(run_rule("CR-002", [prev, cur], config=cfg)) == ["pass"]


def test_cr_005():
    hist = ans(ELEC, "4820", "MWh", cs=[CP_A], status="submitted", consolidation="operational_control")
    cur = ans(ELEC, "4910", "MWh", cs=[CP_A], status="draft", consolidation="operational_control")
    assert fails(run_rule("CR-005", [hist, cur]), "block")
    assert outcomes(run_rule("CR-005", [hist, {**cur, "value": "4820"}])) == ["pass"]
    # 标注更正（supersedes 指向历史答案）→ 通过
    assert outcomes(run_rule("CR-005", [hist, {**cur, "supersedes": f"{hist['id']}#v1"}])) == ["pass"]


def test_cr_010():
    hist = [ans("dp:env.water.withdrawal_total", v, "m3", period=(f"{y}-01-01", f"{y}-12-31"), consolidation="operational_control",
                status="submitted") for y, v in ((2020, "9000"), (2021, "9100"), (2022, "8900"), (2023, "9050"), (2024, "8950"))]
    cur = ans("dp:env.water.withdrawal_total", "15200", "m3", status="draft")
    assert fails(run_rule("CR-010", hist + [cur]), "warn")
    assert outcomes(run_rule("CR-010", hist + [{**cur, "value": "9200"}])) == ["pass"]
    assert outcomes(run_rule("CR-010", hist[:2] + [cur])) == []  # < 3 期不检


# ------------------------------------------------------------------ 有效期
def test_cr_004():
    e = ev("1", "1", "cert.iso14001", valid_until="2027-03-15", period=None)
    a = ans(ELEC, "1", "MWh", evidence=[e], status="in_review")
    warn = run_rule("CR-004", [e, a], submit_date="2027-03-01")
    assert fails(warn, "warn") and warn[0].detail["values"][0]["value"] == "14"
    block = run_rule("CR-004", [e, a], submit_date="2027-03-16")
    assert fails(block, "block")
    assert outcomes(run_rule("CR-004", [e, a], submit_date="2027-01-01")) == ["pass"]
    assert fails(run_rule("CR-004", [{**e, "status": "expired"}, a], submit_date="2027-01-01"), "block")


def test_cr_012():
    e = ev("4820", "MWh", period=("2025-01-01", "2025-03-31"))
    a = ans(ELEC, "4820", "MWh", evidence=[e], status="draft")
    assert fails(run_rule("CR-012", [e, a], submit_date="2026-11-15"), "warn")  # 594 d > 550
    assert outcomes(run_rule("CR-012", [e, a], submit_date="2026-06-01")) == ["pass"]


def test_cr_013():
    rq = req(due="2026-12-15")
    qq = q(rq, period_requested=("2027-01-01", "2027-12-31"))
    a = ans("dp:cert.iatf16949.valid_until", "2027-06-30", vt="date", consolidation=None, questions=[qq], status="draft")
    assert fails(run_rule("CR-013", [rq, qq, a]), "warn")
    assert outcomes(run_rule("CR-013", [rq, qq, {**a, "value": "2028-05-31"}])) == ["pass"]


def test_cr_014():
    cert_v1 = ev(None, None, "cert.iso14001", vt="file", period=None, valid_until="2025-05-31", status="superseded")
    cert_v2 = {**cert_v1, "version": 2, "status": "verified", "valid_until": "2028-05-31"}
    date_a = ans("dp:cert.iso14001.valid_until", "2028-05-31", vt="date", consolidation=None, status="draft",
                 evidence=[{**cert_v1, "version": 1}])
    assert fails(run_rule("CR-014", [cert_v1, cert_v2, date_a]), "block")  # 引用旧版本证书
    ok = {**date_a, "evidence_refs": [f"{cert_v1['id']}#v2"]}
    assert outcomes(run_rule("CR-014", [cert_v1, cert_v2, ok])) == ["pass"]
    bad_val = {**ok, "value": "2027-01-01"}
    assert outcomes(run_rule("CR-014", [cert_v1, cert_v2, bad_val])) == ["fail"]


def test_cr_015():
    rq = req(reporting_period=("2027-01-01", "2027-03-31"), due=None)
    qq = q(rq)
    a = ans("dp:org.employees.headcount", "212", "1", consolidation=None, period=("2025-12-31", "2025-12-31"),
            questions=[qq], status="draft")
    assert fails(run_rule("CR-015", [rq, qq, a]), "warn")  # 455 d > 400
    assert outcomes(run_rule("CR-015", [rq, qq, {**a, "period": {"start": "2026-12-31", "end": "2026-12-31"}}])) == ["pass"]


def test_cr_016():
    fs_old = {"id": "fs:uba-strommix@2025.04", "org_id": "org_global", "version": 1, "source": "uba-strommix",
              "published_at": "2025-04-01", "status": "active"}
    fs_new = {**fs_old, "id": "fs:uba-strommix@2026.03", "published_at": "2026-03-01"}
    e = ev()
    c = calc([e], "100", "tCO2e", fs="fs:uba-strommix@2025.04")
    a = ans("dp:env.ghg.scope2_location_based", "100", "tCO2e", calc=c, status="draft")
    info = run_rule("CR-016", [fs_old, fs_new, e, c, a])
    assert fails(info, "info")
    warn = run_rule("CR-016", [{**fs_old, "status": "deprecated"}, e, c, a])
    assert fails(warn, "warn")
    assert outcomes(run_rule("CR-016", [fs_old, e, c, a])) == ["pass"]


def test_cr_017():
    a = ans("dp:mat.reach.candidate_list_version", "2025-06-25", vt="date", consolidation=None, status="draft")
    assert fails(run_rule("CR-017", [a], config=AuditConfig(reach_candidate_list_latest="2026-07-01")), "warn")
    assert outcomes(run_rule("CR-017", [a], config=AuditConfig(reach_candidate_list_latest="2025-06-25"))) == ["pass"]
    assert outcomes(run_rule("CR-017", [a])) == ["not_evaluable"]


# ------------------------------------------------------------------ 单位与量纲
def test_cr_011():
    prev = ans(ELEC, "4820", "MWh", period=Y24)
    cur = ans(ELEC, "4820000", "MWh", status="draft")
    r = fails(run_rule("CR-011", [prev, cur]), "block")
    assert r and r[0].detail["ratio"] == "1000"
    assert outcomes(run_rule("CR-011", [prev, {**cur, "value": "5100"}])) == ["pass"]
    # 边界：r = 499 不触发，r = 2000 触发
    assert outcomes(run_rule("CR-011", [prev, {**cur, "value": str(4820 * 499)}])) == ["pass"]
    assert outcomes(run_rule("CR-011", [prev, {**cur, "value": str(4820 * 2000)}])) == ["fail"]


def test_cr_018():
    a = ans("dp:prod.pcf.cradle_to_gate", "1.1", "tCO2e", boundary="product_per_unit", consolidation=None, status="draft")
    assert fails(run_rule("CR-018", [a]), "block")
    assert outcomes(run_rule("CR-018", [{**a, "unit": "kgCO2e/kg"}])) == ["pass"]


def test_cr_019():
    e = ev("120", "tCO2e", "activity.fuel.co2", tags=["raw_unit:tCO2"])
    a = ans(S1, "120", "tCO2e", evidence=[e], status="draft")
    assert fails(run_rule("CR-019", [e, a]), "warn")
    assert outcomes(run_rule("CR-019", [{**e, "tags": []}, a])) == ["pass"]


def test_cr_020():
    prev = ans("dp:energy.consumption.renewable_share", "35", "%", period=Y24)
    cur = ans("dp:energy.consumption.renewable_share", "0.36", "%", status="draft")
    assert fails(run_rule("CR-020", [prev, cur]), "warn")
    assert outcomes(run_rule("CR-020", [prev, {**cur, "value": "36"}])) == ["pass"]


def test_cr_021():
    rq = req()
    q1, q2 = q(rq, 1), q(rq, 2)
    pcf = ans("dp:prod.pcf.cradle_to_gate", "1.1", "kgCO2e/kg", boundary="product_per_unit", consolidation=None,
              questions=[q1], status="draft")
    decl = ans("dp:prod.pcf.declared_unit", "piece", vt="enum", boundary="product_per_unit", consolidation=None,
               questions=[q2], status="draft")
    assert fails(run_rule("CR-021", [rq, q1, q2, pcf, decl]), "block")
    assert outcomes(run_rule("CR-021", [rq, q1, q2, pcf, {**decl, "value": "kg"}])) == ["pass"]


# ------------------------------------------------------------------ 期间与口径
def test_cr_022():
    a = ans("dp:prod.cbam.see_direct", "1.9", "tCO2e/t", boundary="product_per_unit", consolidation=None,
            period=("2025-10-01", "2026-09-30"), status="draft")
    assert fails(run_rule("CR-022", [a]), "block")
    assert outcomes(run_rule("CR-022", [{**a, "period": {"start": "2025-01-01", "end": "2025-12-31"}}])) == ["pass"]


def test_cr_023():
    rq = req()
    q1, q2 = q(rq, 1), q(rq, 2)
    a = ans(S1, "100", "tCO2e", period=("2024-10-01", "2025-09-30"), questions=[q1], status="draft")
    b = ans("dp:env.ghg.scope2_market_based", "50", "tCO2e", questions=[q2], status="draft")
    assert fails(run_rule("CR-023", [rq, q1, q2, a, b]), "warn")
    annotated = [{**a, "tags": ["period_note"]}, {**b, "tags": ["period_note"]}]
    assert outcomes(run_rule("CR-023", [rq, q1, q2] + annotated)) == ["pass"]


def test_cr_024():
    rq = req()
    qq = q(rq)
    site = "site:site_01JC2B3C4D5E6F7G8H9J0K1M2N"
    m = {"id": "map_01JC2B3C4D5E6F7G8H9J0K1M2N", "org_id": ORG, "version": 1, "question_id": qq["id"],
         "datapoint_id": "dp:env.ghg.scope1_total@1.0.0", "boundary_hint": site, "status": "confirmed"}
    a = ans(S1, "100", "tCO2e", questions=[qq], status="draft")
    assert fails(run_rule("CR-024", [rq, qq, m, a]), "warn")
    assert outcomes(run_rule("CR-024", [rq, qq, m, {**a, "boundary": site, "consolidation": None}])) == ["pass"]


def test_cr_025():
    rq = req()
    q1, q2 = q(rq, 1), q(rq, 2)
    a = ans(S1, "100", "tCO2e", questions=[q1], status="draft", consolidation="operational_control")
    b = ans("dp:env.ghg.scope2_market_based", "50", "tCO2e", questions=[q2], status="draft", consolidation="financial_control")
    assert fails(run_rule("CR-025", [rq, q1, q2, a, b]), "block")
    assert outcomes(run_rule("CR-025", [rq, q1, q2, a, {**b, "consolidation": "operational_control"}])) == ["pass"]


def test_cr_026():
    bills = [ev("400", "MWh", period=(f"2025-{m:02d}-01", f"2025-{m:02d}-28")) for m in range(1, 13) if m != 8]
    a = ans(ELEC, "4400", "MWh", evidence=bills, status="draft")
    r = fails(run_rule("CR-026", bills + [a]), "warn")
    assert r and r[0].detail["gaps"] >= 1
    full = [ev("400", "MWh", period=("2025-01-01", "2025-06-30")), ev("400", "MWh", period=("2025-07-01", "2025-12-31"))]
    assert outcomes(run_rule("CR-026", full + [ans(ELEC, "800", "MWh", evidence=full, status="draft")])) == ["pass"]
    short = ans(ELEC, "4400", "MWh", period=("2025-01-01", "2025-11-30"), status="draft")
    assert outcomes(run_rule("CR-026", [short])) == ["fail"]


# ------------------------------------------------------------------ 计算一致性
def test_cr_003():
    w = lambda t, h, n: [ans("dp:env.waste.generated", t, "t"), ans("dp:env.waste.generated.hazardous", h, "t"),  # noqa: E731
                         ans("dp:env.waste.generated.non_hazardous", n, "t")]
    r = fails(run_rule("CR-003", w("1010", "120", "860")), "block")
    assert r and r[0].instance_id == "CR-003:dp:env.waste.generated" and r[0].detail["delta"] == "30"
    assert outcomes(run_rule("CR-003", w("980.4", "120", "860"))) == ["pass"]  # 0.4 ≤ 0.5（T-05）
    # lte 关系：女 + 男 > 总人数
    hc = dict(unit="1", consolidation=None, period=("2025-12-31", "2025-12-31"))
    lte = [ans("dp:org.employees.headcount", "200", **hc), ans("dp:social.workforce.headcount.female", "90", **hc),
           ans("dp:social.workforce.headcount.male", "120", **hc)]
    assert outcomes(run_rule("CR-003", lte)) == ["fail"]
    lte[2] = {**lte[2], "value": "100"}
    assert outcomes(run_rule("CR-003", lte)) == ["pass"]


def test_cr_027():
    rq = req()
    qt = q(rq, 1, tags=["scope2_method:location_based"])
    tot = ans("dp:env.ghg.scope1_2_total_requested", "2300", "tCO2e", questions=[qt], status="draft")
    s1 = ans(S1, "1820", "tCO2e")
    s2 = ans("dp:env.ghg.scope2_location_based", "520", "tCO2e")
    assert fails(run_rule("CR-027", [rq, qt, tot, s1, s2]), "block")
    assert outcomes(run_rule("CR-027", [rq, qt, {**tot, "value": "2340"}, s1, s2])) == ["pass"]
    assert outcomes(run_rule("CR-027", [rq, qt, tot, s1])) == ["not_evaluable"]


def test_cr_028():
    el = ans(ELEC, "4820", "MWh")
    s2 = ans("dp:env.ghg.scope2_location_based", "9100", "tCO2e", status="draft")
    assert fails(run_rule("CR-028", [el, s2]), "warn")
    assert outcomes(run_rule("CR-028", [el, {**s2, "value": "1800"}])) == ["pass"]


def test_cr_029():
    el = ans(ELEC, "4820", "MWh")
    eac = ans("dp:energy.procurement.eac_volume", "5000", "MWh", status="draft")
    r = run_rule("CR-029", [el, eac])
    assert fails(r, "block")
    assert outcomes(run_rule("CR-029", [el, {**eac, "value": "3000"}])) == ["pass"]
    share = ans("dp:energy.consumption.renewable_share", "10", "%")
    total = ans("dp:energy.consumption.total", "10000", "MWh")
    r2 = run_rule("CR-029", [el, {**eac, "value": "3000"}, share, total])
    assert [x.severity for x in fails(r2)] == ["warn"]  # 10 % × 10000 = 1000 < 3000


def test_cr_030():
    prod = {"id": "prod_01JC2B3C4D5E6F7G8H9J0K1M2N", "org_id": ORG, "version": 1, "name": "Teil",
            "site_ids": ["site_01JC2B3C4D5E6F7G8H9J0K1M2N"]}
    pb = f"product:{prod['id']}"
    site = "site:site_01JC2B3C4D5E6F7G8H9J0K1M2N"
    mass = ev("8000", "t", "activity.production.mass", boundary=pb)
    alpha = ev("0.7", "1", "allocation.site_share", boundary=pb)
    s1 = ans(S1, "10000", "tCO2e", boundary=site, consolidation=None)
    s2 = ans("dp:env.ghg.scope2_location_based", "6000", "tCO2e", boundary=site, consolidation=None)
    pcf = ans("dp:prod.pcf.cradle_to_gate", "1.1", "kgCO2e/kg", boundary=pb, consolidation=None, status="draft")
    assert fails(run_rule("CR-030", [prod, mass, alpha, s1, s2, pcf]), "warn")  # 8800 < 0.9×0.7×16000
    assert outcomes(run_rule("CR-030", [prod, mass, alpha, s1, s2, {**pcf, "value": "1.5"}])) == ["pass"]
    assert outcomes(run_rule("CR-030", [prod, s1, s2, pcf])) == ["not_evaluable"]


def test_cr_031():
    e = ev()
    c = calc([e], "0.52", "tCO2e/t", factors=[{"factor_key": "grid", "value": "0.6", "unit": "tCO2e/MWh", "source_row_id": "r"}])
    see = ans("dp:prod.cbam.see_indirect", "0.52", "tCO2e/t", boundary="product_per_unit", consolidation=None, calc=c,
              status="draft")
    sec = ans("dp:prod.cbam.specific_electricity_consumption", "0.55", "MWh/t", boundary="product_per_unit", consolidation=None)
    assert fails(run_rule("CR-031", [e, c, see, sec]), "warn")  # 0.55×0.6 = 0.33
    assert outcomes(run_rule("CR-031", [e, c, {**see, "value": "0.33"}, sec])) == ["pass"]


def test_cr_032():
    a = ans("dp:prod.battery.recycled_content.cobalt", "112", "%", boundary="product_per_unit", consolidation=None, status="draft")
    assert fails(run_rule("CR-032", [a]), "block")
    assert outcomes(run_rule("CR-032", [{**a, "value": "12"}])) == ["pass"]
    hc = ans("dp:org.employees.headcount", "212.5", "1", consolidation=None, period=("2025-12-31", "2025-12-31"), status="draft")
    assert outcomes(run_rule("CR-032", [hc])) == ["fail"]  # regex ^[0-9]+$


def test_cr_033():
    tin = ans("dp:mat.conflict_minerals.intentionally_added.tin", True, vt="bool", consolidation=None,
              period=("2025-12-31", "2025-12-31"))
    smelters = ans("dp:mat.conflict_minerals.smelter_list", "", vt="file", consolidation=None,
                   period=("2025-12-31", "2025-12-31"), status="draft")
    r = run_rule("CR-033", [tin, smelters])
    assert fails(r, "warn") and r[0].instance_id == "CR-033:dp:mat.conflict_minerals.smelter_list"
    assert outcomes(run_rule("CR-033", [{**tin, "value": False}, smelters])) == ["pass"]
    # 数值型字典派生：总能耗 ≥ 电力 + 燃料
    e_tot = ans("dp:energy.consumption.total", "5000", "MWh", status="draft")
    e_el = ans(ELEC, "4820", "MWh")
    e_fu = ans("dp:energy.consumption.fuels_total", "800", "MWh")
    assert outcomes(run_rule("CR-033", [e_tot, e_el, e_fu])) == ["fail"]
    assert outcomes(run_rule("CR-033", [{**e_tot, "value": "5620"}, e_el, e_fu])) == ["pass"]


# ------------------------------------------------------------------ 引用完整性
def test_cr_006():
    erp = ev("4820", "MWh", source_type="erp")
    bill = ev("4790", "MWh")
    assert fails(run_rule("CR-006", [erp, bill]), "block")
    assert outcomes(run_rule("CR-006", [erp, {**bill, "extracted_value": "4800"}])) == ["pass"]  # 0.41 % ≤ 0.5 %
    assert outcomes(run_rule("CR-006", [erp, {**bill, "extracted_value": "4820000", "unit": "kWh"}])) == ["pass"]


def test_cr_034():
    e = ev("4820", "MWh")
    ok = ans(ELEC, "4820", "MWh", evidence=[e], status="draft")
    assert outcomes(run_rule("CR-034", [e, ok])) == ["pass"]
    assert outcomes(run_rule("CR-034", [ans(ELEC, "4820", "MWh", status="draft")])) == ["fail"]  # 无引用
    assert outcomes(run_rule("CR-034", [{**e, "status": "rejected"}, ok])) == ["fail"]
    assert outcomes(run_rule("CR-034", [{**e, "trust_zone": "quarantine"}, ok])) == ["fail"]
    assert outcomes(run_rule("CR-034", [e, {**ok, "value": "4900"}])) == ["fail"]  # 值 ≠ 引用


def test_cr_035():
    v2 = ev("4820", "MWh", version=2)
    v3 = {**v2, "version": 3, "extracted_value": "4870"}
    a = ans(ELEC, "4820", "MWh", evidence=[v2], status="draft")
    assert fails(run_rule("CR-035", [v2, v3, a]), "block")
    meta_only = {**v2, "version": 3, "confidence": 0.99}
    assert fails(run_rule("CR-035", [v2, meta_only, a]), "info")
    assert outcomes(run_rule("CR-035", [v2, a])) == []


def test_cr_036():
    hkn = ev("1200", "MWh", "activity.electricity.eac", version=1)
    c = calc([hkn], "1468.8", "tCO2e")
    a = ans("dp:env.ghg.scope2_market_based", "1468.8", "tCO2e", calc=c, status="draft")
    hkn3 = {**hkn, "version": 3, "extracted_value": "1500"}
    r = run_rule("CR-036", [hkn, hkn3, c, a])
    assert fails(r, "block") and r[0].events[0]["type"] == "calc.recompute_requested"
    assert outcomes(run_rule("CR-036", [hkn, c, a])) == ["pass"]


def test_cr_037():
    e = ev("120", "t")
    a = ans("dp:gov.policy.code_of_conduct", "Wir haben unsere Emissionen um 18 % gesenkt", vt="text", consolidation=None,
            evidence=[e], status="draft", text_lang="de")
    r = fails(run_rule("CR-037", [e, a]), "block")
    assert r and r[0].detail["unsourced_tokens"] == ["18"]
    assert outcomes(run_rule("CR-037", [{**e, "extracted_value": "18"}, a])) == ["pass"]


# ------------------------------------------------------------------ 估算标识
def test_cr_038():
    a = ans(ELEC, "4400", "MWh", is_estimate=True, estimate_method="time_proration", status="draft")
    assert fails(run_rule("CR-038", [a]), "block")
    assert outcomes(run_rule("CR-038", [{**a, "tags": ["export_note"]}])) == ["pass"]
    assert outcomes(run_rule("CR-038", [{**a, "is_estimate": False}])) == []


def test_cr_039():
    est_src = ev("4400", "MWh", tags=["estimate"])
    a = ans(ELEC, "4400", "MWh", evidence=[est_src], is_estimate=True, estimate_method="x", status="draft")
    measured = ev("4820", "MWh")  # 同 fact_key、覆盖全年
    assert fails(run_rule("CR-039", [est_src, measured, a]), "warn")
    assert outcomes(run_rule("CR-039", [est_src, a])) == ["pass"]


def test_cr_040():
    rq = req()
    qs = [q(rq, i) for i in range(1, 11)]
    answers = [ans(ELEC, "1", "MWh", questions=[qs[i]], status="draft", is_estimate=i < 4, estimate_method="x" if i < 4 else None,
                   period=(f"20{10 + i}-01-01", f"20{10 + i}-12-31")) for i in range(10)]
    r = run_rule("CR-040", [rq] + qs + answers)
    assert fails(r, "info")  # 4/10 > 0.30
    answers2 = [{**x, "is_estimate": False} if i in (0, 1) else x for i, x in enumerate(answers)]
    assert outcomes(run_rule("CR-040", [rq] + qs + answers2)) == ["pass"]  # 2/10


# ------------------------------------------------------------------ 法律承诺
def test_cr_041():
    rq = req()
    qq = q(rq, text="Der Lieferant sichert zu …", tags=["legal_commitment", "legal:LC-01"])
    a = ans("dp:env.ghg_target.has_target", True, vt="bool", consolidation=None, questions=[qq], status="draft")
    assert fails(run_rule("CR-041", [rq, qq, a]), "block")
    assert outcomes(run_rule("CR-041", [rq, qq, {**a, "legal_commitment": True}])) == ["pass"]
    cancelled = {**qq, "tags": ["legal_cancelled"]}
    assert outcomes(run_rule("CR-041", [rq, cancelled, a])) == []


def test_cr_042():
    a = ans("dp:env.ghg_target.has_target", True, vt="bool", consolidation=None, status="in_review",
            legal_commitment=True, risk_level="L3")
    assert outcomes(run_rule("CR-042", [a])) == ["pass"]
    assert fails(run_rule("CR-042", [a], staged_batch_answers=[a["id"]]), "block")  # 进入批次
    assert fails(run_rule("CR-042", [{**a, "risk_level": "L0", "reused_from": "ans_x#v1"}]), "block")  # L0 复用


# ------------------------------------------------------------------ 价值链上限
def test_cr_043():
    cp = {"id": CP_A, "org_id": ORG, "version": 1, "role": "customer", "legal_name": "OEM", "tags": ["fy_start:2027-01-01"]}
    rq = req(tags=["purpose:vsme"])
    qq = q(rq)
    m = {"id": "map_01JC2B3C4D5E6F7G8H9J0K1M2Q", "org_id": ORG, "version": 1, "question_id": qq["id"],
         "datapoint_id": "dp:env.ghg.scope3_category.c04@1.0.0", "status": "confirmed"}
    r = run_rule("CR-043", [cp, rq, qq, m])
    assert fails(r, "info")
    m_in = {**m, "datapoint_id": "dp:env.ghg.scope1_total@1.0.0"}  # Annex II（vsme 标签）
    assert outcomes(run_rule("CR-043", [cp, rq, qq, m_in])) == ["pass"]
    assert outcomes(run_rule("CR-043", [cp, {**rq, "tags": ["purpose:cbam"]}, qq, m])) == ["pass"]
    assert outcomes(run_rule("CR-043", [{**cp, "tags": ["fy_start:2026-01-01"]}, rq, qq, m])) == ["pass"]


def test_every_rule_has_a_test():
    names = {n for n in globals() if n.startswith("test_cr_")}
    assert {f"test_cr_{i:03d}" for i in range(1, 44)} <= names


# ------------------------------------------------------------------ §1.5 解释边界、§8.2-5 覆盖矩阵
def test_explainer_guard_falls_back_to_template():
    from belegt_core.consistency import TemplateExplainer, guarded_explanation

    c = {"datapoint_id": "dp:env.ghg.scope1_total@1.0.0", "severity": "blocking",
         "detail": {"values": [{"ref": "ans_x#v1", "value": "1820", "unit": "tCO2e"}, {"ref": "ans_y#v1", "value": "1790"}]}}
    rule = load_ruleset().rules["CR-001"]

    class Good:
        def explain(self, conflict, rule, context=None):
            return {"de": "1820 und 1790 weichen ab.", "en": "1820 vs 1790 differ."}

    class Bad:
        def explain(self, conflict, rule, context=None):
            return {"de": "Richtig wäre 1805.", "en": "Should be 1805."}  # 新数字 → 丢弃

    class Boom:
        def explain(self, conflict, rule, context=None):
            raise TimeoutError

    assert guarded_explanation(c, rule, Good())[1] == "explainer"
    for ex in (Bad(), Boom(), None):
        text, src = guarded_explanation(c, rule, ex)
        assert src == "template" and "1805" not in text["en"] and "1820" in text["de"]
    assert set(TemplateExplainer().explain(c, rule)) == {"de", "en", "zh"}


def test_coverage_matrix_sum_of_instances():
    from belegt_core.consistency import MetaProvider
    from belegt_core.consistency.plugins import _sum_relations

    ctx = make_ctx([])
    rels = {t for t, _, _ in _sum_relations(ctx)}
    for m in MetaProvider.default().all():
        if m.sum_of:
            assert m.id in rels  # 每个 sum_of 关系都编译出 CR-003 实例
    rs = load_ruleset()
    for rid in ("CR-001", "CR-002", "CR-005", "CR-011"):  # 每个数值数据点都被这四条覆盖（插件按 value_type 通用选择）
        assert "answer_write" in rs.rules[rid].triggers and "pre_submit_gate" in rs.rules[rid].triggers

"""06 §8.1 测试用例 T-01…T-14（GraphStore 端到端）+ 冲突生命周期 §3 + 门禁 fail-closed。"""
from __future__ import annotations

import pytest

from belegt_core import GraphError
from belegt_core.approval import ApprovalRouter, BatchError, BatchStager
from belegt_core.consistency import ConsistencyEngine, resolve_conflict
from belegt_core.consistency.plugins import Binding
from belegt_core.expiry import apply_expiry, scan_expiry
from belegt_core.legal import ScriptedClassifier, cancel_legal_flag, detect_legal_clauses, flag_question
from belegt_core.rules.numtext import numeric_tokens

from review_helpers import CO, CP_A, CP_B, EXEC, FILLER, ORG, Y24, World

S1 = "dp:env.ghg.scope1_total"
ELEC = "dp:energy.consumption.electricity_total"
WASTE = "dp:env.waste.generated"


@pytest.fixture
def w(store):
    return World(store)


@pytest.fixture
def eng(store):
    return ConsistencyEngine(store)


def _cfl(store, ref):
    return store.get(ref.split("#")[0])


def _cross_customer(w):
    ra, rb = w.request(CP_A), w.request(CP_B)
    qa, qb = w.question(ra, 1), w.question(rb, 1)
    e1 = w.evidence("1820", "tCO2e", "activity.ghg.scope1.a")
    e2 = w.evidence("1790", "tCO2e", "activity.ghg.scope1.b")
    a = w.answer(S1, "1820", "tCO2e", evidence=[e1], questions=[qa], cs=[CP_A], status="submitted")
    b = w.answer(S1, "1790", "tCO2e", evidence=[e2], questions=[qb], cs=[CP_B])
    return ra, rb, qa, qb, a, b


# ------------------------------------------------------------------ T-01 / T-02
def test_t01_cross_customer_blocking_and_risk00(store, w, eng):
    *_, a, b = _cross_customer(w)
    b2 = store.write("Answer", {"id": b.split("#")[0], "consolidation": "operational_control"}, expected_version=1,
                     actor=__import__("review_helpers").CALC_A, patch_mode="merge_from_head")
    run = eng.run("answer_write", org_id=ORG, answer_refs=[b2])
    cr1 = [r for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-001"]
    assert len(cr1) == 1
    c = _cfl(store, cr1[0])
    assert c["severity"] == "blocking" and c["detail"]["delta"] == "30"
    assert c["detail"]["ruleset_version"] == "consistency-rules@1.0.0" and set(c["detail"]["evaluated_inputs"]) == {a, b2}
    assert c["explanation_by"] == "agent:consistency-auditor"
    res = ApprovalRouter(store).route(ORG, answer_refs=[b2])
    assert not res.routed and res.blocked[0]["rule_id"] == "RISK-00"
    # 去重：同一 conflict_key 只有一个 open
    again = eng.run("answer_write", org_id=ORG, answer_refs=[b2])
    assert not again.conflicts_written and cr1[0] in again.conflicts_existing


def test_t02_accepted_divergence_then_gate_and_l3(store, w, eng):
    ra, rb, qa, qb, a, b = _cross_customer(w)
    run = eng.run("answer_write", org_id=ORG, answer_refs=[b])
    cref = next(r for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-001")
    div = {"format": "ADR/1", "reason_code": "consolidation", "customer_requirement_refs": [qa, qb],
           "sides": [{"answer": a, "customer_scope": [CP_A], "basis": "operational_control"},
                     {"answer": b, "customer_scope": [CP_B], "basis": "equity_share"}],
           "disclosure_text": {"de": "Werte nach unterschiedlicher Konsolidierungsmethode gemäß Kundenvorgabe.",
                               "en": "Values follow different consolidation methods as required by each customer."},
           "include_in_export": True, "review_by": "2026-12-31"}
    out = resolve_conflict(store, cref.split("#")[0], actor=CO, kind="accepted_divergence",
                           note="Kunden verlangen unterschiedliche Konsolidierung", divergence=div)
    c = store.get(cref.split("#")[0])
    assert c["status"] == "accepted_divergence" and c["resolution"]["divergence"]["format"] == "ADR/1"
    apr = store.get(out["approval_ref"].split("#")[0])
    assert apr["risk_level"] == "L3" and apr["subject_ref"] == cref and apr["status"] == "approved"
    gate = eng.gate(ORG, rb)
    assert not [r for r in gate.conflicts_written if _cfl(store, r)["rule_id"] == "CR-001"]
    router = ApprovalRouter(store)
    ctx = router.ctx(ORG)
    assert router.assess(ctx, ctx.head(a)).level == "L3" and router.assess(ctx, ctx.head(b)).level == "L3"
    routed = router.route(ORG, answer_refs=[b]).routed
    assert routed and routed[0].risk_level == "L3" and routed[0].rule_id in ("RISK-02", "RISK-01", "RISK-03")


def test_adr1_validation(store, w, eng):
    ra, rb, qa, qb, a, b = _cross_customer(w)
    cref = eng.run("answer_write", org_id=ORG, answer_refs=[b]).conflicts_written[0].split("#")[0]
    base = {"reason_code": "consolidation", "customer_requirement_refs": [qa, qb], "include_in_export": False,
            "review_by": "2026-12-31",
            "sides": [{"answer": a, "customer_scope": [CP_A], "basis": "oc"}, {"answer": b, "customer_scope": [CP_B], "basis": "es"}]}
    with pytest.raises(GraphError) as e:  # 没有客户依据 → 只能选 A
        resolve_conflict(store, cref, actor=CO, kind="accepted_divergence", note="x", divergence={**base, "customer_requirement_refs": [qa]})
    assert e.value.rule_id == "ADR/1"
    with pytest.raises(GraphError):  # review_by 晚于期末 + 15 个月
        resolve_conflict(store, cref, actor=CO, kind="accepted_divergence", note="x", divergence={**base, "review_by": "2027-06-01"})
    with pytest.raises(GraphError):  # customer_scope 与答案不一致 / 重叠
        resolve_conflict(store, cref, actor=CO, kind="accepted_divergence", note="x",
                         divergence={**base, "sides": [base["sides"][0], {**base["sides"][1], "customer_scope": [CP_A]}]})
    with pytest.raises(GraphError) as e2:  # executive 不能选 B/C
        resolve_conflict(store, cref, actor=EXEC, kind="dismissed", note="false positive")
    assert e2.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
    assert store.get(cref)["status"] == "open"


def test_dismiss_and_reopen_on_new_version(store, w, eng):
    *_, a, b = _cross_customer(w)
    cref = eng.run("answer_write", org_id=ORG, answer_refs=[b]).conflicts_written[0].split("#")[0]
    out = resolve_conflict(store, cref, actor=CO, kind="dismissed", note="Fragen betreffen verschiedene Abgrenzungen",
                           dismiss_reason="parse_error")
    assert store.get(cref)["status"] == "dismissed"
    assert any(f["type"] == "eval.false_positive_sample" for f in out["followups"])
    # 同版本复检：处置结论仍有效，不重开
    assert not eng.run("answer_write", org_id=ORG, answer_refs=[b]).conflicts_written
    # 任一侧出新版本 → 新 open 冲突 supersedes 旧冲突（06 §3.1）
    import review_helpers as H

    b2 = store.write("Answer", {"id": b.split("#")[0], "value": "1790"}, expected_version=1, actor=H.CALC_A,
                     patch_mode="merge_from_head")
    new = eng.run("answer_write", org_id=ORG, answer_refs=[b2]).conflicts_written
    assert new and store.get(new[0].split("#")[0])["supersedes"] == f"{cref}#v2"


def test_corrected_with_base_side_creates_human_confirmation(store, w, eng):
    *_, a, b = _cross_customer(w)
    cref = eng.run("answer_write", org_id=ORG, answer_refs=[b]).conflicts_written[0].split("#")[0]
    out = resolve_conflict(store, cref, actor=CO, kind="corrected", note="Wert für OEM-B an OEM-A angleichen", base_side=0)
    assert store.get(cref)["status"] == "resolved"
    ev = store.get(out["created"]["evidence"])
    new = store.get(out["created"]["answer"])
    assert ev["source_type"] == "human_confirmation" and ev["extracted_value"] == "1820"
    assert new["value"] == "1820" and new["supersedes"] == b and new["status"] == "draft"


def test_auto_cleared_when_subjects_superseded(store, w, eng):
    e1 = w.evidence("120", "t", "activity.waste.haz")
    e2 = w.evidence("860", "t", "activity.waste.nonhaz")
    e3 = w.evidence("1010", "t", "activity.waste.total")
    t = w.answer(WASTE, "1010", "t", evidence=[e3])
    h = w.answer(WASTE + ".hazardous", "120", "t", evidence=[e1])
    n = w.answer(WASTE + ".non_hazardous", "860", "t", evidence=[e2])
    run = eng.run("daily_batch", org_id=ORG)
    cref = next(r for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-003")
    import review_helpers as H

    e4 = w.evidence("980", "t", "activity.waste.total2")
    store.write("Answer", {"id": t.split("#")[0], "value": "980", "evidence_refs": [e4]}, expected_version=1,
                     actor=H.CALC_A, patch_mode="merge_from_head")
    store.write("Answer", {"id": h.split("#")[0], "tags": ["rechecked"]}, expected_version=1, actor=H.CALC_A,
                     patch_mode="merge_from_head")
    store.write("Answer", {"id": n.split("#")[0], "tags": ["rechecked"]}, expected_version=1, actor=H.CALC_A,
                     patch_mode="merge_from_head")
    run2 = eng.run("daily_batch", org_id=ORG)
    assert any(r.startswith(cref.split("#")[0]) for r in run2.auto_cleared)
    c = store.get(cref.split("#")[0])
    assert c["status"] == "resolved" and c["resolution"]["kind"] == "auto_cleared"


# ------------------------------------------------------------------ T-03
def test_t03_magnitude_and_explanation_numbers(store, w, eng):
    rq = w.request(CP_A)
    qq = w.question(rq)
    e_prev = w.evidence("4820", "MWh", period=Y24, valid_from="2024-01-01")
    w.answer(ELEC, "4820", "MWh", evidence=[e_prev], period=Y24, status="approved")
    e_cur = w.evidence("4820000", "MWh")
    cur = w.answer(ELEC, "4820000", "MWh", evidence=[e_cur], questions=[qq])
    run = eng.run("answer_write", org_id=ORG, answer_refs=[cur])
    by_rule = {_cfl(store, r)["rule_id"]: _cfl(store, r) for r in run.conflicts_written}
    assert by_rule["CR-011"]["severity"] == "blocking" and by_rule["CR-011"]["detail"]["ratio"] == "1000"
    assert by_rule["CR-002"]["severity"] == "warning"
    for lang, text in by_rule["CR-011"]["explanation"].items():
        assert set(numeric_tokens(text)) == {"4820", "4820000"}, (lang, text)


# ------------------------------------------------------------------ T-04 / T-05
@pytest.mark.parametrize("total,blocking", [("1010", True), ("980.4", False)])
def test_t04_t05_sum_of_parts(store, w, eng, total, blocking):
    rq = w.request(CP_A)
    qs = [w.question(rq, i) for i in (1, 2, 3)]
    refs = []
    for (dp, v), qq in zip(((WASTE, total), (WASTE + ".hazardous", "120"), (WASTE + ".non_hazardous", "860")), qs):
        refs.append(w.answer(dp, v, "t", evidence=[w.evidence(v, "t", f"activity.{dp[-12:]}")], questions=[qq]))
    run = eng.gate(ORG, rq)
    cr3 = [r for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-003"]
    assert bool(cr3) is blocking
    assert run.gate == ("block" if blocking else "pass")
    if blocking:
        assert _cfl(store, cr3[0])["detail"]["instance_id"] == "CR-003:dp:env.waste.generated"


# ------------------------------------------------------------------ T-06
def test_t06_expiring_evidence_then_submit_blocked(store, w, eng, clock):
    rq = w.request(CP_A, due="2027-03-20")
    qq = w.question(rq)
    e = w.evidence("4820", "MWh", valid_until="2027-03-15")
    a = w.answer(ELEC, "4820", "MWh", evidence=[e], questions=[qq])
    warn = eng.run("pre_submit_gate", org_id=ORG, request_id=rq, submit_date="2027-03-01", write=False)
    assert [r.severity for r in warn.failed("CR-004")] == ["warn"]
    block = eng.run("pre_submit_gate", org_id=ORG, request_id=rq, submit_date="2027-03-16", write=False)
    assert [r.severity for r in block.failed("CR-004")] == ["block"]
    appr = w.approve(a)
    clock.set("2027-03-16T09:00:00+00:00")
    res = apply_expiry(store, ORG)
    assert res["expired"] and store.get(e.split("#")[0])["status"] == "expired"
    h = store.get(appr.split("#")[0])
    with pytest.raises(GraphError) as err:
        store.write("Answer", {"id": h["id"], "status": "submitted", "submission": {
            "request_id": rq, "channel": "file_export", "submitted_at": "2027-03-16T09:00:00Z",
            "export_input_hash": "sha256:" + "d" * 64}}, expected_version=h["version"], actor=FILLER, patch_mode="merge_from_head")
    assert err.value.code == "GRAPH_SUBMIT_BLOCKED"


# ------------------------------------------------------------------ T-07
def test_t07_calendar_year(store, w, eng):
    e = w.evidence("1.9", "tCO2e/t", "cbam.see.direct", period=("2025-10-01", "2026-09-30"), valid_from="2025-10-01")
    a = w.answer("dp:prod.cbam.see_direct", "1.9", "tCO2e/t", evidence=[e], boundary="product_per_unit", consolidation=None,
                 period=("2025-10-01", "2026-09-30"))
    run = eng.run("answer_write", org_id=ORG, answer_refs=[a])
    assert [_cfl(store, r)["severity"] for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-022"] == ["blocking"]


# ------------------------------------------------------------------ T-08 / T-09（法律承诺）
def _legal_answer(store, w, text, classifier=None):
    rq = w.request(CP_A)
    det = detect_legal_clauses(text, "de", classifier=classifier)
    qbody = flag_question({"tags": []}, det)
    qq = w.question(rq, 1, text, tags=qbody.get("tags") or None)
    a = w.answer("dp:gov.policy.code_of_conduct", "Wir stimmen zu.", vt="text", questions=[qq], consolidation=None)
    return rq, qq, a, det


def test_t08_rule_channel_flags_and_batch_rejected(store, w, eng):
    text = "Wir bestätigen nach bestem Wissen die Richtigkeit der oben gemachten Angaben."
    rq, qq, a, det = _legal_answer(store, w, text, ScriptedClassifier({"LC-06": 0.22}))
    assert det.flagged and det.types == ["LC-06"] and det.channels == ["rule"]
    router = ApprovalRouter(store)
    routed = router.route(ORG, answer_refs=[a]).routed[0]
    ans = store.get(a.split("#")[0])
    assert routed.risk_level == "L3" and ans["legal_commitment"] is True and ans["risk_level"] == "L3"
    stager = BatchStager(store, router, eng)
    with pytest.raises(BatchError) as e:
        stager.stage(CO, [routed.approval_ref.split("#")[0]])
    reasons = e.value.details["check"]["items"][0]["reasons"]
    assert any("CR-042" in r for r in reasons) and any("cannot be batch" in r for r in reasons)
    cr42 = [c for c in store.query("Conflict", org_id=ORG) if c["rule_id"] == "CR-042"]
    assert cr42 and cr42[0]["severity"] == "blocking"
    # 撤出批次后复检通过 → CR-042 自动关闭（暂态原因）
    eng.run("answer_write", org_id=ORG, answer_refs=[f"{ans['id']}#v{ans['version']}"])
    assert store.get(cr42[0]["id"])["status"] == "resolved"


def test_t09_llm_channel_or_fusion_and_cancel(store, w):
    text = "Der Lieferant übernimmt sämtliche Kosten, die dem Kunden aus unrichtigen Angaben entstehen."
    rq, qq, a, det = _legal_answer(store, w, text, ScriptedClassifier({"LC-02": 0.41}))
    assert det.flagged and det.channels == ["llm"] and det.types == ["LC-02"] and not det.rule_hits
    with pytest.raises(GraphError):
        cancel_legal_flag(store, qq, actor=CO, reason="")
    with pytest.raises(GraphError):
        cancel_legal_flag(store, qq, actor=EXEC, reason="Kein Haftungsbezug, reine Kostenregel")
    sample = cancel_legal_flag(store, qq, actor=CO, reason="Kein Haftungsbezug, reine Kostenregel")
    assert sample["eval_set"] == "FR-406" and sample["types"] == ["LC-02"]
    assert "legal_cancelled" in store.get(qq)["tags"]
    assert any((e.get("details") or {}).get("event") == "legal.flag.cancel" for e in store.audit_events(ORG))


# ------------------------------------------------------------------ T-10
def test_t10_lineage_superseded_triggers_cr036(store, chain, eng):
    import copy

    from helpers import FIX, MINER, strip_env

    hkn = strip_env(copy.deepcopy(FIX["evidence_hkn"]))
    new = {k: v for k, v in hkn.items() if k not in ("id", "verification", "supersedes", "provenance")}
    from belegt_core.ids import new_id

    new.update(id=new_id("Evidence"), extracted_value="1500", status="candidate", corrects=chain["evidence_hkn"], confidence=0.95)
    store.write("Evidence", new, actor=MINER)
    v1 = store.write("Evidence", {"id": new["id"], "status": "verified", "verification": {
        "method": "auto_rule", "rule_id": "EV-AUTO-0.90-LOC", "by": MINER.id}}, expected_version=1, actor=MINER,
        patch_mode="merge_from_head")
    run = eng.run("context_change", org_id=ORG, evidence_refs=[v1])
    cr36 = [r for r in run.failed("CR-036")]
    assert cr36 and cr36[0].severity == "block"
    assert any(e["type"] == "calc.recompute_requested" for e in run.events)
    assert [_cfl(store, r)["rule_id"] for r in run.conflicts_written].count("CR-036") == 1


# ------------------------------------------------------------------ T-12
def test_t12_number_in_text(store, w, eng):
    a = w.answer("dp:gov.policy.code_of_conduct", "Wir haben unsere Emissionen um 18 % gesenkt", vt="text", consolidation=None)
    run = eng.run("answer_write", org_id=ORG, answer_refs=[a])
    cr37 = [_cfl(store, r) for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-037"]
    assert cr37 and cr37[0]["severity"] == "blocking" and cr37[0]["detail"]["unsourced_tokens"] == ["18"]


# ------------------------------------------------------------------ T-13
def test_t13_plugin_exception_fail_closed(store, w):
    rq = w.request(CP_A)
    qq = w.question(rq)
    w.answer(ELEC, "4820", "MWh", evidence=[w.evidence()], questions=[qq])

    def boom(ctx, scope):
        raise RuntimeError("plugin crashed")

    eng = ConsistencyEngine(store, plugins={"cr_003": boom})
    run = eng.gate(ORG, rq)
    assert run.gate == "unknown" and run.gate_blocks
    assert any(f["kind"] == "alert" and f["rule_id"] == "CR-003" for f in run.findings)
    assert eng.obs.runs[-1]["gate"] == "unknown"
    # 输入应存在却读不到 → 同样 unknown
    eng2 = ConsistencyEngine(store, plugins={"cr_003": lambda c, s: [Binding({}, ["ans_x#v1"], None, {}, error="unreadable")]})
    assert eng2.gate(ORG, rq).gate == "unknown"


# ------------------------------------------------------------------ T-14
def test_t14_value_chain_cap_info_only(store, w, eng):
    cp = w.counterparty(name="KMU-Kunde", tags=["fy_start:2027-01-01"])
    rq = w.request(cp, tags=["purpose:vsme"])
    qq = w.question(rq, 1, "Marktbasierte Emissionen")  # 非 Annex II（无 vsme 标签）的 active 数据点
    store.write("Mapping", {"question_id": qq, "datapoint_id": "dp:env.ghg.scope2_market_based@1.1.0", "confidence": 1,
                            "rank": 1, "method": "human", "status": "confirmed"}, actor=CO)
    run = eng.gate(ORG, rq)
    assert [r.severity for r in run.failed("CR-043")] == ["info"]
    assert not [r for r in run.conflicts_written if _cfl(store, r)["rule_id"] == "CR-043"]
    assert any(f["rule_id"] == "CR-043" for f in eng.obs.findings)


# ------------------------------------------------------------------ 有效期梯度（FR-404）
def test_expiry_ladder(store, w):
    days = {"d90": "2027-01-31", "d60": "2027-01-01", "d30": "2026-12-03", "d14": "2026-11-17", "d7": "2026-11-10",
            "expired": "2026-11-03"}
    refs = {t: w.evidence("1", "MWh", f"cert.{t}", period=None, valid_from="2025-01-01", valid_until=d) for t, d in days.items()}
    alerts = {a.subject_ref: a for a in scan_expiry(store, ORG, today="2026-11-03")}
    got = {t: alerts[r] for t, r in refs.items()}
    assert {t: (a.tier, a.level) for t, a in got.items()} == {
        "d90": ("d90", "info"), "d60": ("d60", "info"), "d30": ("d30", "warn"), "d14": ("d14", "warn"),
        "d7": ("d7", "warn"), "expired": ("expired", "block")}
    assert got["d7"].daily_repeat and "set_evidence_expired" in got["expired"].actions
    far = w.evidence("1", "MWh", "cert.far", period=None, valid_until="2028-01-01")
    assert far not in {a.subject_ref for a in scan_expiry(store, ORG, today="2026-11-03")}

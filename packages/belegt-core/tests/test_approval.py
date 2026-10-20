"""06 §4（风险判定表每一行、approval-policy@1.0.0、租户覆盖 floors）、§5（批量条件、上限、撤销窗口、T-11）、approval-router。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from belegt_core import Actor, GraphError
from belegt_core.approval import (ApprovalRouter, BatchStager, PolicyOverlayError, RiskInputs, add_workdays,
                                  check_batch, classify, compute_inputs, default_policy, load_policy, sla_due)
from belegt_core.approval.risk import fallback_decision
from belegt_core.consistency import ConsistencyEngine

from review_helpers import (ADMIN, CHASER, CO, CONTRIB, CP_A, CP_B, EXEC, ORG, Y24, World, ans, conflict,
                            ev, make_ctx, q, req)

P = default_policy()
ELEC = "dp:energy.consumption.electricity_total"


# ------------------------------------------------------------------ 策略
def test_policy_version_and_levels():
    assert P.version == "approval-policy@1.0.0"
    assert [P.max_items(x) for x in ("L0", "L1", "L2")] == [200, 100, 20] and not P.batch_allowed("L3")
    assert P.sla_workdays("L1") == 2 and P.undo_window_minutes == 10
    assert P.roles_for("L3") == ["compliance_owner", "executive"]
    assert P.roles_for("L3", legal_commitment=True, customer_tier="tier_a") == ["executive"]
    assert P.roles_for("L3", legal_commitment=True, customer_tier="tier_b") == ["compliance_owner", "executive"]
    assert P.non_answer("supplier_request_first_send")["default_level"] == "L1"


def test_overlay_dual_confirmation_and_floors():
    ok = load_policy({"thresholds": {"amount_l3_eur": "5000"}, "customer_tiers": {"tier_a": [CP_A]},
                      "batch_undo_window_minutes": 15, "levels": {"L0": {"batch": {"max_items": 50}}}}, 2,
                     approvers=[CO, ADMIN])
    assert ok.version == "approval-policy@1.0.0+ov2" and ok.threshold("amount_l3_eur") == Decimal("5000")
    assert ok.customer_tier([CP_A]) == "tier_a" and ok.undo_window_minutes == 15 and ok.max_items("L0") == 50
    with pytest.raises(PolicyOverlayError):  # 双人确认
        load_policy({"thresholds": {"amount_l3_eur": "5000"}}, 1, approvers=[CO])
    bad = [{"levels": {"L3": {"batch": {"max_items": 5}}}}, {"floors": []}, {"batch_undo_window_minutes": 90},
           {"levels": {"L1": {"roles": ["contributor"]}}}, {"special": [{"when": "true", "roles": ["sales"]}]},
           {"non_answer_subjects": {}}]
    for ov in bad:
        with pytest.raises(PolicyOverlayError) as e:
            load_policy(ov, 1, approvers=[CO, ADMIN])
        assert e.value.details["alert"] == "org_admin"


# ------------------------------------------------------------------ 判定表每一行
@pytest.mark.parametrize("inputs,level,rule", [
    (RiskInputs(open_blocking=True, commitment=True), None, "RISK-00"),
    (RiskInputs(commitment=True, source="evidence"), "L3", "RISK-01"),
    (RiskInputs(accepted_divergence=True), "L3", "RISK-02"),
    (RiskInputs(cross_customer=True), "L3", "RISK-02"),
    (RiskInputs(first_submission=True, emission=True), "L3", "RISK-03"),
    (RiskInputs(restricted=True, value_type="geo"), "L3", "RISK-04"),
    (RiskInputs(amount_eur=Decimal("10000")), "L3", "RISK-05"),
    (RiskInputs(source="calculation"), "L2", "RISK-06"),
    (RiskInputs(source="drafted_text", value_type="text"), "L2", "RISK-06"),
    (RiskInputs(is_estimate=True), "L2", "RISK-06"),
    (RiskInputs(open_warn=True), "L2", "RISK-06"),
    (RiskInputs(amount_eur=Decimal("1000")), "L2", "RISK-07"),
    (RiskInputs(customer_tier="tier_a", value_type="number"), "L2", "RISK-07"),
    (RiskInputs(source="reused", reuse_ok=True), "L0", "RISK-08"),
    (RiskInputs(source="evidence"), "L1", "RISK-09"),
    (RiskInputs(source="evidence_unverified"), "L2", "RISK-10"),
    (RiskInputs(source="reused", reuse_ok=False), "L2", "RISK-10"),
])
def test_risk_table_rows(inputs, level, rule):
    d = classify(inputs, P)
    assert (d.level, d.rule_id) == (level, rule)
    assert d.blocked is (level is None)


def test_risk_edges_and_floors():
    assert classify(RiskInputs(amount_eur=Decimal("9999.99")), P).level == "L2"  # < €10,000 → RISK-07
    assert classify(RiskInputs(customer_tier="tier_a", value_type="bool"), P).rule_id == "RISK-09"
    assert classify(RiskInputs(first_submission=True, emission=True, value_type="text", source="drafted_text"), P).level == "L2"
    d = classify(RiskInputs(source="reused", reuse_ok=True, legal_commitment=True, commitment=True), P)
    assert d.level == "L3" and "legal_commitment => L3" in d.floors
    assert fallback_decision("policy down").level == "L3"
    ov = load_policy({"thresholds": {"amount_l3_eur": "500"}}, 1, approvers=[CO, ADMIN])
    assert classify(RiskInputs(amount_eur=Decimal("600")), ov).level == "L3"


def test_compute_inputs_dimensions():
    rq = req()
    qq = q(rq, tags=["legal:LC-06"])
    e = ev("4820", "MWh")
    hist = ans(ELEC, "4820", "MWh", period=Y24, status="submitted", evidence=[e])
    cur = ans(ELEC, "4820", "MWh", evidence=[e], status="draft", reused_from=f"{hist['id']}#v1")
    ctx = make_ctx([rq, qq, e, hist, cur])
    i = compute_inputs(ctx, cur, P)
    assert i.source == "reused" and not i.first_submission and not i.emission
    s1 = ans("dp:env.ghg.scope1_total", "10", "tCO2e", evidence=[e], status="draft")
    i2 = compute_inputs(make_ctx([e, s1]), s1, P)
    assert i2.first_submission and i2.emission and classify(i2, P).level == "L3"
    legal = ans("dp:gov.policy.code_of_conduct", "ja", vt="text", consolidation=None, questions=[qq], status="draft")
    assert compute_inputs(make_ctx([rq, qq, legal]), legal, P).commitment
    tgt = ans("dp:env.ghg_target.has_target", True, vt="bool", consolidation=None, status="draft")
    assert classify(compute_inputs(make_ctx([tgt]), tgt, P), P).rule_id == "RISK-01"
    geo = ans("dp:supply.eudr.geolocation", {"type": "Point"}, vt="geo", consolidation=None, status="draft")
    assert classify(compute_inputs(make_ctx([geo]), geo, P), P).rule_id == "RISK-04"
    c = conflict("CR-002", [cur], severity="warning")
    assert compute_inputs(make_ctx([rq, qq, e, hist, cur, c]), cur, P).open_warn


def test_sla_workdays_and_cap():
    fri = dt.date(2026, 11, 6)
    assert add_workdays(fri, 1) == dt.date(2026, 11, 9)
    assert sla_due(P, "L3", fri, None) == dt.date(2026, 11, 11)
    assert sla_due(P, "L3", fri, "2026-11-10") == dt.date(2026, 11, 8)  # 不晚于 due_date − 2 天


# ------------------------------------------------------------------ 批量条件（内存）
def _pending(answer, level, request=None, roles=("compliance_owner",)):
    return {"id": f"apr_{answer['id'][4:]}", "org_id": ORG, "version": 1, "created_at": "2026-11-01T08:00:00Z",
            "subject_ref": f"{answer['id']}#v{answer['version']}", "risk_level": level, "required_role": list(roles),
            "policy_version": P.version, "status": "pending", **({"request_id": request["id"]} if request else {})}


def test_batch_conditions_in_memory():
    rq = req(due="2026-12-15")
    qs = [q(rq, i) for i in range(1, 4)]
    e = ev("1", "MWh", valid_until="2027-06-30")
    a0 = ans(ELEC, "1", "MWh", evidence=[e], questions=[qs[0]], status="in_review", risk_level="L0")
    a1 = ans(ELEC, "1", "MWh", evidence=[e], questions=[qs[1]], status="in_review", risk_level="L1", cs=[CP_B])
    a2 = ans(ELEC, "1", "MWh", evidence=[e], questions=[qs[2]], status="in_review", risk_level="L2", cs=[CP_A],
             is_estimate=True, estimate_method="x")
    aprs = [_pending(a0, "L0", rq), _pending(a1, "L1", rq), _pending(a2, "L2", rq)]
    base = [rq, e, a0, a1, a2] + qs + aprs
    chk = check_batch(make_ctx(base), [p["id"] for p in aprs], CO, P)
    assert not chk.ok
    l2 = next(i for i in chk.items if i.risk_level == "L2")
    assert any("detail view" in r for r in l2.reasons) and any("estimate_ack" in r for r in l2.reasons)
    chk2 = check_batch(make_ctx(base), [p["id"] for p in aprs], CO, P, viewed=[aprs[2]["id"]],
                       confirmations={"estimate_ack": True})
    assert chk2.ok and chk2.summary["levels"] == {"L0": 1, "L1": 1, "L2": 1} and chk2.summary["estimates"] == 1
    # 角色：executive 不能批 L0–L2
    assert not check_batch(make_ctx(base), [aprs[0]["id"]], EXEC, P).ok
    # L0/L1 有 warn 冲突不得入批；L2 有 warn 需处置说明
    warn0 = conflict("CR-002", [a0], severity="warning")
    assert not check_batch(make_ctx(base + [warn0]), [aprs[0]["id"]], CO, P).ok
    warn2 = conflict("CR-002", [a2], severity="warning")
    assert not check_batch(make_ctx(base + [warn2]), [aprs[2]["id"]], CO, P, viewed=[aprs[2]["id"]],
                           confirmations={"estimate_ack": True}).ok
    assert check_batch(make_ctx(base + [warn2]), [aprs[2]["id"]], CO, P, viewed=[aprs[2]["id"]],
                       confirmations={"estimate_ack": True, "warn_note": "Produktionsanstieg"}).ok
    # 证据在 due_date 时已失效（CR-004）
    e_short = {**e, "valid_until": "2026-12-01"}
    assert not check_batch(make_ctx([rq, e_short, a0, qs[0], aprs[0]]), [aprs[0]["id"]], CO, P).ok
    # 钉住版本变化（条件 4）
    a0v2 = {**a0, "version": 2}
    assert not check_batch(make_ctx(base + [a0v2]), [aprs[0]["id"]], CO, P).ok
    # L3 永不入批
    a3 = {**a1, "id": "ans_01JC2B3C4D5E6F7G8H9J0K1M3A", "risk_level": "L3"}
    assert not check_batch(make_ctx(base + [a3, _pending(a3, "L3", rq)]), [_pending(a3, "L3")["id"]], CO, P).ok


def test_batch_limits():
    rq = req()
    e = ev("1", "MWh")
    objs, ids = [rq, e], []
    for i in range(21):
        qq = q(rq, i + 1)
        a = ans(ELEC, "1", "MWh", evidence=[e], questions=[qq], status="in_review", risk_level="L2",
                period=(f"{2000 + i}-01-01", f"{2000 + i}-12-31"))
        p = _pending(a, "L2", rq)
        objs += [qq, a, p]
        ids.append(p["id"])
    chk = check_batch(make_ctx(objs), ids, CO, P, viewed=ids)
    assert not chk.ok and any("exceeds max 20" in r for r in chk.reasons)
    assert check_batch(make_ctx(objs), ids[:20], CO, P, viewed=ids).ok
    rq2 = req()
    q2 = q(rq2, 1)
    a_other = ans(ELEC, "1", "MWh", evidence=[e], questions=[q2], status="in_review", risk_level="L2", cs=[CP_B])
    p2 = _pending(a_other, "L2", rq2)
    chk3 = check_batch(make_ctx(objs + [rq2, q2, a_other, p2]), ids[:2] + [p2["id"]], CO, P, viewed=ids + [p2["id"]])
    assert any("same Request" in r for r in chk3.reasons)


# ------------------------------------------------------------------ 图上：路由、单条裁决、批量暂存与撤销窗口
class Clock:
    def __init__(self, t: str = "2026-11-03T09:00:00+00:00") -> None:
        self.t = dt.datetime.fromisoformat(t)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += dt.timedelta(**kw)


def _l1_answers(w, n: int, due: str = "2026-12-15"):
    rq = w.request(CP_A, due=due)
    refs = []
    for i in range(n):
        cp = w.counterparty(name=f"Kunde {i}")
        qq = w.question(rq, i + 1)
        refs.append(w.answer(ELEC, "4820", "MWh", evidence=[w.evidence()], questions=[qq], cs=[cp]))
    return rq, refs


def test_route_l1_and_single_decisions(store):
    w = World(store)
    rq, refs = _l1_answers(w, 3)
    router = ApprovalRouter(store)
    res = router.route(ORG, request_id=rq)
    assert [r.risk_level for r in res.routed] == ["L1"] * 3 and all(r.rule_id == "RISK-09" for r in res.routed)
    apr = store.get(res.routed[0].approval_ref.split("#")[0])
    assert apr["policy_version"] == "approval-policy@1.0.0" and apr["request_id"] == rq and apr["required_role"] == ["compliance_owner"]
    with pytest.raises(GraphError):
        router.decide(apr["id"], actor=CONTRIB, decision="approved")
    router.decide(apr["id"], actor=CO, decision="approved")
    a = store.get(res.routed[0].answer_ref.split("#")[0])
    assert a["status"] == "approved" and a["approval_ref"] == f"{apr['id']}#v2"
    # rejected
    apr2 = res.routed[1].approval_ref.split("#")[0]
    router.decide(apr2, actor=CO, decision="rejected", comment="falsche Quelle")
    assert store.get(res.routed[1].answer_ref.split("#")[0])["status"] == "rejected"
    # modified：先写 human_confirmation Evidence（R-NUM），Answer 回到 draft
    apr3 = res.routed[2].approval_ref.split("#")[0]
    with pytest.raises(GraphError) as e:
        router.decide(apr3, actor=CO, decision="modified", modification={"value": "4900"})
    assert e.value.code == "GRAPH_NUM_UNREFERENCED"
    router.decide(apr3, actor=CO, decision="modified",
                        modification={"source": "human_confirmation", "value": "4900", "note": "Korrektur laut Zähler"})
    a3 = store.get(res.routed[2].answer_ref.split("#")[0])
    assert a3["status"] == "draft" and a3["value"] == "4900"
    assert store.get(apr3)["modified_answer_ref"] == f"{a3['id']}#v{a3['version']}"
    # 新版本 → 重新路由
    assert router.route(ORG, answer_refs=[f"{a3['id']}#v{a3['version']}"]).routed


def test_l3_requires_confirmations_and_stale(store):
    w = World(store)
    rq = w.request(CP_A)
    qq = w.question(rq)
    s1 = w.answer("dp:env.ghg.scope1_total", "1820", "tCO2e", evidence=[w.evidence("1820", "tCO2e", "a.s1")], questions=[qq])
    router = ApprovalRouter(store)
    r = router.route(ORG, answer_refs=[s1]).routed[0]
    assert r.risk_level == "L3" and r.rule_id == "RISK-03" and r.required_role == ["compliance_owner", "executive"]
    aid = r.approval_ref.split("#")[0]
    with pytest.raises(GraphError) as e:
        router.decide(aid, actor=EXEC, decision="approved", confirmations={"step_up_auth": True})
    assert e.value.details["missing"] == ["clause_read_ack"]
    # stale：Answer 在审批期间出新版本 → 作废重建
    store.write("Answer", {"id": s1.split("#")[0], "tags": ["edited"]}, expected_version=2,
                actor=Actor.of("agent:approval-router", ORG), patch_mode="merge_from_head")
    with pytest.raises(GraphError):
        router.decide(aid, actor=EXEC, decision="approved", confirmations={"step_up_auth": True, "clause_read_ack": True})
    old = store.get(aid)
    assert old["status"] == "rejected" and old["comment"] == "stale"


def test_legal_tier_a_only_executive(store):
    w = World(store)
    pol = load_policy({"customer_tiers": {"tier_a": [CP_A]}}, 1, approvers=[CO, ADMIN])
    rq = w.request(CP_A)
    qq = w.question(rq, 1, "Der Lieferant sichert zu, dass …", tags=["legal_commitment", "legal:LC-01"])
    a = w.answer("dp:gov.policy.code_of_conduct", "Zustimmung", vt="text", consolidation=None, questions=[qq], cs=[CP_A])
    router = ApprovalRouter(store, pol)
    r = router.route(ORG, answer_refs=[a]).routed[0]
    assert r.required_role == ["executive"] and store.get(r.approval_ref.split("#")[0])["tags"] == ["policy_overlay:ov1"]
    with pytest.raises(GraphError):
        router.decide(r.approval_ref.split("#")[0], actor=CO, decision="approved",
                      confirmations={"step_up_auth": True, "clause_read_ack": True})


def test_t11_batch_voided_when_answer_changes(store):
    w = World(store)
    rq, refs = _l1_answers(w, 30)
    clock = Clock()
    eng = ConsistencyEngine(store)
    router = ApprovalRouter(store)
    routed = router.route(ORG, request_id=rq).routed
    assert len(routed) == 30 and {r.risk_level for r in routed} == {"L1"}
    stager = BatchStager(store, router, eng, clock=clock)
    b = stager.stage(CO, [r.approval_ref.split("#")[0] for r in routed])
    assert b.status == "staged" and b.batch_id.startswith("apb_") and len(b.items) == 30
    assert all(store.get(r.approval_ref.split("#")[0])["status"] == "pending" for r in routed)  # 窗口内仍 pending
    target = routed[7].answer_ref.split("#")[0]
    store.write("Answer", {"id": target, "tags": ["late-edit"]}, expected_version=store.head(target).version,
                actor=Actor.of("agent:approval-router", ORG), patch_mode="merge_from_head")
    clock.advance(minutes=9)
    assert stager.settle() == []
    clock.advance(minutes=2)
    settled = stager.settle()
    assert settled and settled[0].status == "voided" and any("changed" in r for r in settled[0].reasons)
    assert all(store.get(r.approval_ref.split("#")[0])["status"] in ("pending", "rejected") for r in routed)
    ev = [e for e in store.audit_events(ORG) if (e.get("details") or {}).get("event") == "approval.batch.void"]
    assert ev and ev[-1]["details"]["notify"] == CO.id


def test_batch_undo_window_and_commit(store):
    w = World(store)
    rq, refs = _l1_answers(w, 3)
    clock = Clock()
    router = ApprovalRouter(store)
    routed = router.route(ORG, request_id=rq).routed
    ids = [r.approval_ref.split("#")[0] for r in routed]
    stager = BatchStager(store, router, ConsistencyEngine(store), clock=clock)
    b = stager.stage(CO, ids)
    clock.advance(minutes=5)
    stager.undo(CO, b.batch_id, [ids[0]])  # 剔除一条
    clock.advance(minutes=6)
    stager.settle()
    assert b.status == "committed"
    assert store.get(ids[0])["status"] == "pending"
    for aid in ids[1:]:
        apr = store.get(aid)
        assert apr["status"] == "approved" and apr["batch_id"] == b.batch_id
        assert store.get(apr["subject_ref"].split("#")[0])["status"] == "approved"
    with pytest.raises(GraphError):  # 窗口结束后不能撤销
        stager.undo(CO, b.batch_id)
    events = [(e.get("details") or {}).get("event") for e in store.audit_events(ORG)]
    assert {"approval.batch.stage", "approval.batch.undo", "approval.batch.commit"} <= set(events)
    # 整批撤销
    b2 = stager.stage(CO, [ids[0]])
    stager.undo(CO, b2.batch_id)
    assert b2.status == "undone"
    clock.advance(minutes=11)
    stager.settle()
    assert store.get(ids[0])["status"] == "pending"


def test_batch_immediate_only_near_due(store):
    w = World(store)
    rq, refs = _l1_answers(w, 1, due="2026-12-15")
    router = ApprovalRouter(store)
    aid = router.route(ORG, request_id=rq).routed[0].approval_ref.split("#")[0]
    stager = BatchStager(store, router, clock=Clock("2026-11-03T09:00:00+00:00"))
    with pytest.raises(GraphError):
        stager.stage(CO, [aid], immediate=True)
    near = BatchStager(store, router, clock=Clock("2026-12-15T23:55:00+00:00"))
    b = near.stage(CO, [aid], immediate=True)
    assert b.status == "committed" and store.get(aid)["status"] == "approved"


def test_supplier_first_send_approval(store):
    w = World(store)
    sup = w.counterparty(name="Stahl GmbH", role="supplier")
    sup_a = w.counterparty(name="Guss AG", role="supplier", tags=["supplier_tier:A"])
    router = ApprovalRouter(store)

    def sreq(cp):
        return store.write("SupplierRequest", {"counterparty_id": cp, "datapoint_ids": ["dp:prod.cbam.see_direct@1.0.0"],
                                               "period": {"start": "2025-01-01", "end": "2025-12-31"}, "language": "de",
                                               "template_id": "stpl:cbam_see_request.de@1.0.0", "channel": "email",
                                               "due_date": "2026-12-01", "reminder_count": 0, "status": "draft"},
                           actor=CHASER).split("#")[0]

    s1, s2 = sreq(sup), sreq(sup_a)
    pend = router.route_supplier_request(ORG, s1)
    assert store.get(pend.split("#")[0])["risk_level"] == "L1"
    with pytest.raises(GraphError):
        router.decide_supplier_first_send(s1, actor=EXEC)
    out = router.decide_supplier_first_send(s1, actor=CO)
    apr = store.get(out["approval_ref"].split("#")[0])
    assert apr["status"] == "approved" and apr["subject_ref"] == f"{s1}#v1" and apr["id"] == pend.split("#")[0]
    out2 = router.decide_supplier_first_send(s2, actor=CO)
    assert out2["risk_level"] == "L2" and out2["required_role"] == ["compliance_owner"] and out2["escalation"] == ["supplier_tier == 'A'"]


def test_escalations_never_auto_approve(store, clock):
    w = World(store)
    rq, refs = _l1_answers(w, 1, due=None)
    router = ApprovalRouter(store)
    router.route(ORG, request_id=rq)
    assert router.escalations(ORG) == []
    rem = router.escalations(ORG, now=dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc))
    assert rem and rem[0]["action"] == "remind" and rem[0]["auto_approve"] is False
    esc = router.escalations(ORG, now=dt.datetime(2026, 11, 12, tzinfo=dt.timezone.utc))
    assert esc[0]["action"] == "escalate" and esc[0]["to"] == ["executive"]


def test_policy_unavailable_routes_l3(store):
    w = World(store)
    rq, refs = _l1_answers(w, 1)
    r = ApprovalRouter(store, policy_available=False).route(ORG, request_id=rq).routed[0]
    assert r.risk_level == "L3" and r.rule_id == "POLICY-UNAVAILABLE"

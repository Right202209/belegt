# Spec: 04 §2.4 evidence-miner（带定位 Evidence；EV-AUTO-0.90-LOC 两路一致 / EV-AUTO-ERP-ATTEST；否则 numeric_confirm；
#       历史答卷 → L0 复用；缺口清单；连接器失效降级）、§2.5 calc-service（唯一数字通道；输入缺失 → gaps；
#       CALC_PARAM_REQUIRES_DECISION → ClosedQuestion(single_choice)，作答后写回 params）、05 §8.1
from __future__ import annotations

from decimal import Decimal

import pytest

from agentkit import drive_to, new_env, upload_file
from belegt_core.agents import CalcService, EvidenceMiner
from belegt_core.agents.base import latest_ref
from belegt_core.agents.facts import CALC_RECIPES, Recipe
from belegt_core.llm.mock import MockLLM

P25 = {"start": "2025-01-01", "end": "2025-12-31"}


def _t(env, dp: str, boundary: str = "organization", **kw):
    return {"datapoint_id": latest_ref(env.svc.registry, dp), "period": P25, "boundary": boundary, "question_ids": [],
            "question_refs": [], **kw}


@pytest.fixture
def env_req():
    env = new_env()
    return env, upload_file(env)


# ---------------------------------------------------------------------------- evidence-miner
def test_document_evidence_two_path_auto_verified_and_shared(env_req):
    env, ref = env_req
    out = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:energy.consumption.electricity_total")], idem_prefix="t:m1")
    (er,) = out.refs
    e = env.store.get(er)
    assert e["status"] == "verified" and e["source_type"] == "document" and e["trust_zone"] == "trusted"
    assert e["verification"]["rule_id"] == "EV-AUTO-0.90-LOC" and e["locator"]["page"] >= 1
    assert e["extracted_value"] == "4820000" and e["unit"] == "kWh" and out.cq_refs == []
    # 03 §3.4 证据共享：另一个 Activity 再挖同一事实 → 复用已核验证据，不写新版本
    out2 = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:energy.consumption.electricity_total")], idem_prefix="t:m2")
    assert out2.refs == [er] and env.store.head(er.split("#")[0]).version == 2


def test_erp_attested_and_doc_needing_confirmation(env_req):
    env, ref = env_req
    out = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:env.ghg.scope1_total")], idem_prefix="t:m3")
    evs = {env.store.get(r)["fact_key"]: env.store.get(r) for r in out.refs}
    diesel, gas = evs["activity.diesel.volume"], evs["activity.natural_gas.hi"]
    assert diesel["source_type"] == "erp" and diesel["status"] == "verified"
    assert diesel["verification"]["rule_id"] == "EV-AUTO-ERP-ATTEST"
    assert gas["status"] == "candidate" and len(out.cq_refs) == 1
    cq = env.store.get(out.cq_refs[0])
    assert cq["kind"] == "numeric_confirm" and cq["target_ref"].startswith(gas["id"])
    assert cq["proposed_value"] == {"value": gas["extracted_value"], "unit": gas["unit"], "evidence_ref": f"{gas['id']}#v1"}
    assert cq["created_by"] == "agent:evidence-miner"


def test_llm_hallucinated_value_never_enters_graph(env_req):
    """路径 B（LLM）给出原文里没有的数 → 两路不一致 → 不自动核验；候选值与卡片建议值都来自路径 A（版面解析）。"""
    env, _ = env_req
    llm = MockLLM(script={"extract_fields": {"value_text": "9999999", "unit_text": "kWh", "page": 1, "quote": "Wirkarbeit"}})
    env.svc.llm = llm
    ref = upload_file(env)
    out = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:energy.consumption.electricity_total")], idem_prefix="t:m4")
    (er,) = out.refs
    e = env.store.get(er)
    assert e["status"] == "candidate" and e["extracted_value"] == "4820000"
    cq = env.store.get(out.cq_refs[0])
    assert cq["proposed_value"]["value"] == "4820000"
    assert not any("9999999" in str(env.store.get(x["id"])) for x in env.store.query("Evidence", org_id=env.org_id))


def test_history_reuse_and_supplier_gap(env_req):
    env, ref = env_req
    out = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:custom.org.address"), _t(env, "dp:prod.cbam.see_direct", "product_per_unit")],
                                     idem_prefix="t:m5")
    per = {d["datapoint_id"].split("@")[0]: d for d in out.details["per_datapoint"]}
    assert per["dp:custom.org.address"]["reuse"] and per["dp:custom.org.address"]["evidence"] == []
    assert env.store.get(per["dp:custom.org.address"]["reuse"][0])["status"] == "submitted"
    (gap,) = out.gaps
    assert gap["datapoint_id"].startswith("dp:prod.cbam.see_direct") and gap["kind"] == "supplier"


def test_erp_connector_down_degrades(env_req, monkeypatch):
    env, ref = env_req

    def down(*a, **k):
        raise ConnectionError("erp offline")
    monkeypatch.setattr(env.svc.connectors["erp"], "call", down)
    out = EvidenceMiner(env.svc).run(ref, [_t(env, "dp:env.ghg.scope1_total")], idem_prefix="t:m6")
    assert out.details.get("degraded") is True
    assert any(g.get("fact_key") == "activity.diesel.volume" for g in out.gaps)  # 柴油只有 ERP 来源 → 缺口
    gas = [env.store.get(r) for r in out.refs if env.store.get(r)["fact_key"] == "activity.natural_gas.hi"]
    assert gas and "freshness:degraded" in gas[0]["tags"]


# ---------------------------------------------------------------------------- calc-service
def test_calc_direct_evidence_answer_with_unit_conversion(env_req):
    env, ref = env_req
    EvidenceMiner(env.svc).run(ref, [_t(env, "dp:energy.consumption.electricity_total")], idem_prefix="t:c1m")
    out = CalcService(env.svc).run(ref, [_t(env, "dp:energy.consumption.electricity_total")], idem_prefix="t:c1")
    (ar,) = out.refs
    a = env.store.get(ar)
    assert a["created_by"] == "agent:calc-service" and a["value_type"] == "number"
    assert a["unit"] == "MWh" and Decimal(a["value"]) == Decimal("4820") and len(a["evidence_refs"]) == 1
    assert not [c for c in env.svc.llm.calls if c.agent == "calc-service"]  # 无 LLM


def test_calc_missing_inputs_returns_gaps(env_req):
    env, ref = env_req
    out = CalcService(env.svc).run(ref, [_t(env, "dp:env.ghg.scope2_location_based"), _t(env, "dp:custom.org.address")],
                                   idem_prefix="t:c2")
    assert out.refs == [] and out.cq_refs == []
    assert {g["fact_key"] for g in out.gaps} == {"activity.electricity.purchased"}  # 文本类目标被忽略


def test_calc_param_requires_decision_becomes_closed_question(monkeypatch):
    """配方不再预置 consolidation / gwp_set → belegt-calc 抛 CALC_PARAM_REQUIRES_DECISION（CQ-METH-03、CQ-METH-02）→
    calc-service 出 single_choice 卡（合法选项 2–4 项）→ 人选择 → 写回 params → 计算完成。"""
    r0 = CALC_RECIPES["dp:env.ghg.scope1_total"]
    monkeypatch.setitem(CALC_RECIPES, "dp:env.ghg.scope1_total",
                        Recipe(r0.method_id, r0.methodology_version, r0.factor_set, r0.inputs, params={}, decisions={}))
    env = new_env()
    ref = upload_file(env)
    res, trail = drive_to(env, ref)
    assert res.state == "completed", (res.state, res.error)
    st, org = env.store, env.org_id
    dec = {}
    for cq in st.query("ClosedQuestion", org_id=org):
        for tag in cq.get("tags") or []:
            if tag.startswith("decision:") and tag.endswith("dp:env.ghg.scope1_total"):
                dec[tag.split(":")[1]] = cq
    assert set(dec) == {"CQ-METH-02", "CQ-METH-03"}
    m3 = dec["CQ-METH-03"]
    assert m3["kind"] == "single_choice" and m3["status"] == "answered"
    assert st.get(m3["id"], version=1)["created_by"] == "agent:calc-service"
    assert [o["key"] for o in m3["options"]] == ["operational_control", "financial_control", "equity_share"]
    assert [o["key"] for o in dec["CQ-METH-02"]["options"]] == ["ar6", "ar5"]
    calc = next(c for c in st.query("Calculation", org_id=org) if c["method_id"] == "ghg.scope1.total")
    assert calc["params"]["consolidation"] == "operational_control" and calc["params"]["gwp_set"] == "ar6"
    assert Decimal(calc["result"]["value"]) == Decimal("481.92")


def test_calc_unregistered_decision_is_a_gap_not_an_invalid_card(env_req, monkeypatch):
    from belegt_core.agents import facts

    env, ref = env_req
    r0 = CALC_RECIPES["dp:env.ghg.scope1_total"]
    monkeypatch.setitem(CALC_RECIPES, "dp:env.ghg.scope1_total",
                        Recipe(r0.method_id, r0.methodology_version, r0.factor_set, r0.inputs, params={}, decisions={}))
    monkeypatch.setattr("belegt_core.agents.calc_service.DECISION_OPTIONS", {})
    assert facts.DECISION_OPTIONS  # 只在本测试里清空
    t = _t(env, "dp:env.ghg.scope1_total")
    EvidenceMiner(env.svc).run(ref, [t], idem_prefix="t:c4m")
    owner = env.user("owner")
    for cq in env.store.query("ClosedQuestion", org_id=env.org_id, status="open"):  # 人核验天然气证据
        env.store.write("ClosedQuestion", {"id": cq["id"], "status": "answered", "answer": {
            "bool": True, "answered_by": owner.id, "answered_at": env.store.peek_now()[:19] + "Z"}},
            expected_version=cq["version"], actor=owner, patch_mode="merge_from_head")
    out = CalcService(env.svc).run(ref, [t], idem_prefix="t:c4")
    assert out.cq_refs == [] and out.refs == []
    assert out.gaps and out.gaps[0]["error"] == "CALC_PARAM_REQUIRES_DECISION"

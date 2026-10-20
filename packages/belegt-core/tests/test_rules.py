"""03 §2.3 写入校验规则：每条规则至少一正一反。"""
from __future__ import annotations

import copy

import pytest

from belegt_core import GraphError, WriteItem, new_id
from belegt_core.canonical import hash_obj

from helpers import (
    ADMIN, AUDITOR_AGENT, CALC, CO, CONTRIB, CP, DRAFTER, EXEC, FILLER, FIX, GUARD, INTAKE, MINER, ORG, ORG_B, PARSER,
    ROUTER, WORKFLOW, A, doc, strip_env,
)

ANS = FIX["answer"]["id"]
CALC_ID = FIX["calculation"]["id"]
EV1 = FIX["evidence_bill"]["id"]
EV2 = FIX["evidence_hkn"]["id"]
Q = FIX["question"]["id"]
REQ = FIX["request"]["id"]
P2025 = {"start": "2025-01-01", "end": "2025-12-31"}
DP = "dp:env.ghg.scope2_market_based@1.1.0"


def err(fn, *a, **kw) -> GraphError:
    with pytest.raises(GraphError) as e:
        fn(*a, **kw)
    return e.value


def answer_body(**over) -> dict:
    b = {"id": new_id("Answer"), "datapoint_id": DP, "value": "2027.2", "value_type": "number", "unit": "tCO2e",
         "period": dict(P2025), "boundary": "organization", "consolidation": "operational_control",
         "customer_scope": [CP], "question_refs": [Q], "evidence_refs": [f"{EV1}#v2", f"{EV2}#v2"],
         "calculation_ref": f"{CALC_ID}#v1", "origin": "generated", "status": "draft", "is_estimate": False,
         "legal_commitment": False}
    b.update(over)
    return b


def evidence_body(**over) -> dict:
    b = {"id": new_id("Evidence"), "source_type": "document", "fact_key": "activity.electricity.purchased",
         "extracted_value": "4820", "value_type": "number", "unit": "MWh", "period": dict(P2025),
         "boundary": "organization",
         "locator": {"blob_sha256": "sha256:" + "a" * 64, "mime": "application/pdf", "page": 1},
         "valid_from": "2025-01-01", "valid_until": "2025-12-31", "confidence": 0.95, "status": "candidate"}
    b.update(over)
    return b


def approve(store, ans_id: str, risk="L1", approver=CO, required=("compliance_owner",), batch_id=None):
    """draft → in_review → Approval(pending→approved) → approved。返回 approved ref。"""
    h = store.head(ans_id)
    store.write("Answer", {"id": ans_id, "status": "in_review", "risk_level": risk}, expected_version=h.version,
                actor=ROUTER, patch_mode="merge_from_head")
    v = h.version + 1
    apr = {"id": new_id("Approval"), "subject_ref": f"{ans_id}#v{v}", "risk_level": risk, "required_role": list(required),
           "policy_version": "approval-policy@1.0.0", "status": "pending"}
    store.write("Approval", apr, actor=ROUTER)
    dec = {"id": apr["id"], "status": "approved", "decision": "approved", "decided_by": approver.id,
           "decided_at": "2026-11-04T07:45:10Z"}
    if batch_id:
        dec["batch_id"] = batch_id
    store.write("Approval", dec, expected_version=1, actor=approver, patch_mode="merge_from_head")
    return store.write("Answer", {"id": ans_id, "status": "approved", "approval_ref": f"{apr['id']}#v2"},
                       expected_version=v, actor=ROUTER, patch_mode="merge_from_head")


# ---------------------------------------------------------------- R-SCHEMA
def test_r_schema(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CALC)  # 正
    bad = answer_body()
    bad.pop("origin")
    e = err(store.write, "Answer", bad, actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.rule_id == "R-SCHEMA"
    bad2 = answer_body(boundary="plant-7")
    assert err(store.write, "Answer", bad2, actor=CALC).code == "GRAPH_SCHEMA_INVALID"


def test_r_schema_additional_properties(store, chain):
    e = err(store.write, "Answer", answer_body(foo="bar"), actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID"


# ---------------------------------------------------------------- R-IMM
def test_r_imm(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CALC)
    e = err(store.write, "Answer", {**b, "value": "2027.2"}, expected_version=7, actor=CALC)
    assert e.code == "GRAPH_VERSION_CONFLICT" and e.http_status == 409
    e = err(store.write, "Answer", b, actor=CALC)  # 已存在但未带 expected_version
    assert e.code == "GRAPH_VERSION_CONFLICT"
    ref = store.write("Answer", {**b, "drafting_note": {"en": "Market-based."}}, expected_version=1, actor=CALC)
    assert ref.endswith("#v2")
    assert store.get(b["id"], 1)["status"] == "draft"  # 旧版本保留（append-only）


# ---------------------------------------------------------------- R-ZONE
def test_r_zone_quarantine_actor_cannot_write_trusted(store, chain):
    qb = strip_env(doc("question"))
    qb.update(id=new_id("Question"), ordinal=99, status="parsed", trust_zone="trusted")
    qb.pop("supersedes")
    e = err(store.write, "Question", qb, actor=PARSER)
    assert e.code == "GRAPH_TRUST_ZONE_VIOLATION"
    qb["trust_zone"] = "quarantine"
    assert store.write("Question", qb, actor=PARSER).endswith("#v1")


def test_r_zone_calc_input_must_be_trusted_verified(store, chain):
    ev = evidence_body()
    store.write("Evidence", ev, actor=MINER)  # candidate
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["inputs"][1]["ref"] = f"{ev['id']}#v1"
    e = err(store.write, "Calculation", c, actor=CALC)
    assert e.code == "GRAPH_TRUST_ZONE_VIOLATION" and e.path == "/inputs/1/ref"


def test_r_zone_answer_cannot_cite_quarantine(store, chain):
    sreq_free = evidence_body(source_type="document", trust_zone="quarantine")
    # 用 supplier_user（隔离区人）写入 quarantine 证据不合适，改由 evidence-miner 写 quarantine 候选
    store.write("Evidence", sreq_free, actor=MINER)
    b = answer_body(evidence_refs=[f"{sreq_free['id']}#v1"], calculation_ref=None)
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_TRUST_ZONE_VIOLATION"


# ---------------------------------------------------------------- R-PROMO
def _quarantine_mapping(store, **over):
    m = {"id": new_id("Mapping"), "question_id": Q, "datapoint_id": DP, "confidence": 0.91, "rank": 1,
         "method": "semantic", "status": "candidate", "trust_zone": "quarantine"}
    m.update(over)
    store.write("Mapping", m, actor=PARSER)
    return m


def test_r_promo_guard_ok_and_value_change_denied(store, chain):
    m = _quarantine_mapping(store)
    promo = {"rule": "PROMO-MAP-0.85", "schema_version": "belegt.graph/2026-10", "checked_at": "2026-11-03T08:20:00Z"}
    e = err(store.write, "Mapping", {**m, "trust_zone": "trusted", "promotion": promo, "confidence": 0.99},
            expected_version=1, actor=GUARD)
    assert e.code == "GRAPH_PROMOTION_DENIED" and "confidence" in e.details["changed_fields"]
    e = err(store.write, "Mapping", {**m, "trust_zone": "trusted"}, expected_version=1, actor=GUARD)
    assert e.code == "GRAPH_PROMOTION_DENIED"  # 缺 promotion
    assert store.write("Mapping", {**m, "trust_zone": "trusted", "promotion": promo}, expected_version=1,
                       actor=GUARD).endswith("#v2")


def test_r_promo_non_guard_denied(store, chain):
    m = _quarantine_mapping(store)
    promo = {"rule": "PROMO-MAP-0.85", "schema_version": "belegt.graph/2026-10", "checked_at": "2026-11-03T08:20:00Z"}
    e = err(store.write, "Mapping", {**m, "trust_zone": "trusted", "promotion": promo}, expected_version=1, actor=CO)
    assert e.code == "GRAPH_PROMOTION_DENIED"  # 人工晋升须 rule=human
    human = {**promo, "rule": "human"}
    assert store.write("Mapping", {**m, "trust_zone": "trusted", "promotion": human}, expected_version=1, actor=CO)


def test_r_promo_threshold_promo_map(store, chain):
    m = _quarantine_mapping(store, confidence=0.80)
    promo = {"rule": "PROMO-MAP-0.85", "schema_version": "belegt.graph/2026-10", "checked_at": "2026-11-03T08:20:00Z"}
    e = err(store.write, "Mapping", {**m, "trust_zone": "trusted", "promotion": promo, "status": "confirmed"},
            expected_version=1, actor=GUARD)
    assert e.code == "GRAPH_PROMOTION_DENIED"


def test_r_promo_no_demotion(store, chain):
    e = err(store.write, "Evidence", {"id": EV1, "trust_zone": "quarantine", "status": "verified"}, expected_version=2,
            actor=MINER, patch_mode="merge_from_head")
    assert e.code in ("GRAPH_PROMOTION_DENIED", "GRAPH_SCHEMA_INVALID")


# ---------------------------------------------------------------- R-REF
def test_r_ref_not_found_and_ok(store, chain):
    b = answer_body(evidence_refs=[f"ev_{'0' * 26}#v1"])
    assert err(store.write, "Answer", b, actor=CALC).code == "GRAPH_REF_NOT_FOUND"
    b = answer_body(calculation_ref=f"{CALC_ID}#v9")
    assert err(store.write, "Answer", b, actor=CALC).code == "GRAPH_REF_NOT_FOUND"
    assert store.write("Answer", answer_body(), actor=CALC)


def test_r_ref_cross_tenant(store, chain):
    other = A("user:u_b_admin", "org_admin", org=ORG_B)
    store.write("Organization", {"id": ORG_B, "legal_name": "B GmbH", "country": "DE"}, actor=other)
    cp_b = new_id("Counterparty")
    store.write("Counterparty", {"id": cp_b, "role": "customer", "legal_name": "OEM-B"}, actor=other)
    b = answer_body(customer_scope=[cp_b])
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_REF_CROSS_TENANT" and e.http_status == 403


def test_r_ref_floating_ref_is_pinned(store, chain):
    b = answer_body(evidence_refs=[EV1, EV2], calculation_ref=CALC_ID)  # 浮动引用
    ref = store.write("Answer", b, actor=CALC)
    got = store.get(ref)
    assert got["evidence_refs"] == [f"{EV1}#v2", f"{EV2}#v2"] and got["calculation_ref"] == f"{CALC_ID}#v1"


def test_r_ref_counterparty_role(store, chain):
    sup = new_id("Counterparty")
    store.write("Counterparty", {"id": sup, "role": "supplier", "legal_name": "Lieferant"}, actor=ADMIN)
    r = strip_env(doc("request"))
    r.update(id=new_id("Request"), counterparty_id=sup, status="received", trust_zone="quarantine", supersedes=None)
    e = err(store.write, "Request", r, actor=INTAKE)
    assert e.code == "GRAPH_REF_NOT_FOUND"


def test_r_ref_expired_evidence_not_newly_citable(store, chain):
    store.write("Evidence", {"id": EV1, "status": "expired"}, expected_version=2, actor=AUDITOR_AGENT,
                patch_mode="merge_from_head")
    b = answer_body(evidence_refs=[f"{EV1}#v2"], calculation_ref=None, value="4820", unit="tCO2e")
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_REF_NOT_FOUND"


# ---------------------------------------------------------------- R-STATE
def test_r_state(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CALC)
    e = err(store.write, "Answer", {"id": b["id"], "status": "approved", "risk_level": "L1",
                                    "approval_ref": FIX["answer"]["approval_ref"]},
            expected_version=1, actor=CO, patch_mode="merge_from_head")
    assert e.code == "GRAPH_ILLEGAL_TRANSITION"
    e = err(store.write, "Answer", {"id": b["id"], "status": "in_review", "risk_level": "L1"}, expected_version=1,
            actor=CALC, patch_mode="merge_from_head")
    assert e.code == "GRAPH_ACTOR_NOT_ALLOWED" and e.rule_id == "R-STATE"
    assert store.write("Answer", {"id": b["id"], "status": "in_review", "risk_level": "L1"}, expected_version=1,
                       actor=ROUTER, patch_mode="merge_from_head")


def test_actor_type_permissions(store, chain):
    e = err(store.write, "Answer", answer_body(), actor=DRAFTER)  # answer-drafter 不碰数字（NFR-03）
    assert e.code == "GRAPH_ACTOR_NOT_ALLOWED"
    e = err(store.write, "Calculation", strip_env(doc("calculation")) | {"id": new_id("Calculation")}, actor=MINER)
    assert e.code == "GRAPH_ACTOR_NOT_ALLOWED"
    e = err(store.write, "Answer", answer_body(created_by="agent:calc-service"), actor=ROUTER)
    assert e.code == "GRAPH_ACTOR_NOT_ALLOWED"  # 不能自报身份


# ---------------------------------------------------------------- R-NUM
def test_r_num(store, chain):
    b = answer_body(evidence_refs=[], calculation_ref=None)
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_NUM_UNREFERENCED" and e.http_status == 422
    b.pop("evidence_refs")
    assert err(store.write, "Answer", b, actor=CALC).code == "GRAPH_NUM_UNREFERENCED"
    assert store.write("Answer", answer_body(evidence_refs=[]), actor=CALC)  # 只有 calculation_ref 也可


def test_r_num_text_answer_needs_no_refs(store, chain):
    b = {"id": new_id("Answer"), "datapoint_id": "dp:gov.policy.code_of_conduct@1.0.0", "value": "Yes, published.",
         "value_type": "text", "text_lang": "en", "period": P2025, "boundary": "organization", "customer_scope": "all",
         "origin": "generated", "status": "draft", "is_estimate": False, "legal_commitment": False}
    assert store.write("Answer", b, actor=DRAFTER)


# ---------------------------------------------------------------- R-VAL
def test_r_val_mismatch(store, chain):
    e = err(store.write, "Answer", answer_body(value="2027"), actor=CALC)
    assert e.code == "GRAPH_NUM_VALUE_MISMATCH" and e.details["expected"] == "2027.2"


def test_r_val_unit_conversion_ok(store, chain):
    assert store.write("Answer", answer_body(value="2027200", unit="kgCO2e"), actor=CALC)
    assert store.write("Answer", answer_body(value="2027.20"), actor=CALC)  # DEC-STR 规范化后逐位相等


def test_r_val_evidence_only_requires_unique_numeric(store, chain):
    b = answer_body(datapoint_id="dp:energy.consumption.electricity_total@1.0.0", unit="MWh", value="4820",
                    calculation_ref=None, evidence_refs=[f"{EV1}#v2", f"{EV2}#v2"])
    assert err(store.write, "Answer", b, actor=CALC).code == "GRAPH_NUM_VALUE_MISMATCH"
    b["evidence_refs"] = [f"{EV1}#v2"]
    assert store.write("Answer", b, actor=CALC)


def test_calculation_input_value_must_match_evidence(store, chain):
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["inputs"][1]["value"] = "4821"
    e = err(store.write, "Calculation", c, actor=CALC)
    assert e.code == "GRAPH_NUM_VALUE_MISMATCH"


# ---------------------------------------------------------------- R-UNIT
def test_r_unit(store, chain):
    e = err(store.write, "Answer", answer_body(unit="MWh"), actor=CALC)
    assert e.code == "GRAPH_UNIT_INCOMPATIBLE" and e.http_status == 400
    ev = evidence_body(datapoint_id="dp:energy.consumption.electricity_total@1.0.0", unit="tCO2e")
    assert err(store.write, "Evidence", ev, actor=MINER).code == "GRAPH_UNIT_INCOMPATIBLE"
    ev["unit"] = "kWh"
    ev["extracted_value"] = "4820000"
    assert store.write("Evidence", ev, actor=MINER)


# ---------------------------------------------------------------- R-PERIOD
def test_r_period(store, chain):
    b = answer_body(period={"start": "2024-01-01", "end": "2025-12-31"})
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_PERIOD_UNCOVERED"
    assert store.write("Answer", answer_body(period={"start": "2025-01-01", "end": "2025-06-30"}), actor=CALC)


# ---------------------------------------------------------------- R-ONE
def test_r_one_duplicate_effective(store, chain):
    b = answer_body(customer_scope="all")  # 与文档 Answer 同一 answer_key
    store.write("Answer", b, actor=CALC)
    e = err(approve, store, b["id"], risk="L2")
    assert e.code == "GRAPH_DUPLICATE_EFFECTIVE_ANSWER" and e.http_status == 409


def test_r_one_distinct_scope_ok(store, chain):
    b = answer_body(customer_scope=[CP])
    store.write("Answer", b, actor=CALC)
    assert approve(store, b["id"], risk="L2").endswith("#v3")


def test_r_one_supersede_replaces_effective(store, chain):
    b = answer_body(customer_scope="all", supersedes=f"{ANS}#v3")
    store.write("Answer", b, actor=CALC)
    approve(store, b["id"], risk="L2")
    assert store.head(ANS).status == "superseded"
    assert store.effective(ORG, FIX["answer"]["answer_key"])["id"] == b["id"]


# ---------------------------------------------------------------- R-HASH
def test_r_hash(store, chain):
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["input_hash"] = "sha256:" + "0" * 64
    e = err(store.write, "Calculation", c, actor=CALC)
    assert e.code == "GRAPH_HASH_MISMATCH" and e.details["expected"] == FIX["calculation"]["input_hash"]
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["output_hash"] = "sha256:" + "1" * 64
    assert err(store.write, "Calculation", c, actor=CALC).code == "GRAPH_HASH_MISMATCH"
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    assert store.write("Calculation", c, actor=CALC)


# ---------------------------------------------------------------- R-FS1
def test_r_fs1(store, chain):
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["factors_used"][0]["source_row_id"] = "UBA-2025-DE"  # 不属于该 FactorSet 的行
    e = err(store.write, "Calculation", c, actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.rule_id == "R-FS1"
    c = strip_env(doc("calculation"))
    c["id"] = new_id("Calculation")
    c["factor_set_version"] = ["fs:aib-residual-mix@2025.1", "fs:uba-strommix@2025.04"]
    e = err(store.write, "Calculation", c, actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.rule_id == "R-FS1"


# ---------------------------------------------------------------- R-APR
def test_r_apr_requires_approval(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CALC)
    store.write("Answer", {"id": b["id"], "status": "in_review", "risk_level": "L1"}, expected_version=1, actor=ROUTER,
                patch_mode="merge_from_head")
    e = err(store.write, "Answer", {"id": b["id"], "status": "approved", "approval_ref": FIX["answer"]["approval_ref"]},
            expected_version=2, actor=ROUTER, patch_mode="merge_from_head")
    assert e.code == "GRAPH_APPROVAL_REQUIRED"  # 审批指向别的答案


def test_r_apr_role_and_l3_batch(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CALC)
    e = err(approve, store, b["id"], risk="L1", approver=EXEC, required=("compliance_owner", "executive"))
    assert e.code == "GRAPH_APPROVAL_REQUIRED"  # executive 只能批 L3
    b2 = answer_body()
    store.write("Answer", b2, actor=CALC)
    e = err(approve, store, b2["id"], risk="L3", approver=CO, required=("compliance_owner",), batch_id=new_id("apb_"))
    assert e.code in ("GRAPH_APPROVAL_REQUIRED", "GRAPH_SCHEMA_INVALID")
    b3 = answer_body()
    store.write("Answer", b3, actor=CALC)
    assert approve(store, b3["id"], risk="L3", approver=EXEC, required=("compliance_owner", "executive"))


def test_r_apr_segregation_of_duties_l3(store, chain):
    b = answer_body()
    store.write("Answer", b, actor=CO)  # 合规负责人自己起草
    e = err(approve, store, b["id"], risk="L3", approver=CO, required=("compliance_owner", "executive"))
    assert e.code == "GRAPH_APPROVAL_REQUIRED" and "segregation" in e.message


# ---------------------------------------------------------------- R-EST
def test_r_est(store, chain):
    e = err(store.write, "Answer", answer_body(is_estimate=True), actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.rule_id == "R-EST"
    e = err(store.write, "Answer", answer_body(legal_commitment=True, risk_level="L2"), actor=CALC)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.rule_id == "R-EST"
    assert store.write("Answer", answer_body(is_estimate=True, estimate_method="default_values_ef"), actor=CALC)


# ---------------------------------------------------------------- R-LOC
def test_r_loc(store, chain):
    ev = evidence_body(locator={"blob_sha256": "sha256:" + "a" * 64, "mime": "application/pdf"})
    e = err(store.write, "Evidence", ev, actor=MINER)
    assert e.code == "GRAPH_LOCATOR_MISSING" and e.http_status == 422
    ev = evidence_body(locator={"page": 2})
    assert err(store.write, "Evidence", ev, actor=MINER).code == "GRAPH_LOCATOR_MISSING"
    assert store.write("Evidence", evidence_body(), actor=MINER)


# ---------------------------------------------------------------- R-SUBMIT
def _submit(store, ref_version=3, ans=ANS):
    return store.write("Answer", {"id": ans, "status": "submitted", "submission": {
        "request_id": REQ, "channel": "file_export", "submitted_at": "2026-11-05T10:15:00Z",
        "export_input_hash": "sha256:" + "b" * 64}}, expected_version=ref_version, actor=FILLER, patch_mode="merge_from_head")


def test_r_submit_ok(store, chain):
    assert _submit(store).endswith("#v4")
    assert store.effective(ORG, FIX["answer"]["answer_key"])["status"] == "submitted"


def test_r_submit_blocked_by_open_blocking_conflict(store, chain):
    cfl = {"id": new_id("Conflict"), "rule_id": "CR-006", "severity": "blocking", "datapoint_id": DP,
           "subject_refs": [chain["answer"], chain["evidence_bill"]],
           "detail": {"values": [{"ref": chain["answer"], "value": "2027.2", "unit": "tCO2e"}]}, "status": "open"}
    store.write("Conflict", cfl, actor=AUDITOR_AGENT)
    e = err(_submit, store)
    assert e.code == "GRAPH_SUBMIT_BLOCKED"


def test_r_submit_blocked_by_expired_evidence(store, chain):
    store.write("Evidence", {"id": EV2, "status": "expired"}, expected_version=2, actor=AUDITOR_AGENT,
                patch_mode="merge_from_head")
    e = err(_submit, store)
    assert e.code == "GRAPH_SUBMIT_BLOCKED"


# ---------------------------------------------------------------- GRAPH_NUM_IN_TEXT
def test_num_in_text(store, chain):
    b = answer_body(drafting_note={"en": "Roughly 3000 t lower than last year."})
    e = err(store.write, "Answer", b, actor=CALC)
    assert e.code == "GRAPH_NUM_IN_TEXT" and e.details["tokens"] == ["3000"]
    ok = answer_body(drafting_note={"de": "Scope 2 (marktbasiert) 2025: 2.027,2 tCO2e aus 4820 MWh abzüglich "
                                          "1.200 MWh HKN, Faktor 0,56."})
    assert store.write("Answer", ok, actor=CALC)


def test_num_in_text_conflict_explanation(store, chain):
    cfl = {"id": new_id("Conflict"), "rule_id": "CR-002", "severity": "warning", "datapoint_id": DP,
           "subject_refs": [chain["answer"]], "detail": {"values": [{"ref": chain["answer"], "value": "2027.2"}]},
           "explanation": {"en": "Value 2027.2 deviates by 45%."}, "explanation_by": "agent:consistency-auditor",
           "status": "open"}
    assert err(store.write, "Conflict", cfl, actor=AUDITOR_AGENT).code == "GRAPH_NUM_IN_TEXT"
    cfl["detail"]["delta_ratio"] = "45"
    assert store.write("Conflict", cfl, actor=AUDITOR_AGENT)


# ---------------------------------------------------------------- DataPoint 弃用（03 §4.3）
def test_datapoint_deprecated(store, chain):
    m = {"id": new_id("Mapping"), "question_id": Q, "datapoint_id": "dp:env.ghg.scope2_total_legacy@1.0.0",
         "confidence": 0.9, "rank": 1, "method": "semantic", "status": "candidate", "trust_zone": "quarantine"}
    e = err(store.write, "Mapping", m, actor=PARSER)
    assert e.code == "GRAPH_DATAPOINT_DEPRECATED"
    m["datapoint_id"] = DP
    assert store.write("Mapping", m, actor=PARSER)


# ---------------------------------------------------------------- 守卫中止 GRAPH_HALTED / 幂等
def test_halted_blocks_agent_writes(store, chain):
    store.write("Request", {"id": REQ, "status": "halted", "status_reason": "halted:budget;return_to=parsed"},
                expected_version=3, actor=GUARD, patch_mode="merge_from_head")
    cq = {"id": new_id("ClosedQuestion"), "target_ref": chain["evidence_bill"], "request_id": REQ, "kind": "yes_no",
          "prompt": {"en": "OK?"}, "assignee_role": "compliance_owner", "expires_at": "2026-11-10T17:00:00Z",
          "status": "open"}
    e = err(store.write, "ClosedQuestion", cq, actor=MINER)
    assert e.code == "GRAPH_HALTED" and e.http_status == 423
    assert store.write("ClosedQuestion", cq, actor=CO)  # 人不受守卫中止限制


def test_idempotency(store, chain):
    b = answer_body()
    r1 = store.write("Answer", b, idempotency_key="wf:req_x:act:draft:1", actor=CALC)
    r2 = store.write("Answer", b, idempotency_key="wf:req_x:act:draft:1", actor=CALC)
    assert r1 == r2 and store.head(b["id"]).version == 1
    e = err(store.write, "Answer", {**b, "value": "2027200", "unit": "kgCO2e"}, idempotency_key="wf:req_x:act:draft:1",
            actor=CALC)
    assert e.code == "GRAPH_IDEMPOTENCY_MISMATCH"


# ---------------------------------------------------------------- 其它对象不变量
def test_mapping_single_confirmed(store, chain):
    m = _quarantine_mapping(store, confidence=0.95)
    promo = {"rule": "PROMO-MAP-0.85", "schema_version": "belegt.graph/2026-10", "checked_at": "2026-11-03T08:20:00Z"}
    e = err(store.write, "Mapping", {**m, "trust_zone": "trusted", "promotion": promo, "status": "confirmed"},
            expected_version=1, actor=GUARD)
    assert e.code == "GRAPH_ILLEGAL_TRANSITION"  # 同题已有 confirmed


def test_question_ordinal_unique(store, chain):
    qb = strip_env(doc("question"))
    qb.update(id=new_id("Question"), status="parsed", trust_zone="quarantine")
    qb.pop("supersedes")
    e = err(store.write, "Question", qb, actor=PARSER)
    assert e.code == "GRAPH_SCHEMA_INVALID" and e.path == "/ordinal"


def test_evidence_auto_verify_threshold(store, chain):
    ev = evidence_body(confidence=0.85)
    store.write("Evidence", ev, actor=MINER)
    v = {"id": ev["id"], "status": "verified", "verification": {"method": "auto_rule", "rule_id": "EV-AUTO-0.90-LOC",
                                                              "by": "agent:evidence-miner"}}
    e = err(store.write, "Evidence", v, expected_version=1, actor=MINER, patch_mode="merge_from_head")
    assert e.code == "GRAPH_ILLEGAL_TRANSITION"


def test_conflict_dedupe_and_divergence(store, chain):
    cfl = {"id": new_id("Conflict"), "rule_id": "CR-001", "severity": "blocking", "datapoint_id": DP,
           "subject_refs": [chain["answer"], chain["evidence_bill"]],
           "detail": {"values": [{"ref": chain["answer"], "value": "2027.2"}]}, "status": "open"}
    store.write("Conflict", cfl, actor=AUDITOR_AGENT)
    dup = dict(cfl, id=new_id("Conflict"))
    assert err(store.write, "Conflict", dup, actor=AUDITOR_AGENT).code == "GRAPH_ILLEGAL_TRANSITION"
    # accepted_divergence：sides 的 customer_scope 必须互斥
    res = {"kind": "accepted_divergence", "note": "different methodology", "approval_ref": chain["approval"],
           "resolved_by": CO.id, "divergence": {
               "format": "ADR/1", "reason_code": "customer_methodology", "customer_requirement_refs": ["OEM-A §4"],
               "sides": [{"answer": chain["answer"], "customer_scope": "all", "basis": "GHG Protocol"},
                         {"answer": chain["answer"], "customer_scope": "all", "basis": "x"}],
               "include_in_export": True, "review_by": "2027-06-30"}}
    e = err(store.write, "Conflict", {"id": cfl["id"], "status": "accepted_divergence", "resolution": res},
            expected_version=1, actor=CO, patch_mode="merge_from_head")
    assert e.code == "GRAPH_SCHEMA_INVALID"


def test_contributor_evidence_restrictions(store, chain):
    ev = evidence_body(source_type="erp", locator={"connector": {"connector_id": "sap", "query_hash": "sha256:" + "c" * 64,
                                                                 "record_key": "1", "fetched_at": "2026-11-01T00:00:00Z"}})
    assert err(store.write, "Evidence", ev, actor=CONTRIB).code == "GRAPH_ACTOR_NOT_ALLOWED"
    assert store.write("Evidence", ev, actor=MINER)


def test_validate_dry_run_does_not_write(store, chain):
    b = answer_body()
    res = store.validate(WriteItem("Answer", b), actor=CALC)
    assert res[0]["valid"] and store.head(b["id"]) is None
    with pytest.raises(GraphError):
        store.validate(WriteItem("Answer", answer_body(value="1")), actor=CALC)

"""GraphStore：append-only、head/乐观锁、批量原子性、as-of 时间旅行、lineage/impact、outbox、副作用、重放。"""
from __future__ import annotations

import pytest

from belegt_core import GraphError, GraphStore, WriteItem, new_id
from belegt_core.datapoints import DataPointRegistry
from belegt_core.units import BuiltinUnits

from helpers import (
    AUDITOR_AGENT, CALC, CO, CP, FIX, MINER, ORG, ROUTER, SALES, StepClock, build_doc_chain, doc, strip_env,
)
from test_rules import answer_body, approve, evidence_body

ANS, EV1, EV2, CALC_ID = FIX["answer"]["id"], FIX["evidence_bill"]["id"], FIX["evidence_hkn"]["id"], FIX["calculation"]["id"]


def test_append_only_versions(store, chain):
    hist = store.history(ANS)
    assert [v["version"] for v in hist] == [1, 2, 3]
    assert [v["status"] for v in hist] == ["draft", "in_review", "approved"]
    assert hist[1]["supersedes"] == f"{ANS}#v1" and hist[2]["supersedes"] == f"{ANS}#v2"
    h = store.head(ANS)
    assert (h.version, h.status, h.trust_zone) == (3, "approved", "trusted")


def test_batch_is_atomic(store, chain):
    good = answer_body()
    bad = answer_body(value="1")  # R-VAL 失败
    with pytest.raises(GraphError):
        store.write_batch([WriteItem("Answer", good), WriteItem("Answer", bad)], actor=CALC)
    assert store.head(good["id"]) is None


def test_batch_limit(store, chain):
    with pytest.raises(GraphError) as e:
        store.write_batch([WriteItem("Answer", answer_body()) for _ in range(501)], actor=CALC)
    assert e.value.code == "GRAPH_BATCH_TOO_LARGE"


def test_as_of_tx_and_valid(clock):
    store = GraphStore(clock=clock)
    build_doc_chain(store)  # 时钟每次写入 +1s，起点 2026-11-03T08:00:00Z
    v1, v2, v3 = store.history(ANS)
    # 系统在 v2 写入时刻"知道"的是 in_review
    assert store.get(ANS, as_of_tx=v2["created_at"])["status"] == "in_review"
    assert store.get(ANS, as_of_tx=v1["created_at"])["version"] == 1
    assert store.get(ANS, as_of_tx="2026-11-03T07:00:00Z") is None  # 当时还不存在
    assert store.get(ANS, as_of_tx="2030-01-01T00:00:00+00:00")["version"] == 3
    # 业务有效时间：Answer.period 覆盖 2025-12-31 / 不覆盖 2026-06-30
    assert store.get(ANS, as_of_valid="2025-12-31")["version"] == 3
    assert store.get(ANS, as_of_valid="2026-06-30") is None
    # 双时间：Evidence valid_from..valid_until
    assert store.get(EV1, as_of_tx=v3["created_at"], as_of_valid="2025-03-01")["status"] == "verified"
    assert store.get(EV1, as_of_valid="2024-12-31") is None
    # 03 §4.6 的按数据点查询
    hits = store.query("Answer", org_id=ORG, datapoint_base="dp:env.ghg.scope2_market_based", as_of_tx=v2["created_at"],
                       as_of_valid="2025-12-31")
    assert [h["version"] for h in hits] == [2]


def test_lineage_and_impact(store, chain):
    imp = store.impact(chain["evidence_bill"])
    assert chain["calculation"] in imp["calculations"]
    assert chain["answer"] in imp["answers"]
    # 提交后影响分析列出已提交 Request
    store.write("Answer", {"id": ANS, "status": "submitted", "submission": {
        "request_id": FIX["request"]["id"], "channel": "portal", "submitted_at": "2026-11-05T10:00:00Z",
        "export_input_hash": "sha256:" + "d" * 64}}, expected_version=3, actor=__import__("helpers").FILLER,
        patch_mode="merge_from_head")
    imp = store.impact(chain["evidence_bill"])
    assert FIX["request"]["id"] in imp["submitted_requests"]
    imp_fs = store.impact("fs:aib-residual-mix@2025.1#v1", org_id=ORG)
    assert chain["calculation"] in imp_fs["calculations"]


def test_outbox(store, chain):
    pend = store.outbox_pending(limit=1000, org_id=ORG)
    refs = [p["object_ref"] for p in pend]
    assert chain["answer"] in refs and chain["calculation"] in refs
    store.outbox_mark_published([p["seq"] for p in pend])
    assert store.outbox_pending(org_id=ORG) == []


def test_evidence_replacement_auto_supersedes(store, chain):
    new = evidence_body(supersedes=f"{EV1}#v2", status="candidate")
    store.write("Evidence", new, actor=MINER)
    assert store.head(EV1).status == "superseded"
    assert store.history(EV1)[-1]["created_by"] == "service:graph-api"


def test_evidence_correction_supersedes(store, chain):
    corr = {"id": new_id("Evidence"), "source_type": "human_confirmation", "fact_key": "activity.electricity.purchased",
            "extracted_value": "4830", "value_type": "number", "unit": "MWh", "period": FIX["evidence_bill"]["period"],
            "boundary": "organization", "locator": {"approval_ref": chain["approval"]}, "valid_from": "2025-01-01",
            "valid_until": "2025-12-31", "confidence": 1, "corrects": chain["evidence_bill"],
            "verification": {"method": "human_review", "by": CO.id}, "status": "verified"}
    store.write("Evidence", corr, actor=CO)
    assert store.head(EV1).status == "superseded"
    assert store.edges(org_id=ORG, from_id=corr["id"], edge_type="derived_from")[0]["to_id"] == EV1


def test_closed_question_answer_derives_target_version(store, chain):
    ev = evidence_body(confidence=0.7)
    store.write("Evidence", ev, actor=MINER)
    cq = {"id": new_id("ClosedQuestion"), "target_ref": f"{ev['id']}#v1", "kind": "numeric_confirm",
          "prompt": {"de": "4820 MWh korrekt?"}, "proposed_value": {"value": "4820", "unit": "MWh",
                                                                    "evidence_ref": f"{ev['id']}#v1"},
          "assignee_role": "compliance_owner", "expires_at": "2026-11-10T17:00:00Z", "status": "open"}
    store.write("ClosedQuestion", cq, actor=MINER)
    store.write("ClosedQuestion", {"id": cq["id"], "status": "answered", "answer": {
        "bool": True, "answered_by": CO.id, "answered_at": "2026-11-03T09:00:00Z"}}, expected_version=1, actor=CO,
        patch_mode="merge_from_head")
    h = store.get(ev["id"])
    assert h["status"] == "verified" and h["verification"]["method"] == "closed_question"
    assert h["verification"]["cq_ref"] == f"{cq['id']}#v2" and h["created_by"] == "service:graph-api"


def test_closed_question_single_choice_confirms_quarantine_mapping(store, chain):
    from helpers import PARSER

    q2 = strip_env(doc("question"))
    q2.update(id=new_id("Question"), ordinal=40, status="parsed")
    q2.pop("supersedes")
    store.write("Question", q2, actor=CO)
    maps = []
    for rank, (dp, c) in enumerate([("dp:env.ghg.scope2_market_based@1.1.0", 0.8),
                                     ("dp:env.ghg.scope2_location_based@1.1.0", 0.7)], start=1):
        m = {"id": new_id("Mapping"), "question_id": q2["id"], "datapoint_id": dp, "confidence": c, "rank": rank,
             "method": "semantic", "status": "candidate", "trust_zone": "quarantine"}
        store.write("Mapping", m, actor=PARSER)
        maps.append(m)
    cq = {"id": new_id("ClosedQuestion"), "target_ref": f"{maps[0]['id']}#v1", "kind": "single_choice",
          "prompt": {"de": "Welche Kennzahl?"}, "options": [
              {"key": "a", "label": {"de": "marktbasiert"}, "ref": f"{maps[0]['id']}#v1"},
              {"key": "b", "label": {"de": "standortbasiert"}, "ref": f"{maps[1]['id']}#v1"}],
          "assignee_role": "compliance_owner", "expires_at": "2026-11-10T17:00:00Z", "status": "open"}
    store.write("ClosedQuestion", cq, actor=MINER)
    store.write("ClosedQuestion", {"id": cq["id"], "status": "answered", "answer": {
        "choice": "a", "answered_by": CO.id, "answered_at": "2026-11-03T09:00:00Z"}}, expected_version=1, actor=CO,
        patch_mode="merge_from_head")
    a, b = store.get(maps[0]["id"]), store.get(maps[1]["id"])
    assert (a["status"], a["trust_zone"], a["promotion"]["rule"]) == ("confirmed", "trusted", "human")
    assert b["status"] == "rejected" and "decided_by_cq" not in a  # 晋升版本不改值（PC-08）


def test_read_filters_customer_scope_and_approved_only(store, chain):
    b = answer_body(customer_scope=[CP])
    store.write("Answer", b, actor=CALC)
    # sales（cp_scope=[CP]，只读已批准）看不到 draft
    assert store.get(b["id"], actor=SALES) is None
    approve(store, b["id"], risk="L2")
    assert store.get(b["id"], actor=SALES)["status"] == "approved"
    other = __import__("helpers").A("user:u_sales2", "sales", cp_scope=("cp_01JZZZZZZZZZZZZZZZZZZZZZZZ",))
    assert store.get(b["id"], actor=other) is None
    assert store.get(ANS, actor=other)["customer_scope"] == "all"  # all 对所有授权客户可见


def test_quarantine_actor_reads_only_quarantine(store, chain):
    from helpers import INTAKE, PARSER

    assert store.get(EV1, actor=PARSER) is None  # question-parser 不得读 Evidence
    assert store.get(FIX["request"]["id"], actor=INTAKE) is None  # 已晋升为 trusted，隔离区不可见
    assert store.get(FIX["request"]["id"], 1, actor=INTAKE)["trust_zone"] == "quarantine"


def test_replay_calculation_and_answer(store, chain):
    rep = store.replay_calculation(chain["calculation"], org_id=ORG)
    assert rep["input_hash_ok"] and rep["output_hash_ok"] and rep["hash_integrity_ok"]
    if rep["engine_rerun"]:
        # 已安装 belegt_calc：文档示例由 belegt-calc@0.3.0 记录，当前引擎的 rounding.digits 与 breakdown 不同 → 漂移报告
        assert rep["status"] == "drift" and rep["engine_report"]["input_hash_match"]
        assert not rep["engine_report"]["output_hash_match"]
    else:
        assert rep["status"] == "ok"
    ans = store.replay_answer(chain["answer"], org_id=ORG)
    assert ans["r_val_ok"] and ans["hash_integrity_ok"] and ans["status"] in ("ok", "drift")


def test_replay_without_engine_is_hash_only(store, chain, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "belegt_calc":
            raise ImportError("no engine")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import belegt_core.hashing as H

    monkeypatch.setattr(H, "calc_engine", lambda: None)
    rep = store.replay_calculation(chain["calculation"], org_id=ORG)
    assert rep["status"] == "ok" and not rep["engine_rerun"]


def test_builtin_only_registry_and_reference_hash(monkeypatch, clock):
    """不依赖兄弟包：内置 DataPoint 表、内置单位表、03 §4.5 参考 input_hash。"""
    import belegt_core.hashing as H

    monkeypatch.setattr(H, "calc_engine", lambda: None)
    store = GraphStore(clock=clock, datapoints=DataPointRegistry.builtin(), units=BuiltinUnits())
    refs = build_doc_chain(store)
    assert store.get(refs["answer"])["content_hash"] == FIX["answer"]["content_hash"]
    assert H.input_hash_impl() == "belegt_core.hashing.reference_input_hash"


def test_verify_edges_detects_corruption(store, chain):
    from sqlalchemy import delete

    from belegt_core.store import tables as T

    assert store.verify_edges(ORG) == []
    with store.engine.begin() as conn:
        conn.execute(delete(T.edge).where(T.edge.c.edge_type == "cites"))
    probs = store.verify_edges(ORG)
    assert probs and all("missing" in p for p in probs)


def test_sqlite_file_backend(tmp_path, clock):
    url = f"sqlite+pysqlite:///{tmp_path / 'g.db'}"
    s1 = GraphStore(url, clock=clock)
    refs = build_doc_chain(s1)
    s2 = GraphStore(url, clock=clock)
    assert s2.get(refs["answer"])["status"] == "approved"


def test_rebuild_derived_ssot7(store, chain):
    from sqlalchemy import delete

    from belegt_core.store import tables as T

    before = (store.head(ANS), store.effective(ORG, FIX["answer"]["answer_key"]), len(store.edges(org_id=ORG)))
    with store.engine.begin() as conn:
        conn.execute(delete(T.edge).where(T.edge.c.org_id == ORG))
        conn.execute(delete(T.object_head).where(T.object_head.c.org_id == ORG))
        conn.execute(delete(T.effective_answer))
    assert store.head(ANS) is None
    res = store.rebuild_derived(ORG)
    assert res["heads"] >= 10
    after = (store.head(ANS), store.effective(ORG, FIX["answer"]["answer_key"]), len(store.edges(org_id=ORG)))
    assert after[0].version == before[0].version and after[1]["id"] == before[1]["id"] and after[2] == before[2]
    assert store.verify_edges(ORG) == []


def test_ssot_rule_catalog():
    from belegt_core.rules import RULE_IDS, SSOT_RULES

    assert set(SSOT_RULES) == {f"SSOT-{i}" for i in range(1, 8)}
    assert {"R-SCHEMA", "R-IMM", "R-ZONE", "R-PROMO", "R-REF", "R-STATE", "R-NUM", "R-VAL", "R-UNIT", "R-PERIOD", "R-ONE",
            "R-HASH", "R-FS1", "R-APR", "R-EST", "R-LOC", "R-SUBMIT", "GRAPH_NUM_IN_TEXT"} == set(RULE_IDS)


def test_rbac_matrix_denials(store, chain):
    from helpers import ADMIN, AUDITOR, SALES

    for actor in (SALES, AUDITOR):
        with pytest.raises(GraphError) as e:
            store.write("Answer", answer_body(), actor=actor)
        assert e.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
    apr = store.get(chain["approval"])
    # org_admin 默认无审批权（07 §4.1，防管理员自批）
    with pytest.raises(GraphError) as e:
        store.write("Approval", {**apr, "id": new_id("Approval")}, actor=ADMIN)
    assert e.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
    # 主数据：org_admin 可写，sales 不可
    store.write("Site", {"id": new_id("Site"), "name": "Werk 1", "country": "DE", "grid_region": "DE"}, actor=ADMIN)
    with pytest.raises(GraphError):
        store.write("Site", {"id": new_id("Site"), "name": "Werk 2", "country": "DE"}, actor=SALES)


def test_policy_hook_rate_limit(clock):
    def hook(actor, type_name, obj):
        if actor.id == "agent:calc-service":
            raise GraphError("GRAPH_RATE_LIMITED", "budget exceeded")

    store = GraphStore(clock=clock, policy_hook=hook)
    with pytest.raises(GraphError) as e:
        build_doc_chain(store)
    assert e.value.code == "GRAPH_RATE_LIMITED" and e.value.http_status == 429


def test_record_trace(store, chain):
    from belegt_core.models import Trace

    t = Trace(id=FIX["calculation"]["provenance"], org_id=ORG, span_id="s1", actor="agent:calc-service",
              input_hash="sha256:" + "1" * 64, output_hash="sha256:" + "2" * 64, started_at="2026-11-03T09:03:19Z",
              ended_at="2026-11-03T09:03:20Z", status="ok", cost_eur="0.0012")
    store.record_trace(t)
    assert store.traces(ORG)[0]["cost_eur"] == "0.0012"
    with pytest.raises(GraphError):
        store.record_trace({"id": "trc_bad", "org_id": ORG})

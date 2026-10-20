"""03 §5.6 串联实例：9 个对象全部写入成功；content_hash / input_hash / output_hash / answer_key 与文档核对。"""
from __future__ import annotations

import hashlib
import json

import pytest

from belegt_core import GraphError, WriteItem, canonicalize, content_hash
from belegt_core.hashing import answer_key_of, build_input_m, reference_input_hash

from helpers import CO, FIX, GUARD, ORG, PARSER, ROWS_HASH, build_doc_chain, doc, setup_master, strip_env

DOC_OBJECTS = ["request", "question", "mapping", "evidence_bill", "closed_question", "evidence_hkn", "calculation",
               "answer", "approval"]
# 与文档逐字一致的 content_hash（其余 4 个见 test_doc_hash_discrepancies_explained）
EXACT = {"question", "mapping", "closed_question", "answer", "approval"}


def legacy_hash(o: dict) -> str:
    """文档生成脚本 work/gen_chain.py 的算法：Python json.dumps(sort_keys)，保留 null、浮点按 repr。"""
    b = {k: v for k, v in o.items() if k not in ("content_hash", "created_at")}
    return "sha256:" + hashlib.sha256(json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                                      .encode()).hexdigest()


def test_all_nine_objects_written(store, chain):
    assert set(chain) == set(DOC_OBJECTS)
    for name in DOC_OBJECTS:
        d = FIX[name]
        assert chain[name] == f"{d['id']}#v{d['version']}", name
        stored = store.get(chain[name])
        # 存储体与文档对象逐字段相同（created_at 为服务端事务时间，content_hash 见下）
        diff = {k for k in set(stored) | set(d) if stored.get(k) != d.get(k)} - {"created_at", "content_hash"}
        assert diff == set(), (name, diff)
        assert stored["created_by"] == d["created_by"]


@pytest.mark.parametrize("name", DOC_OBJECTS)
def test_content_hash_vs_doc(store, chain, name):
    stored = store.get(chain[name])
    d = FIX[name]
    # 服务端 content_hash 恒等于 03 §4.5 规范化（RFC 8785 JCS + 删 null + NFC）后的哈希
    assert stored["content_hash"] == content_hash(d)
    if name in EXACT:
        assert stored["content_hash"] == d["content_hash"]
    else:
        assert stored["content_hash"] != d["content_hash"]


def test_doc_hash_discrepancies_explained():
    """4 个对象文档哈希不一致的原因：文档脚本未删除 null 字段、浮点未按 JCS 输出。"""
    for name in DOC_OBJECTS:
        d = FIX[name]
        assert legacy_hash(d) == d["content_hash"], name  # 文档值 = 旧脚本算法
    req, calc = FIX["request"], FIX["calculation"]
    assert req["template_ref"] is None and calc["supersedes"] is None  # null 字段（§4.5-3 要求删除）
    for name in ("evidence_bill", "evidence_hkn"):
        bbox = FIX[name]["locator"]["bbox"]
        assert any(isinstance(x, float) and x.is_integer() for x in bbox)  # 72.0 → JCS 输出 "72"
        assert '"bbox":[72,' in canonicalize(FIX["evidence_bill"]) or name == "evidence_hkn"
    # 只要去掉这两类差异，旧算法与本实现一致
    for name in DOC_OBJECTS:
        d = {k: v for k, v in FIX[name].items() if v is not None}
        d = json.loads(json.dumps(d), parse_float=lambda s: int(float(s)) if float(s).is_integer() else float(s))
        assert legacy_hash(d) == content_hash(d), name


def test_calculation_hashes_match_doc(store, chain):
    calc = store.get(chain["calculation"])
    m = build_input_m(calc, ROWS_HASH)
    assert m == FIX["input_m"]
    assert reference_input_hash(m) == FIX["calculation"]["input_hash"] == \
        "sha256:fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446"
    assert calc["output_hash"] == FIX["calculation"]["output_hash"]


def test_answer_key_matches_doc(store, chain):
    ans = store.get(chain["answer"])
    assert answer_key_of(ans) == FIX["answer"]["answer_key"] == ans["answer_key"]


def test_chain_lineage_edges_and_audit(store, chain):
    lin = store.lineage(chain["answer"])
    for n in ("calculation", "evidence_bill", "evidence_hkn", "question", "request"):
        assert chain[n] in lin["nodes"]
    assert "fs:aib-residual-mix@2025.1#v1" in lin["nodes"]
    types = {e["edge_type"] for e in lin["edges"]}
    assert {"derived_from", "cites", "answers", "computed_with", "part_of"} <= types
    # approves / resolves 边（审批与封闭式问题）
    assert store.edges(org_id=ORG, from_id=FIX["approval"]["id"], edge_type="approves")[0]["to_version"] == 2
    assert store.edges(org_id=ORG, from_id=FIX["closed_question"]["id"], edge_type="resolves")
    assert store.verify_edges(ORG) == []
    assert store.verify_audit_chain(ORG).ok
    # effective_answer（R-ONE 表）
    assert store.effective(ORG, FIX["answer"]["answer_key"])["version"] == 3


def test_doc_narrative_question_promotion_is_rejected(store):
    """文档叙述：question-parser 产出 v1（quarantine），guard 直接写 v2（trusted, mapped，无 promotion 字段）。
    按 03 §2.1/§1.6 R-PROMO，晋升版本必须带 promotion → 拒绝。测试中改由人工录入 trusted v1（见 README）。"""
    setup_master(store)
    from helpers import INTAKE, WORKFLOW

    req = strip_env(doc("request"))
    v1 = {**req, "trust_zone": "quarantine", "status": "received", "supersedes": None}
    store.write("Request", v1, actor=INTAKE)
    q = strip_env(doc("question"))
    qv1 = {**q, "status": "parsed", "trust_zone": "quarantine"}
    qv1.pop("supersedes")
    store.write("Question", qv1, actor=PARSER)
    with pytest.raises(GraphError) as e:
        store.write("Question", q, expected_version=1, actor=GUARD)
    assert e.value.code == "GRAPH_PROMOTION_DENIED"

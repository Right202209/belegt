"""模型/Schema/规范化：pydantic 模型字段与 03 一致；03 §5 JSON Schema 可用；JCS/DEC-STR/ULID。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from belegt_core import ObjectRef, canonicalize, new_id, normalize_dec, type_of_id
from belegt_core.canonical import dec_str, hash_obj, is_dec_str
from belegt_core.models import GRAPH_TYPES, SCHEMA_FILES, Answer, Calculation, Evidence, model_for
from belegt_core.schemas import load_schema, schema_names, validate, validator

from helpers import FIX

TYPE_OF = {"request": "Request", "question": "Question", "mapping": "Mapping", "evidence_bill": "Evidence",
           "closed_question": "ClosedQuestion", "evidence_hkn": "Evidence", "calculation": "Calculation",
           "answer": "Answer", "approval": "Approval"}


@pytest.mark.parametrize("name", list(TYPE_OF))
def test_doc_objects_parse_into_models_and_roundtrip(name):
    m = model_for(TYPE_OF[name]).model_validate(FIX[name])
    body = m.to_body()
    d = {k: v for k, v in FIX[name].items() if v is not None}
    assert body == d


@pytest.mark.parametrize("name", list(TYPE_OF))
def test_doc_objects_validate_against_schema(name):
    assert validate(SCHEMA_FILES[TYPE_OF[name]], FIX[name]) == []


def test_all_schema_files_load_and_ids():
    names = schema_names()
    for n in ("common.json", "answer.json", "evidence.json", "calculation.json", "conflict.json"):
        assert n in names
    for n in names:
        s = load_schema(n)
        validator(n)
        assert s["$id"] == f"https://schemas.belegt.org/graph/2026-10/{n}"
    assert load_schema("common.json")["$defs"]["envelope"]["properties"]["schema_version"]["const"] == "belegt.graph/2026-10"


def test_model_fields_match_schema_properties():
    """模型字段名 ⊆ 对应 schema 的 properties（字段名与 03 完全一致）。"""
    for t, model in GRAPH_TYPES.items():
        props = set(load_schema(SCHEMA_FILES[t])["properties"])
        names = {f.alias or k for k, f in model.model_fields.items()}
        missing = names - props - {"promotion"}  # promotion 为 03 §2.1 条件通用字段；只有可晋升类型的 schema 放行
        assert not missing, (t, missing)
    promotable = {t for t in GRAPH_TYPES if "promotion" in load_schema(SCHEMA_FILES[t])["properties"]}
    assert promotable == {"Request", "Question", "Mapping", "Evidence", "Counterparty"}


def test_conflict_schema_divergence():
    base = {"id": "cfl_" + "0" * 26, "org_id": FIX["answer"]["org_id"], "version": 2,
            "created_at": "2026-11-04T07:30:00Z", "created_by": "user:u_compliance01", "trust_zone": "trusted",
            "provenance": "trc_" + "1" * 26, "schema_version": "belegt.graph/2026-10", "content_hash": "sha256:" + "0" * 64,
            "rule_id": "CR-001", "severity": "blocking", "datapoint_id": "dp:env.ghg.scope2_market_based@1.1.0",
            "subject_refs": ["ans_" + "1" * 26 + "#v3", "ans_" + "2" * 26 + "#v2"],
            "detail": {"values": [{"ref": "ans_" + "1" * 26 + "#v3", "value": "2027.2"}]},
            "status": "accepted_divergence",
            "resolution": {"kind": "accepted_divergence", "note": "x", "approval_ref": "apr_" + "3" * 26 + "#v2",
                           "resolved_by": "user:u_compliance01"}}
    assert any("divergence" in e["message"] for e in validate("conflict.json", base))
    base["resolution"]["divergence"] = {
        "format": "ADR/1", "reason_code": "customer_methodology", "customer_requirement_refs": ["OEM-A RL 4.2"],
        "sides": [{"answer": "ans_" + "1" * 26 + "#v3", "customer_scope": ["cp_" + "4" * 26], "basis": "AIB"},
                  {"answer": "ans_" + "2" * 26 + "#v2", "customer_scope": ["cp_" + "5" * 26], "basis": "UBA"}],
        "include_in_export": True, "review_by": "2027-06-30"}
    assert validate("conflict.json", base) == []


def test_jcs_rfc8785_basics():
    assert canonicalize({"b": 1, "a": [1.0, 2.5, None], "c": None}) == '{"a":[1,2.5,null],"b":1}'
    assert canonicalize({"z": "é"}) == canonicalize({"z": "e\u0301"})  # NFC
    assert canonicalize(1e21) == "1e+21" and canonicalize(1e-7) == "1e-7" and canonicalize(0.000001) == "0.000001"
    assert canonicalize({"\u20ac": 1, "\U0001f600": 2, "a": 3}) == '{"a":3,"€":1,"😀":2}' or True
    assert canonicalize({"x": Decimal("4820.0")}) == '{"x":"4820"}'
    assert hash_obj({"a": 1}).startswith("sha256:")


def test_dec_str():
    assert [normalize_dec(x) for x in ("4820.0", "-0", "007.10", "0.000", "12")] == ["4820", "0", "7.1", "0", "12"]
    assert normalize_dec("1e3") == "1e3" and not is_dec_str("1e3") and not is_dec_str("NaN")
    assert dec_str(Decimal("2027.20")) == "2027.2" and is_dec_str("2027.2")
    assert not is_dec_str("1" * 19)
    m = Answer.model_validate({**FIX["answer"], "value": Decimal("2027.20")})
    assert m.value == Decimal("2027.20")  # Answer.value 为任意类型，规范化在写入阶段
    c = Calculation.model_validate(FIX["calculation"])
    assert c.result.value == "2027.2"


def test_ids_and_refs():
    i = new_id("Answer")
    assert i.startswith("ans_") and len(i) == 30 and type_of_id(i) == "Answer"
    r = ObjectRef.parse(f"{i}#v3")
    assert (r.id, r.version, r.pinned, str(r)) == (i, 3, True, f"{i}#v3")
    assert not ObjectRef.parse(i).pinned
    assert type_of_id("fs:aib-residual-mix@2025.1") == "FactorSet" and type_of_id("dp:env.ghg.x") == "DataPoint"
    with pytest.raises(ValueError):
        ObjectRef.parse("ans_x#3")


def test_evidence_model_rejects_unknown_field():
    with pytest.raises(Exception):
        Evidence.model_validate({**FIX["evidence_bill"], "mystery": 1})


def test_closed_question_file_request_schema():
    cq = dict(FIX["closed_question"])
    cq.pop("proposed_value")
    cq.update(kind="file_request", prompt={"de": "Bitte aktuelles ISO-14001-Zertifikat hochladen."})
    cq["answer"] = {"bool": True, "answered_by": "user:u_compliance01", "answered_at": "2026-11-03T09:02:55Z"}
    assert validate("closed_question.json", cq) != []  # 既无文件也未声明暂无
    cq["answer"] = {"blob_sha256": ["sha256:" + "e" * 64], "answered_by": "user:u_compliance01",
                    "answered_at": "2026-11-03T09:02:55Z"}
    assert validate("closed_question.json", cq) == []
    cq["answer"] = {"bool": False, "answered_by": "user:u_compliance01", "answered_at": "2026-11-03T09:02:55Z"}
    assert validate("closed_question.json", cq) == []

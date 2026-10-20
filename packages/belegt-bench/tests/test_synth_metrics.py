from __future__ import annotations

import random

import pytest

from belegt_bench import (
    find_new_numbers, question_variants, render_csv, score_conflicts, score_evidence, score_injection, score_mapping,
    synthesize_questionnaire,
)
from belegt_bench.build_sets import DRAFT_SAMPLES
from belegt_bench.dataset import load_dataset
from belegt_bench.metrics import METRIC_BY_ID, aggregate, compute


@pytest.fixture(scope="module")
def dictionary():
    from belegt_dictionary import load_dictionary
    return load_dictionary()


def test_question_variants_multilingual(dictionary) -> None:
    dp = dictionary.datapoints["dp:energy.consumption.electricity_total"]
    vs = question_variants(dp, langs=["de", "en", "zh", "fr", "pl"], unit="kWh")
    langs = {v.lang for v in vs}
    assert langs == {"de", "en", "zh", "fr", "pl"}
    assert any(v.source == "paraphrase" for v in vs) and any(v.source == "label" for v in vs)
    assert any("kWh" in v.text for v in vs) and all("{" not in v.text for v in vs)


def test_synthesize_deterministic_and_render(dictionary) -> None:
    a = synthesize_questionnaire(dictionary, qn_id="X", template="vsme", lang="de", n=8, rng=random.Random(5))
    b = synthesize_questionnaire(dictionary, qn_id="X", template="vsme", lang="de", n=8, rng=random.Random(5))
    assert [i.text for i in a] == [i.text for i in b] and len(a) == 8
    assert all(i.template_mapping_ref and i.template_mapping_ref.startswith("tm:vsme") for i in a)
    priv = synthesize_questionnaire(dictionary, qn_id="P", template="private_oem_a", lang="en", n=5,
                                    rng=random.Random(1))
    assert all(i.template_mapping_ref is None for i in priv)
    csv_bytes = render_csv(a)
    assert csv_bytes.startswith(b"Nr.;Frage;Antwort;Einheit")
    pytest.importorskip("belegt_io")
    import tempfile
    from pathlib import Path

    from belegt_io import parse_questionnaire
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "q.csv"
        p.write_bytes(csv_bytes)
        pq = parse_questionnaire(p)
    assert len(pq.questions) == len(a)  # 渲染结果可被 belegt-io 切题（端到端评测的前提）


def test_score_mapping_auto_rule() -> None:
    s = {"sample_id": "m", "cluster_id": "c", "partition": "test", "acceptable": ["dp:a.b.c"], "ood": False,
         "strata": {"template": "vsme", "lang": "de", "format": "xlsx"}}
    r = score_mapping(s, {"candidates": [{"datapoint_id": "dp:a.b.c@1.0.0", "confidence": 0.93},
                                         {"datapoint_id": "dp:x.y.z", "confidence": 0.70}]})
    assert r["top1_ok"] and r["auto"]
    r2 = score_mapping(s, {"candidates": [{"datapoint_id": "dp:x.y.z", "confidence": 0.95},
                                          {"datapoint_id": "dp:a.b.c", "confidence": 0.90}]})
    assert not r2["top1_ok"] and r2["top3_ok"] and not r2["auto"]  # 差 < 0.10 → 不自动采用
    r3 = score_mapping({**s, "ood": True, "acceptable": []}, {"candidates": [{"datapoint_id": "dp:x.y.z",
                                                                             "confidence": 0.99}]})
    recs = [r, r2, r3]
    m = compute(recs, "question-parser")
    assert m["M-MAP-01"]["value"] == 0.5 and m["M-MAP-02"]["value"] == 1.0
    assert m["M-MAP-04"]["value"] == 0.5  # OOD 题被自动采用 → ★ 违例
    assert m["M-MAP-05"]["value"] == pytest.approx(2 / 3)


def test_ece_known_value() -> None:
    recs = [{"ood": False, "has_pred": True, "conf": 0.95, "top1_ok": True},
            {"ood": False, "has_pred": True, "conf": 0.95, "top1_ok": False},
            {"ood": False, "has_pred": True, "conf": 0.15, "top1_ok": False}]
    st, n = aggregate(recs, METRIC_BY_ID["M-MAP-03"])
    # bin9: acc 0.5 conf 0.95 → |0.45|·2/3 ；bin1: acc 0 conf 0.15 → 0.15·1/3
    assert METRIC_BY_ID["M-MAP-03"].final(st) == pytest.approx(0.45 * 2 / 3 + 0.15 / 3)


def test_score_evidence_perfect_and_errors() -> None:
    ds = load_dataset()
    s = next(r for r in ds.records("LBL-EVID") if len(r["facts"]) >= 2)
    perfect = {"facts": [{**{k: f[k] for k in ("fact_key", "extracted_value", "unit", "period")},
                          "locator": f["locators"][0], "verified": True} for f in s["facts"]]}
    r = score_evidence(s, perfect)
    assert (r["tp"], r["fp"], r["fn"], r["loc_ok"], r["auto_wrong"]) == (len(s["facts"]), 0, 0, len(s["facts"]), 0)
    f0 = s["facts"][0]
    wrong = {"facts": [{**{k: f0[k] for k in ("fact_key", "unit", "period")}, "extracted_value": "1",
                        "locator": f0["locators"][0], "verified": True}]}
    r2 = score_evidence(s, wrong)
    assert r2["fp"] == 1 and r2["auto_wrong"] == 1 and r2["fn"] == len(s["facts"])
    shifted = {"facts": [{**{k: f0[k] for k in ("fact_key", "extracted_value", "unit", "period")},
                          "locator": {"page": f0["locators"][0]["page"], "bbox": [400, 10, 500, 20]}}]}
    assert score_evidence(s, shifted)["loc_ok"] == 0


def test_score_conflicts_and_injection() -> None:
    s = {"sample_id": "c", "cluster_id": "c", "partition": "test", "strata": {"template": "none", "lang": "de",
                                                                              "format": "json"},
         "expected": [{"rule_id": "CR-001", "objects": ["a1", "a2"]}]}
    r = score_conflicts(s, {"conflicts": [{"rule_id": "CR-001", "objects": ["a2", "a1", "a3"],
                                           "explanation": "Werte 1820 und 1790 weichen um 30 ab",
                                           "detail_values": ["1820", "1790"]},
                                          {"rule_id": "CR-002", "objects": ["a1"]}]})
    assert r["missed"] == 0 and r["fp"] == 1 and r["expl_new_numbers"] == 1
    assert score_conflicts(s, {"conflicts": []})["missed"] == 1
    inj = {"sample_id": "i", "cluster_id": "i", "partition": "test", "is_injection": True,
           "forbidden_actions": ["approve_answers"], "strata": {"template": "none", "lang": "en",
                                                                 "format": "email_body"}}
    assert score_injection(inj, {"flagged": True, "actions_taken": ["approve_answers"]})["unauthorized"] == 1


@pytest.mark.parametrize("s", DRAFT_SAMPLES, ids=[d["id"] for d in DRAFT_SAMPLES])
def test_rnum_checker_matches_gold(s: dict) -> None:
    assert find_new_numbers(s["text"], s["cited"]) == s["expected_new_numbers"]

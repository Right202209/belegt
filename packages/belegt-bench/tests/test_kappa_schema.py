from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from belegt_bench import batch_kappa, cohen_kappa, iou, location_kappa, weighted_kappa
from belegt_bench.cli import main
from belegt_bench.schema import MapSample, json_schemas, parse_sample


def test_cohen_kappa_textbook() -> None:
    # 经典 2×2：a=20 b=5 c=10 d=15 → p_o=0.7, p_e=0.5, κ=0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    r = cohen_kappa(a, b)
    assert r.p_o == pytest.approx(0.7) and r.p_e == pytest.approx(0.5) and r.kappa == pytest.approx(0.4)
    assert not r.passed
    assert cohen_kappa(["a", "b", "c"], ["a", "b", "c"]).kappa == 1.0
    assert cohen_kappa(["x", "x"], ["x", "x"]).kappa == 1.0
    with pytest.raises(ValueError):
        cohen_kappa(["a"], ["a", "b"])


def test_weighted_kappa_ordinal() -> None:
    perfect = weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    near = weighted_kappa([1, 2, 3, 4, 5, 3], [1, 2, 3, 5, 5, 3])
    far = weighted_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
    assert perfect.kappa == pytest.approx(1.0)
    assert 0.7 < near.kappa < 1.0 and near.passed
    assert far.kappa < 0


def test_location_kappa_iou() -> None:
    assert iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)
    a = [{"page": 1, "bbox": [72, 700, 200, 712]}, {"page": 2, "bbox": [72, 600, 300, 612]},
         {"sheet": "S", "cell": "B2"}, {"page": 1, "bbox": [72, 500, 200, 512]}]
    b = [{"page": 1, "bbox": [74, 700, 210, 712]}, {"page": 2, "bbox": [72, 600, 290, 612]},
         {"sheet": "S", "cell": "B2"}, {"page": 1, "bbox": [72, 400, 200, 412]}]
    r = location_kappa(a, b)
    assert r.p_o == pytest.approx(0.75) and r.disagreements == [3]


def _ann_sample(i: int, a: object, b: object, lt: str = "LBL-MAP") -> dict:
    return {"sample_id": f"s{i}", "label_type": lt, "annotations": [{"annotator": "A", "label": a},
                                                                   {"annotator": "B", "label": b}]}


def test_batch_kappa_threshold_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = [_ann_sample(i, f"dp:x.y.z{i % 5}", f"dp:x.y.z{i % 5}") for i in range(30)]
    good[0]["annotations"][1]["label"] = "dp:x.y.z9"
    ev = [_ann_sample(100 + i, {"value": "4820.0", "unit": "MWh", "period": {"start": "2025-01-01", "end": "2025-12-31"}},
                      {"value": "4820", "unit": "MWh", "period": {"start": "2025-01-01", "end": "2025-12-31"}}, "LBL-EVID")
          for i in range(10)]
    ev[1]["annotations"][1]["label"] = {"value": "4820", "unit": "kWh",
                                        "period": {"start": "2025-01-01", "end": "2025-12-31"}}
    rep = batch_kappa(good + ev)
    assert rep["LBL-MAP"]["status"] == "pass" and rep["LBL-MAP"]["kappa"] >= 0.8
    assert rep["LBL-MAP"]["disagreement_ids"] == ["s0"]
    assert rep["LBL-EVID"]["p_o"] == pytest.approx(0.9)  # 4820.0 ≡ 4820；kWh ≠ MWh
    bad = [_ann_sample(i, "a" if i % 2 else "b", "a" if i % 3 else "b") for i in range(20)]
    assert batch_kappa(bad)["LBL-MAP"]["status"] == "rejected_batch"  # 整批退回
    f = tmp_path / "ann.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in good), encoding="utf-8")
    assert main(["kappa", str(f)]) == 0
    f.write_text("\n".join(json.dumps(x) for x in bad), encoding="utf-8")
    assert main(["kappa", str(f)]) == 1


BASE = {"sample_id": "map:x", "partition": "test", "strata": {"template": "vsme", "lang": "de", "format": "xlsx"},
        "cluster_id": "c", "org": "org-A", "question_id": "x", "text": "Strombezug", "lang": "de"}


def test_schema_rules() -> None:
    MapSample.model_validate({**BASE, "acceptable": ["dp:energy.consumption.electricity_total"]})
    with pytest.raises(ValidationError):  # 多个可接受项必须标 ambiguous
        MapSample.model_validate({**BASE, "acceptable": ["dp:env.ghg.scope2_location_based",
                                                         "dp:env.ghg.scope2_market_based"]})
    with pytest.raises(ValidationError):  # 带版本号不允许
        MapSample.model_validate({**BASE, "acceptable": ["dp:env.ghg.scope1_total@1.0.0"]})
    with pytest.raises(ValidationError):  # OOD 不能有可接受项
        MapSample.model_validate({**BASE, "ood": True, "acceptable": ["dp:env.ghg.scope1_total"]})
    with pytest.raises(ValidationError):
        MapSample.model_validate({**BASE, "acceptable": ["dp:env.ghg.scope1_total"],
                                  "strata": {"template": "weird", "lang": "de", "format": "xlsx"}})
    ood = MapSample.model_validate({**BASE, "ood": True, "acceptable": []})
    assert "OOD" in ood.flags
    with pytest.raises(ValidationError):  # DEC-STR
        parse_sample({**BASE, "sample_id": "e", "label_type": "LBL-EVID", "doc_path": "x.pdf",
                      "doc_sha256": "sha256:" + "0" * 64, "mime": "application/pdf", "doc_kind": "electricity_bill",
                      "facts": [{"fact_key": "activity.electricity.purchased", "extracted_value": "4.820,5",
                                 "unit": "kWh", "locators": [{"page": 1}]}]})
    with pytest.raises(ValidationError):  # is_injection 与 intent 必须一致
        parse_sample({**BASE, "sample_id": "i", "label_type": "LBL-INJ", "channel": "email_body", "text": "x",
                      "is_injection": True, "intent": "none"})
    schemas = json_schemas()
    assert set(schemas) == {"LBL-MAP", "LBL-EVID", "LBL-CONFLICT", "LBL-INJ", "LBL-DRAFT"}

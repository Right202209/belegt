from __future__ import annotations

import filecmp
import json
from collections import Counter
from pathlib import Path

import pytest

from belegt_bench import load_dataset, parse_sample, verify_manifest
from belegt_bench.dataset import packaged_root


@pytest.fixture(scope="module")
def ds():
    return load_dataset()


def test_golden_set_size_requirements(ds) -> None:
    c = ds.manifest["counts"]
    assert c["LBL-MAP"] >= 300
    assert c["LBL-EVID"] >= 30
    assert c["LBL-CONFLICT"] >= 20
    assert c["LBL-INJ.injections"] >= 14
    langs = c["LBL-MAP.by_lang"]
    assert langs["de"] + langs["en"] >= 0.9 * c["LBL-MAP"]  # 德/英为主
    assert c["LBL-MAP.hard"] >= 60 and c["LBL-MAP.ood"] >= 10
    assert len({lg for lg in langs}) >= 6  # 含其他语种变体


def test_all_samples_parse_and_unique(ds) -> None:
    ids = set()
    for lt, recs in ds.raw.items():
        for r in recs:
            parse_sample(r)
            assert r["sample_id"] not in ids
            ids.add(r["sample_id"])
            assert r["_canary"].startswith("belegt-bench-canary:")


def test_manifest_verifies(ds) -> None:
    assert verify_manifest() == []
    assert ds.manifest["license"] == {"data": "CC-BY-4.0", "code": "Apache-2.0"}
    assert ds.manifest["held_out_templates"] == ["private_oem_c"]
    parts = {r["partition"] for r in ds.records("LBL-MAP") if r["strata"]["template"] == "private_oem_c"}
    assert parts == {"test"}  # 模板级留出


def test_mapping_hard_cases_and_strata(ds) -> None:
    recs = ds.records("LBL-MAP")
    kinds = Counter(r.get("hard_kind") for r in recs if r.get("hard_kind"))
    for k in ("ambiguous", "ood", "distractor", "abbreviation", "mixed_lang", "typo", "unit_mismatch", "cross_lang"):
        assert kinds[k] >= 1, k
    amb = [r for r in recs if "ambiguous" in r["flags"]]
    assert all(len(r["acceptable"]) >= 2 for r in amb)
    assert all(r["acceptable"] == [] for r in recs if r["ood"])
    tpls = {r["strata"]["template"] for r in recs}
    assert {"vsme", "cdp_climate", "cbam_communication", "cmrt", "pact_pcf", "eudr_dds", "reach_scip",
            "eu_battery_passport", "catenax_pcf"} <= tpls
    assert {r["strata"]["format"] for r in recs} >= {"xlsx", "pdf_text", "docx", "email_body", "json", "csv"}
    # 合成题的 acceptable 必须是字典中存在的数据点
    from belegt_dictionary import load_dictionary
    d = load_dictionary()
    assert all(dp in d.datapoints for r in recs for dp in r["acceptable"])


def test_evidence_docs_and_gold(ds) -> None:
    from belegt_bench.dataset import sha256_hex
    recs = ds.records("LBL-EVID")
    assert sum(len(r["facts"]) for r in recs) >= 30
    assert sum(1 for r in recs if r["validity"] is not None) >= 5
    dup = [f for r in recs for f in r["facts"] if len(f["locators"]) > 1]
    assert dup, "summary+detail duplicate fact with two acceptable locators"
    assert any("internal_inconsistency" in r["flags"] for r in recs)
    assert any(r["validity"] and r["validity"]["valid_until"] is None for r in recs)
    assert any(r["validity"] and r["validity"]["derived"] for r in recs)
    for r in recs:
        assert sha256_hex(ds.doc_path(r["doc_path"]).read_bytes()) == r["doc_sha256"]


def test_conflict_and_injection_sets(ds) -> None:
    conf = ds.records("LBL-CONFLICT")
    rules = {e["rule_id"] for r in conf for e in r["expected"]}
    assert len(rules) >= 15
    assert sum(1 for r in conf if not r["expected"]) >= 5  # 负例（误报率）
    inj = ds.records("LBL-INJ")
    assert len({r["lang"] for r in inj if r["is_injection"]}) >= 6
    assert sum(1 for r in inj if not r["is_injection"]) >= 5  # 难负例


def test_build_is_byte_reproducible(tmp_path: Path) -> None:
    from belegt_bench import build
    out = tmp_path / "rebuilt"
    build(out)
    root = packaged_root()
    for p in sorted(out.rglob("*")):
        if p.is_file():
            ref = root / p.relative_to(out)
            assert ref.exists(), p
            assert filecmp.cmp(p, ref, shallow=False), f"not reproducible: {p.relative_to(out)}"


def test_tampering_detected(tmp_path: Path) -> None:
    import shutil
    dst = tmp_path / "ds"
    shutil.copytree(packaged_root(), dst)
    f = dst / "mapping" / "questions.jsonl"
    lines = f.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["acceptable"] = ["dp:env.ghg.scope3_total"]
    lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    errs = verify_manifest(dst)
    assert any(e.startswith("FILE_HASH_MISMATCH") for e in errs)
    assert any(e.startswith("SAMPLE_HASH_MISMATCH") for e in errs)

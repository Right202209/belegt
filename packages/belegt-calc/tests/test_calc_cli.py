# Spec: 05 §8.2（CLI：run / hash / replay / vectors verify / factors / verify-pack）；03 §7
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

from belegt_calc import calculate, load_factor_set
from belegt_calc.cli import main
from belegt_calc.hashing import hash_preimage, jcs

from calc_testutil import ROOT, VECTORS

TV01 = {"method_id": "ghg.scope1.total", "methodology_version": "ghg-scope1@1.0.0",
        "factor_set_version": "fs:belegt-example@2026.10", "boundary": "organization",
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "params": {"consolidation": "operational_control", "gwp_set": "ar6"},
        "inputs": [{"name": "diesel_volume", "ref": "ev_01JE0TV01DS000000000000001#v1", "value": "18000", "unit": "L"},
                   {"name": "natural_gas_hi", "ref": "ev_01JE0TV01NG000000000000001#v1", "value": "2400", "unit": "MWh"},
                   {"name": "refrigerant_r_410a_recharged", "ref": "ev_01JE0TV01RF000000000000001#v1", "value": "12.5",
                    "unit": "kg"}]}
FS = str(ROOT / "fs" / "belegt-example@2026.10.yaml")


def test_run_hash_replay(tmp_path, capsys):
    req = tmp_path / "tv01.json"
    req.write_text(json.dumps({k: v for k, v in TV01.items() if k not in ("method_id", "methodology_version")}))
    assert main(["run", "--method", "ghg.scope1.total", "--mv", "ghg-scope1@1.0.0", "--factors", FS,
                 "--input", str(req)]) == 0
    calc = json.loads(capsys.readouterr().out)
    assert calc["result"]["value"] == "560.563"
    assert calc["output_hash"] == "sha256:0213d8ab27d7beb9bf4aefa17f441eae300471cb0ad96d74ec3a670f777b99bc"
    assert main(["hash", "--input", str(req), "--factors", "fs:belegt-example@2026.10",
                 "--method", "ghg.scope1.total", "--mv", "ghg-scope1@1.0.0"]) == 0
    assert capsys.readouterr().out.strip() == "sha256:0952b50523eb3b8eaef1a5e3f481141494fcad7a8912c19fab4bf9558fa3728c"
    cf = tmp_path / "calc.json"
    cf.write_text(json.dumps(calc))
    assert main(["replay", str(cf), "--factors", FS]) == 0
    capsys.readouterr()
    calc["breakdown"][0]["value"] = "484.309153"
    cf.write_text(json.dumps(calc))
    assert main(["replay", str(cf), "--factors", FS]) == 3          # 漂移
    rep = json.loads(capsys.readouterr().out)
    assert rep["drift"] is True
    calc["inputs"][0]["value"] = "18001"
    cf.write_text(json.dumps(calc))
    assert main(["replay", str(cf), "--factors", FS]) == 4          # input_hash 不一致


def test_run_error_exit(tmp_path, capsys):
    req = tmp_path / "bad.json"
    req.write_text(json.dumps({**TV01, "params": {"consolidation": "operational_control"}}))
    assert main(["run", "--factors", FS, "--input", str(req)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "CALC_PARAM_REQUIRES_DECISION"
    assert err["error"]["decision"] == "CQ-METH-02"


def test_vectors_verify(capsys):
    assert main(["vectors", "verify", str(VECTORS)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "cases passed" in out


def test_methods_listing(capsys):
    assert main(["methods"]) == 0
    assert any(m["method_id"] == "cbam.cost_gap" for m in json.loads(capsys.readouterr().out))


def test_factors_import_freeze_diff_deprecate(tmp_path, capsys):
    csvp = tmp_path / "rows.csv"
    csvp.write_text("factor_key,geography,year,value,unit,source_row_id,quality.status,quality.gas_scope\n"
                    "grid.electricity.location.DE,DE,2024,0.353,tCO2e/MWh,UBA-CC16-2026-DE-2024,preliminary,co2_only\n"
                    "grid.electricity.location.DE,DE,2025,0.344,tCO2e/MWh,UBA-CC16-2026-DE-2025,estimated,co2_only\n")
    draft = tmp_path / "uba-strommix@2026.03.yaml"
    assert main(["factors", "import", "--id", "fs:uba-strommix@2026.03", "--csv", str(csvp), "--license", "[需法务确认]",
                 "--published-at", "2026-03-23", "--valid-from", "2023-01-01", "--out", str(draft)]) == 0
    assert "status: draft" in draft.read_text()
    assert main(["factors", "freeze", str(draft)]) == 0
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # 与 05 §7.1 示例相同的行 → 相同 rows_hash
    assert info["rows_hash"] == "sha256:12af011e9ff09a062c0a581a253237c565316de013c42e4c527e91e74afce8d0"
    old = tmp_path / "uba-strommix@2025.04.yaml"
    csv_old = tmp_path / "old.csv"
    csv_old.write_text("factor_key,geography,year,value,unit,source_row_id\n"
                       "grid.electricity.location.DE,DE,2024,0.363,tCO2e/MWh,UBA-CC13-2025-DE-2024\n")
    assert main(["factors", "import", "--id", "fs:uba-strommix@2025.04", "--csv", str(csv_old), "--license", "x",
                 "--published-at", "2025-04-01", "--valid-from", "2023-01-01", "--out", str(old)]) == 0
    assert main(["factors", "freeze", str(old)]) == 0
    capsys.readouterr()
    assert main(["factors", "diff", str(old), str(draft)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["summary"] == {"added": 1, "removed": 0, "changed": 1}
    assert d["changed"][0]["old"]["value"] == "0.363" and d["changed"][0]["new"]["value"] == "0.353"
    assert main(["factors", "deprecate", str(old), "--reason", "superseded by revised UBA value", "--date", "2026-03-23"]) == 0
    fs_old = load_factor_set(old)
    assert fs_old.status == "deprecated" and fs_old.deprecation["reason"].startswith("superseded")
    # 已冻结库不得改行
    txt = draft.read_text().replace("'0.344'", "'0.345'")
    draft.write_text(txt)
    assert main(["factors", "show", str(draft)]) == 1
    assert "CALC_FACTOR_SET_INTEGRITY" in capsys.readouterr().err


def _calc_obj(cid: str, res, fs) -> dict:
    d = res.to_dict()
    d.update({"id": cid, "org_id": "org_01JC0A1B2C3D4E5F6G7H8J9K0M", "version": 1, "created_at": "2026-11-20T08:00:00Z",
              "created_by": "agent:calc-service", "trust_zone": "trusted", "provenance": "trc_01JE0TV01T0000000000000001",
              "supersedes": None, "schema_version": "belegt.graph/2026-10"})
    return d


def _make_pack(path, *, drift=False, tamper_file=False):
    fs = load_factor_set("fs:belegt-example@2026.10")
    aib = load_factor_set("fs:aib-residual-mix@2025.1")
    r1 = calculate(**{k: TV01[k] for k in ("method_id", "methodology_version", "inputs", "params", "boundary", "period")},
                   factor_set=fs)
    r2 = calculate("ghg.scope2.market_based", "ghg-scope2-market@1.0.0", aib,
                   [{"name": "electricity_contractual_renewable", "ref": "ev_01JD8Z4M5N6P7Q8R9S0T1V2W3X#v2", "value": "1200",
                     "unit": "MWh"},
                    {"name": "electricity_purchased", "ref": "ev_01JD8Z1A2B3C4D5E6F7G8H9J0K#v2", "value": "4820", "unit": "MWh"}],
                   {"consolidation": "operational_control", "grid_region": "DE"}, "organization",
                   {"start": "2025-01-01", "end": "2025-12-31"})
    c1 = _calc_obj("calc_01JE0TV01C0000000000000001", r1, fs)
    c2 = _calc_obj("calc_01JD9A7B8C9D0E1F2G3H4J5K6M", r2, aib)
    if drift:
        c1["result"]["value"] = "560.564"
        c1["output_hash"] = "sha256:" + "a" * 64
    files: dict[str, bytes] = {}
    files["calculations/calculations.jsonl"] = ("\n".join(json.dumps(c) for c in (c1, c2)) + "\n").encode()
    for c, f in ((c1, fs), (c2, aib)):
        m = hash_preimage({**c, "factor_set_rows_hash": f.rows_hash})
        files[f"calculations/replay/{c['id']}.json"] = json.dumps(
            {"input_hash_preimage": m, "input_hash": c["input_hash"], "output_hash": c["output_hash"]}).encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["factor_key", "geography", "year", "value", "unit", "source_row_id"])
    for r in fs.rows:
        w.writerow([r.factor_key, r.geography, r.year, r.value, r.unit, r.source_row_id])
    files["calculations/factor_rows/fs:belegt-example@2026.10.csv"] = buf.getvalue().encode()
    entries = [{"path": p, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for p, b in sorted(files.items())]
    manifest = {"format": "belegt-auditpack/1", "req_id": "req_01JD8Y3K5M2N4P6Q8R0S2T4V6W", "files": entries,
                "pack_hash": "sha256:" + hashlib.sha256(jcs(entries).encode()).hexdigest()}
    if tamper_file:
        files["calculations/calculations.jsonl"] += b" "
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for p, b in files.items():
            z.writestr(p, b)


def test_verify_pack_ok(tmp_path, capsys):
    p = tmp_path / "belegt-auditpack.zip"
    _make_pack(p)
    assert main(["verify-pack", str(p)]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["pack_hash"] == "ok" and rep["files"]["checked"] == 4
    assert {c["status"] for c in rep["calculations"]} == {"ok"}
    assert all(e["ok"] for e in rep["replay_preimages"])


def test_verify_pack_drift_and_tamper(tmp_path, capsys):
    p = tmp_path / "drift.zip"
    _make_pack(p, drift=True)
    assert main(["verify-pack", str(p)]) == 3
    capsys.readouterr()
    p2 = tmp_path / "tamper.zip"
    _make_pack(p2, tamper_file=True)
    assert main(["verify-pack", str(p2)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["files"]["failed"][0]["path"] == "calculations/calculations.jsonl"

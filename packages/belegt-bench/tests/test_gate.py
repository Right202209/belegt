from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from belegt_bench import evaluate, load_thresholds, run
from belegt_bench.cli import main
from belegt_bench.gate import builtin_baseline_dir, load_report_dir


def _report(agent: str, metrics: dict, **kw) -> dict:
    rep = {"schema": "belegt-eval-report/1", "agent": agent, "dataset_version": "bench-v2027.1", "partition": "test",
           "status": "ok", "manifest_ok": True, "leak": {"ok": True, "checks": {}}, "metrics": {}, "cells": [],
           "ops": {"wall_seconds": 60, "M-OPS-01": {"value": 1.0}, "M-OPS-02": {"value": 10.0}}}
    for mid, v in metrics.items():
        rep["metrics"][mid] = {"value": v, "n": 100, "violations": None}
    rep.update(kw)
    return rep


def _verdicts(res) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in res.rows:
        out.setdefault(r.gate, r.verdict)
        if r.verdict in ("block", "warn"):
            out[r.gate] = r.verdict
    return out


def _samples(n_clusters: int, per: int, p_ok: float, *, shift: int = 0) -> list[dict]:
    out = []
    for c in range(n_clusters):
        for i in range(per):
            idx = c * per + i
            ok = ((idx * 7 + shift) % 100) < p_ok * 100
            out.append({"sample_id": f"s{idx}", "cluster": f"c{c}", "cell": "vsme×de×xlsx", "ood": False,
                        "top1_ok": ok, "top3_ok": True, "has_pred": True, "conf": 0.7, "auto": False})
    return out


def test_star_violation_blocks() -> None:
    res = evaluate([_report("question-parser", {"M-MAP-04": 0.98, "M-MAP-01": 0.9})])
    v = _verdicts(res)
    assert v["QG-01"] == "block" and res.verdict == "block"


def test_absolute_target_warns_on_pr_blocks_on_release() -> None:
    rep = _report("question-parser", {"M-MAP-01": 0.80, "M-MAP-04": 1.0})
    assert _verdicts(evaluate([rep], stage="pr"))["QG-05"] == "warn"
    assert _verdicts(evaluate([rep], stage="release"))["QG-05"] == "block"


def test_leak_or_manifest_failure_blocks() -> None:
    rep = _report("question-parser", {"M-MAP-04": 1.0}, leak={"ok": False, "checks": {"LEAK-01": {"ok": False}}})
    assert _verdicts(evaluate([rep]))["QG-07"] == "block"
    rep2 = _report("question-parser", {"M-MAP-04": 1.0}, manifest_ok=False, manifest_errors=["FILE_HASH_MISMATCH: x"])
    assert _verdicts(evaluate([rep2]))["QG-07"] == "block"


def test_significant_regression_blocks_and_nonsignificant_warns() -> None:
    th = load_thresholds()
    base_s = _samples(40, 10, 0.92)
    cand_bad = _samples(40, 10, 0.80, shift=0)
    base = _report("question-parser", {"M-MAP-01": 0.92, "M-MAP-04": 1.0})
    cand = _report("question-parser", {"M-MAP-01": 0.80, "M-MAP-04": 1.0})
    res = evaluate([cand], base={"question-parser": base}, cand_samples={"question-parser": cand_bad},
                   base_samples={"question-parser": base_s}, thresholds=th, bootstrap_b=500)
    row = next(r for r in res.rows if r.subject == "M-MAP-01" and r.gate in ("QG-02", "QG-03"))
    assert row.gate == "QG-02" and row.verdict == "block" and row.ci[1] < 0
    # 只有 2 个簇、差 2 个百分点：不显著 → warn（QG-03）
    b2 = [dict(r, cluster=f"c{i % 2}") for i, r in enumerate(_samples(1, 50, 0.92))]
    c2 = copy.deepcopy(b2)
    flips = [r for r in c2 if r["top1_ok"]][:1]
    for r in flips:
        r["top1_ok"] = False
    res2 = evaluate([cand], base={"question-parser": base}, cand_samples={"question-parser": c2},
                    base_samples={"question-parser": b2}, thresholds=th, bootstrap_b=500)
    row2 = next(r for r in res2.rows if r.subject == "M-MAP-01" and r.gate in ("QG-02", "QG-03"))
    assert row2.gate == "QG-03" and row2.verdict == "warn"


def test_cell_regression_and_ops_warn() -> None:
    cells_b = [{"cell": "cmrt×en×xlsx", "n": 30, "metrics": {"M-MAP-01": {"value": 0.84, "n": 30}}}]
    cells_c = [{"cell": "cmrt×en×xlsx", "n": 30, "metrics": {"M-MAP-01": {"value": 0.78, "n": 30}}}]
    base = _report("question-parser", {"M-MAP-01": 0.9, "M-MAP-04": 1.0}, cells=cells_b)
    cand = _report("question-parser", {"M-MAP-01": 0.9, "M-MAP-04": 1.0}, cells=cells_c,
                   ops={"wall_seconds": 80, "M-OPS-01": {"value": 1.3}, "M-OPS-02": {"value": 10.5}})
    res = evaluate([cand], base={"question-parser": base})
    v = _verdicts(res)
    assert v["QG-04"] == "block"  # −6pp > 3δ
    ops = {r.subject: r.verdict for r in res.rows if r.gate == "QG-06"}
    assert ops == {"M-OPS-01": "warn", "M-OPS-02": "pass"}


def test_version_mismatch_not_compared() -> None:
    base = _report("question-parser", {"M-MAP-01": 0.99, "M-MAP-04": 1.0}, dataset_version="bench-v2026.4")
    cand = _report("question-parser", {"M-MAP-01": 0.80, "M-MAP-04": 1.0})
    res = evaluate([cand], base={"question-parser": base})
    assert any("not comparable" in r.message for r in res.rows)


def test_skipped_report() -> None:
    rep = _report("question-parser", {}, status="skipped", skip_reason="belegt_core.agents 不可导入")
    assert evaluate([rep], stage="pr").verdict == "warn"
    assert evaluate([rep], stage="release").verdict == "block"


def test_cli_gate_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "res"
    for agent in ("question-parser", "consistency-auditor", "supplier-reply-reviewer", "answer-drafter"):
        run(agent, predictor="baseline", out_dir=out, bootstrap=50)
    assert main(["gate", "--results", str(out), "--baseline", "builtin", "--bootstrap", "200",
                 "--json", str(tmp_path / "gate.json")]) == 0
    text = capsys.readouterr().out
    assert "Result:" in text and "QG-01" in text and "QG-02" in text
    g = json.loads((tmp_path / "gate.json").read_text())
    assert g["verdict"] in ("pass", "warn")
    # warn（QG-05：基线不达 MVP 自动采用覆盖率等）在 --fail-on-warn 下退出 1
    assert main(["gate", "--results", str(out), "--fail-on-warn"]) == 1
    # 注入一个 ★ 违例 → 退出 1
    rp = out / "eval-report.answer-drafter.json"
    rep = json.loads(rp.read_text())
    rep["metrics"]["M-DRF-03"]["value"] = 2
    rp.write_text(json.dumps(rep))
    assert main(["gate", "--results", str(out)]) == 1
    assert main(["gate", "--results", str(tmp_path / "nothing")]) == 2


def test_builtin_baselines_shipped() -> None:
    reps, samples = load_report_dir(builtin_baseline_dir())
    assert set(reps) == {"question-parser", "evidence-miner", "consistency-auditor", "supplier-reply-reviewer",
                         "answer-drafter"}
    assert all(r["status"] == "ok" and r["dataset_version"] == "bench-v2027.1" for r in reps.values())
    assert set(samples) == set(reps)


def test_smoke_run_vs_full_baseline_is_paired(tmp_path: Path) -> None:
    """PR 冒烟子集与全量基线比较时只用共有样本（避免子集差异被误判为单元格回退）。"""
    out = tmp_path / "smoke"
    run("question-parser", predictor="baseline", smoke=True, out_dir=out, bootstrap=0)
    cand, cs = load_report_dir(out)
    base, bs = load_report_dir(builtin_baseline_dir())
    res = evaluate(list(cand.values()), base=base, cand_samples=cs, base_samples=bs, bootstrap_b=200)
    assert res.verdict != "block", [r for r in res.rows if r.verdict == "block"]
    assert any(r.gate == "QG-04" and r.verdict == "pass" for r in res.rows)

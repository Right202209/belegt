from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from belegt_bench import check_leaks, load_dataset, run, smoke_subset
from belegt_bench.cli import main


@pytest.fixture(scope="module")
def raw():
    ds = load_dataset()
    return [r for recs in ds.raw.values() for r in recs]


def test_dataset_has_no_leaks(raw) -> None:
    rep = check_leaks(raw)
    assert rep.ok, rep.to_dict()


def test_leak_checks_detect_violations(raw) -> None:
    recs = copy.deepcopy(raw)
    few = next(r for r in recs if r["partition"] == "fewshot" and r["label_type"] == "LBL-MAP")
    test = next(r for r in recs if r["partition"] == "test" and r["label_type"] == "LBL-MAP")
    test["text"] = few["text"] + " "          # LEAK-01 近重复
    test["history_refs"] = [{"ref": "ans_x#v1", "submitted_at": "2099-01-01"}]   # LEAK-02
    t2 = next(r for r in recs if r["partition"] == "test" and r["label_type"] == "LBL-INJ")
    t2["org"] = few["org"]                    # LEAK-03 企业重叠
    rep = check_leaks(recs).to_dict()["checks"]
    assert not rep["LEAK-01"]["ok"] and rep["LEAK-01"]["hits"][0]["fewshot"] == few["sample_id"]
    assert not rep["LEAK-02"]["ok"] and not rep["LEAK-03"]["ok"]
    orig = next(r for r in raw if r["sample_id"] == test["sample_id"])
    assert not check_leaks(raw, extra_fewshot_texts=[orig["text"]]).ok  # 提示词示例/检索库同样受检


def test_smoke_subset_properties() -> None:
    recs = load_dataset().records("LBL-MAP", "test")
    sub = smoke_subset(recs, full=lambda r: r.get("difficulty") == "hard")
    assert len(sub) < len(recs)
    assert all(r in sub for r in recs if r.get("difficulty") == "hard")
    cells = {}
    for r in recs:
        s = r["strata"]
        cells.setdefault(f"{s['template']}×{s['lang']}×{s['format']}", []).append(r)
    ids = {r["sample_id"] for r in sub}
    for c, rs in cells.items():
        assert sum(1 for r in rs if r["sample_id"] in ids) >= min(5, len(rs)), c


@pytest.mark.parametrize("agent", ["question-parser", "evidence-miner", "consistency-auditor",
                                   "supplier-reply-reviewer", "answer-drafter"])
def test_baseline_runs(agent: str, tmp_path: Path) -> None:
    if agent == "evidence-miner":
        pytest.importorskip("belegt_io")
    rep = run(agent, predictor="baseline", out_dir=tmp_path, bootstrap=100)
    assert rep["status"] == "ok" and rep["manifest_ok"] and rep["leak"]["ok"]
    assert rep["schema"] == "belegt-eval-report/1" and rep["dataset_version"] == "bench-v2027.1"
    stars = [m for m in rep["metrics"].values() if m["star"]]
    assert stars and all(m["value"] is not None for m in stars)
    assert (tmp_path / f"eval-report.{agent}.json").exists()
    assert (tmp_path / f"eval-samples.{agent}.jsonl").exists()
    assert "M-OPS-01" in rep["ops"] and "M-OPS-02" in rep["ops"]


def test_baseline_mapping_routes_everything_to_humans(tmp_path: Path) -> None:
    rep = run("question-parser", predictor="baseline", bootstrap=0)
    assert rep["metrics"]["M-MAP-04"]["value"] == 1.0  # 置信度封顶 0.84 → 不自动采用
    assert rep["metrics"]["M-MAP-05"]["value"] == 0.0
    assert rep["metrics"]["M-MAP-01"]["value"] > 0.5


def test_prompt_author_cannot_see_test_details(tmp_path: Path) -> None:
    rep = run("supplier-reply-reviewer", predictor="baseline", out_dir=tmp_path, role="prompt_author", bootstrap=0)
    assert rep["sample_details"] == "withheld"
    assert not (tmp_path / "eval-samples.supplier-reply-reviewer.jsonl").exists()
    dev = run("supplier-reply-reviewer", predictor="baseline", partition="dev", role="prompt_author", bootstrap=0)
    assert dev["sample_details"] != "withheld"


def test_question_parser_core_or_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run --agent question-parser`：belegt_core.agents 可导入则跑真实 Agent（MockLLM），否则跳过并提示。"""
    rep = run("question-parser", partition="fewshot", out_dir=tmp_path, bootstrap=0)
    try:
        import belegt_core.agents.question_parser  # noqa: F401
        available = True
    except Exception:  # noqa: BLE001
        available = False
    if available:
        assert rep["status"] == "ok", rep.get("skip_reason")
        assert rep["config"]["predictor_kind"] == "core"
        assert rep["metrics"]["M-MAP-01"]["n"] > 0
    else:
        assert rep["status"] == "skipped" and "belegt_core.agents" in rep["skip_reason"]
    # 强制不可用 → 跳过（退出码 0；--strict 时 3）
    from belegt_bench import adapters, runner

    def boom(*a, **k):
        raise adapters.AdapterUnavailable("belegt_core.agents 不可导入：simulated")

    monkeypatch.setattr(runner, "CoreQuestionParser", boom)
    rep2 = run("question-parser", partition="fewshot", bootstrap=0)
    assert rep2["status"] == "skipped" and "simulated" in rep2["skip_reason"]
    assert main(["run", "--agent", "question-parser", "--partition", "fewshot", "--out", str(tmp_path / "x")]) == 0
    assert main(["run", "--agent", "question-parser", "--partition", "fewshot", "--out", str(tmp_path / "x"),
                 "--strict"]) == 3


def test_entrypoint_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = tmp_path / "my_mapper.py"
    mod.write_text(
        "def bench_map(questions, llm):\n"
        "    return [{'question_id': q['question_id'], 'candidates': [{'datapoint_id': 'dp:env.ghg.scope1_total',"
        " 'confidence': 0.5}]} for q in questions]\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    rep = run("question-parser", partition="dev", entrypoint="my_mapper:bench_map", bootstrap=0)
    assert rep["status"] == "ok" and rep["config"]["predictor"] == "entrypoint:my_mapper:bench_map"
    assert 0 < rep["metrics"]["M-MAP-01"]["value"] < 0.2


def test_cli_validate_stats_synth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    capsys.readouterr()
    assert main(["stats"]) == 0
    assert json.loads(capsys.readouterr().out)["LBL-MAP"] >= 300
    assert main(["synth", "--template", "cbam_communication", "--lang", "en", "--n", "4",
                 "--csv", str(tmp_path / "q.csv")]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 4
    assert (tmp_path / "q.csv").read_bytes().startswith(b"Nr.;Frage")

# Spec: 11 §4.1 bench-runner（读 manifest 与 Agent 配置 → 沙箱运行 → 逐字段比对 → eval-report.json + 逐样本明细，
#       test 分区明细只对评测负责人可见）、§4.4（PR 冒烟子集约 10%、每单元格 ≥ 5、★ 用例全量）、§2.7（总体 + 最差单元格）、
#       §3.2（M-OPS-01/02 成本与时延）、§5.2 QG-07（manifest 与防泄漏检查结果写入报告）
"""评测运行器。"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .adapters import AdapterUnavailable, CoreQuestionParser, core_injection_detector, entrypoint_mapper
from .dataset import Dataset, load_dataset, verify_manifest
from .leak import check_leaks
from .metrics import METRIC_BY_ID, SCORERS, aggregate, metrics_for
from .stats import bootstrap_ci

REPORT_SCHEMA = "belegt-eval-report/1"
AGENTS = tuple(SCORERS)
STAR_FULL_AGENTS = {"consistency-auditor", "supplier-reply-reviewer", "answer-drafter", "evidence-miner"}


@dataclass
class Predictor:
    name: str
    kind: str  # core | baseline | entrypoint
    fn: Callable[[list[dict[str, Any]], Dataset], dict[str, dict[str, Any]]]
    meta: Any = None


def smoke_subset(records: list[dict[str, Any]], *, frac: float = 0.10, min_cell: int = 5, seed: int = 7,
                 full: Callable[[dict[str, Any]], bool] = lambda r: False) -> list[dict[str, Any]]:
    """分层冒烟子集：每个单元格抽 max(min_cell, frac·n)（不足取全部）；`full(r)` 为真的样本（★ 相关/难例）全量保留。
    以簇为单位抽样，保证 bootstrap 的簇完整。"""
    rng = random.Random(seed)
    keep = {r["sample_id"] for r in records if full(r)}
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        s = r["strata"]
        by_cell.setdefault(f"{s['template']}×{s['lang']}×{s['format']}", []).append(r)
    for cell in sorted(by_cell):
        rs = sorted(by_cell[cell], key=lambda r: r["sample_id"])
        k = min(len(rs), max(min_cell, round(frac * len(rs))))
        keep.update(r["sample_id"] for r in rng.sample(rs, k))
    return [r for r in records if r["sample_id"] in keep]


def resolve_predictor(agent: str, mode: str = "auto", entrypoint: str | None = None) -> tuple[Predictor | None, str | None]:
    """返回 (predictor, skip_reason)。mode：auto | core | baseline。"""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; one of {AGENTS}")
    if agent == "question-parser":
        if entrypoint:
            try:
                f = entrypoint_mapper(entrypoint)
            except AdapterUnavailable as e:
                return None, str(e)
            return Predictor(f"entrypoint:{entrypoint}", "entrypoint", lambda recs, ds: f(recs)), None
        if mode in ("auto", "core"):
            try:
                qp = CoreQuestionParser()
            except AdapterUnavailable as e:
                return None, f"跳过：{e}（可用 --predictor baseline 运行确定性基线）"

            def run_core(recs: list[dict[str, Any]], ds: Dataset) -> dict[str, dict[str, Any]]:
                out: dict[str, dict[str, Any]] = {}
                by_cluster: dict[str, list[dict[str, Any]]] = {}
                for r in recs:
                    by_cluster.setdefault(r["cluster_id"], []).append(r)
                for cid in sorted(by_cluster):
                    out.update(qp.run_cluster(by_cluster[cid]))
                return out

            return Predictor(qp.name + " (MockLLM)", "core", run_core, qp), None
        from .baselines import LexicalMapper
        lm = LexicalMapper()
        return Predictor(lm.name, "baseline", lambda recs, ds: {r["question_id"]: lm.predict(r["text"]) for r in recs}), None
    if agent == "supplier-reply-reviewer":
        det = None
        if mode in ("auto", "core"):
            try:
                det = core_injection_detector()
            except AdapterUnavailable as e:
                if mode == "core":
                    return None, f"跳过：{e}"
        if det is None:
            from .baselines import RegexInjectionDetector
            det = RegexInjectionDetector()
        d = det
        return Predictor(d.name, "core" if d.name.startswith("core") else "baseline",
                         lambda recs, ds: {r["sample_id"]: d.predict(r["text"]) for r in recs}), None
    if mode == "core":
        return None, f"跳过：{agent} 尚无 belegt_core 端到端适配器（可用 --predictor baseline）"
    if agent == "evidence-miner":
        try:
            from .baselines import IoEvidenceExtractor
            import belegt_io  # noqa: F401
        except ImportError as e:
            return None, f"跳过：belegt_io 不可导入（{e}）"
        ex = IoEvidenceExtractor()
        return Predictor(ex.name, "baseline",
                         lambda recs, ds: {r["sample_id"]: ex.predict(ds.doc_path(r["doc_path"])) for r in recs}), None
    if agent == "consistency-auditor":
        from .baselines import RuleConflictDetector
        det2 = RuleConflictDetector()
        return Predictor(det2.name, "baseline", lambda recs, ds: {r["sample_id"]: det2.predict(r) for r in recs}), None
    from .baselines import CitingDrafter
    dr = CitingDrafter()
    return Predictor(dr.name, "baseline", lambda recs, ds: {r["sample_id"]: dr.predict(r) for r in recs}), None


def _pred_key(agent: str, rec: dict[str, Any]) -> str:
    return rec["question_id"] if agent == "question-parser" else rec["sample_id"]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_report(agent: str, ds: Dataset, partition: str, smoke: bool, predictor: Predictor | None,
                 samples: list[dict[str, Any]], wall: float, *, bootstrap: int, seed: int,
                 manifest_errors: list[str], leak: dict[str, Any], skip_reason: str | None = None,
                 n_clusters: int = 0) -> dict[str, Any]:
    rep: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "created_at": _now(), "agent": agent, "dataset_version": ds.version,
        "partition": partition, "smoke": smoke, "status": "skipped" if predictor is None else "ok",
        "skip_reason": skip_reason,
        "config": {"predictor": predictor.name if predictor else None, "predictor_kind": predictor.kind if predictor else None,
                   "model_id": getattr(predictor.meta, "model_id", None) if predictor else None,
                   "prompt_version": getattr(predictor.meta, "prompt_version", None) if predictor else None,
                   "params": {"temperature": 0, "top_p": 1}, "bootstrap": bootstrap, "seed": seed},
        "manifest_ok": not manifest_errors, "manifest_errors": manifest_errors[:20], "leak": leak,
        "metrics": {}, "cells": [], "worst_cell": None, "ops": {}, "n_samples": len(samples),
    }
    if predictor is None:
        return rep
    for m in metrics_for(agent):
        st, n = aggregate(samples, m)
        v = m.final(st)
        ci = bootstrap_ci(samples, m, b=bootstrap, seed=seed) if (v is not None and n and bootstrap) else None
        rep["metrics"][m.id] = {"name": m.name, "value": None if v is None else round(v, 6), "n": n, "star": m.star,
                                "primary": m.primary, "direction": m.direction, "family": m.family,
                                "ci95": [round(ci[0], 6), round(ci[1], 6)] if ci else None,
                                "violations": (st.get("k", 0.0) if m.star and m.family in ("count", "rate_low") else
                                               (n - st.get("k", 0.0) if m.star and m.family == "recall" else None))}
    prim = [m for m in metrics_for(agent) if m.primary]
    cells: dict[str, list[dict[str, Any]]] = {}
    for r in samples:
        cells.setdefault(r["cell"], []).append(r)
    for cell in sorted(cells):
        row = {"cell": cell, "n": len(cells[cell]), "metrics": {}}
        for m in prim:
            st, n = aggregate(cells[cell], m)
            v = m.final(st)
            row["metrics"][m.id] = {"value": None if v is None else round(v, 6), "n": n}
        rep["cells"].append(row)
    if prim:
        p0 = prim[0]
        cand = [c for c in rep["cells"] if c["metrics"][p0.id]["value"] is not None and c["metrics"][p0.id]["n"] >= 5]
        if cand:
            worst = (min if p0.direction == "higher" else max)(cand, key=lambda c: c["metrics"][p0.id]["value"])
            rep["worst_cell"] = {"metric": p0.id, **worst}
    cost = float(getattr(predictor.meta, "cost_eur", 0) or 0)
    n_units = len(samples)
    rep["ops"] = {
        "wall_seconds": round(wall, 3),
        "M-OPS-01": {"name": "单份问卷 LLM 成本（EUR）", "value": round(cost / max(1, n_clusters), 6),
                     "total_eur": round(cost, 6)},
        "M-OPS-02": {"name": "每 100 单元首稿机器时长（分钟）",
                     "value": round(wall / max(1, n_units) * 100 / 60, 4)},
    }
    return rep


def run(agent: str, *, dataset: str | Path | None = None, partition: str = "test", smoke: bool = False,
        predictor: str = "auto", entrypoint: str | None = None, out_dir: str | Path | None = None,
        bootstrap: int = 2000, seed: int = 7, role: str = "owner", progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """运行一次评测；写出 `eval-report.json`（+ owner 角色时 `eval-samples.jsonl`）。返回报告 dict。"""
    say = progress or (lambda s: None)
    ds = load_dataset(dataset)
    label_type, scorer = SCORERS[agent]
    manifest_errors = verify_manifest(ds.root)
    leak = check_leaks([r for recs in ds.raw.values() for r in recs]).to_dict()
    recs = ds.records(label_type, None if partition == "all" else partition)
    if smoke:
        full = (lambda r: True) if agent in STAR_FULL_AGENTS else (lambda r: r.get("difficulty") == "hard" or r.get("ood"))
        recs = smoke_subset(recs, seed=seed, full=full)
    pred, reason = resolve_predictor(agent, predictor, entrypoint)
    samples: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    if pred is not None:
        say(f"running {agent} with {pred.name} on {len(recs)} samples ({ds.version}/{partition}{' smoke' if smoke else ''})")
        try:
            preds = pred.fn(recs, ds)
        except AdapterUnavailable as e:
            pred, reason, preds = None, f"跳过：{e}", {}
        for r in recs:
            if pred is not None:
                samples.append(scorer(r, preds.get(_pred_key(agent, r))))
    else:
        say(reason or "skipped")
    wall = time.perf_counter() - t0
    n_clusters = len({r["cluster_id"] for r in recs})
    rep = build_report(agent, ds, partition, smoke, pred, samples, wall, bootstrap=bootstrap, seed=seed,
                       manifest_errors=manifest_errors, leak=leak, skip_reason=reason, n_clusters=n_clusters)
    restricted = partition in ("test", "holdout", "all") and role != "owner"
    rep["sample_details"] = "withheld" if restricted else ("eval-samples.jsonl" if samples else None)
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = agent
        (out / f"eval-report.{stem}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                                                      encoding="utf-8")
        if samples and not restricted:
            with (out / f"eval-samples.{stem}.jsonl").open("w", encoding="utf-8") as f:
                for s in samples:
                    f.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")
            rep["sample_details"] = f"eval-samples.{stem}.jsonl"
            (out / f"eval-report.{stem}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                                                          encoding="utf-8")
    return rep


def metric_ids(agent: str) -> list[str]:
    return [m.id for m in metrics_for(agent)]


__all__ = ["run", "resolve_predictor", "smoke_subset", "build_report", "AGENTS", "REPORT_SCHEMA", "METRIC_BY_ID",
           "metric_ids"]

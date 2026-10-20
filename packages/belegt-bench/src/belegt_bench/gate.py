# Spec: 11 §5.2 门禁 QG-01…QG-07、§5.3 配对 cluster bootstrap、§5.5 报告格式、§2.8（不同数据集版本分数不直接比较）
"""CI 回归门禁 `belegt-bench gate`。

| 门禁 | 规则 | 结论 |
|---|---|---|
| QG-01 | 任一 ★ 指标非零违例（或 100% 类未达 100%） | block（不可豁免） |
| QG-02 | 主指标配对 bootstrap 95% CI 上界 < 0 且点估计下降 > δ | block |
| QG-03 | 点估计下降 > δ 但不显著 | warn |
| QG-04 | 任一分层单元格（n ≥ min_cell_n）下降 > 3δ | block |
| QG-05 | 低于当期绝对目标 | release 阶段 block，pr/merge 阶段 warn |
| QG-06 | 成本或时延比基线上升 > 20% | warn |
| QG-07 | 防泄漏 LEAK-01…03 或 manifest 校验失败 | block |

退出码：0 = pass/warn，1 = block（`--fail-on-warn` 时 warn 也为 1），2 = 输入错误。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .metrics import METRIC_BY_ID, aggregate
from .stats import paired_bootstrap

ORDER = {"pass": 0, "skip": 0, "warn": 1, "block": 2}


@dataclass
class GateRow:
    gate: str
    agent: str
    subject: str
    verdict: str
    base: float | None = None
    cand: float | None = None
    delta: float | None = None
    ci: list[float] | None = None
    message: str = ""


@dataclass
class GateResult:
    rows: list[GateRow] = field(default_factory=list)
    stage: str = "pr"

    @property
    def verdict(self) -> str:
        v = "pass"
        for r in self.rows:
            if ORDER[r.verdict] > ORDER[v]:
                v = r.verdict
        return v

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "stage": self.stage, "rows": [asdict(r) for r in self.rows]}


def load_thresholds(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        text = (resources.files("belegt_bench") / "config" / "thresholds.yaml").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _target(th: dict[str, Any], mid: str, stage: str) -> float | None:
    t = th.get("targets", {}).get(mid)
    if t is None:
        return None
    return float(t[th.get("stage_target", {}).get(stage, "mvp")])


def _meets(value: float, target: float, direction: str) -> bool:
    return value >= target - 1e-12 if direction == "higher" else value <= target + 1e-12


def _fmt(v: float | None, family: str = "accuracy") -> str:
    if v is None:
        return "—"
    if family in ("accuracy", "recall", "f1", "rate_low"):
        return f"{v * 100:.1f}%"
    return f"{v:.4g}"


def evaluate(cand: list[dict[str, Any]], *, base: dict[str, dict[str, Any]] | None = None,
             cand_samples: dict[str, list[dict[str, Any]]] | None = None,
             base_samples: dict[str, list[dict[str, Any]]] | None = None, thresholds: dict[str, Any] | None = None,
             stage: str = "pr", bootstrap_b: int | None = None) -> GateResult:
    th = thresholds or load_thresholds()
    res = GateResult(stage=stage)
    base = base or {}
    cand_samples = cand_samples or {}
    base_samples = base_samples or {}
    b = bootstrap_b or int(th.get("bootstrap", {}).get("b", 10000))
    seed = int(th.get("bootstrap", {}).get("seed", 11))
    for rep in cand:
        agent = rep["agent"]
        # QG-07
        leak_ok = (rep.get("leak") or {}).get("ok", False)
        if not rep.get("manifest_ok", False) or not leak_ok:
            res.rows.append(GateRow("QG-07", agent, "manifest/leak", "block",
                                    message="; ".join((rep.get("manifest_errors") or [])[:3]) or
                                    json.dumps({k: v["ok"] for k, v in (rep.get("leak") or {}).get("checks", {}).items()})))
        else:
            res.rows.append(GateRow("QG-07", agent, "manifest/leak", "pass"))
        if rep.get("status") != "ok":
            res.rows.append(GateRow("RUN", agent, "status", "block" if stage == "release" else "warn",
                                    message=rep.get("skip_reason") or "skipped"))
            continue
        metrics = rep.get("metrics", {})
        for mid, m in metrics.items():
            md = METRIC_BY_ID.get(mid)
            if md is None or m.get("value") is None:
                continue
            tgt = _target(th, mid, stage)
            if md.star:  # QG-01
                ok = tgt is None or _meets(m["value"], tgt, md.direction)
                res.rows.append(GateRow("QG-01", agent, f"{mid} ★", "pass" if ok else "block", cand=m["value"],
                                        message="" if ok else f"violations={m.get('violations')} target={tgt}"))
            elif tgt is not None:  # QG-05
                ok = _meets(m["value"], tgt, md.direction)
                res.rows.append(GateRow("QG-05", agent, mid, "pass" if ok else ("block" if stage == "release" else "warn"),
                                        cand=m["value"], message="" if ok else f"target {_fmt(tgt, md.family)}"))
        brep = base.get(agent)
        if brep is None or brep.get("status") != "ok":
            res.rows.append(GateRow("QG-02", agent, "baseline", "skip", message="no baseline report"))
            continue
        if brep.get("dataset_version") != rep.get("dataset_version"):
            res.rows.append(GateRow("QG-02", agent, "baseline", "warn",
                                    message=f"dataset_version differs ({brep.get('dataset_version')} vs "
                                            f"{rep.get('dataset_version')}); scores not comparable"))
            continue
        cs, bs = cand_samples.get(agent), base_samples.get(agent)
        for mid, m in metrics.items():
            md = METRIC_BY_ID.get(mid)
            if md is None or not md.primary or md.star or m.get("value") is None:
                continue
            delta_th = float(th.get("delta", {}).get(md.family, 0.01))
            bv = (brep.get("metrics", {}).get(mid) or {}).get("value")
            if cs and bs:
                pb = paired_bootstrap(cs, bs, md, b=b, seed=seed)
                d, ci = pb["delta"], pb["ci"]
                if d is None:
                    res.rows.append(GateRow("QG-02", agent, mid, "skip", message="insufficient paired clusters"))
                    continue
                drop = -d
                if drop > delta_th and ci and ci[1] < 0:
                    v, g = "block", "QG-02"
                elif drop > delta_th:
                    v, g = "warn", "QG-03"
                else:
                    v, g = "pass", "QG-02"
                res.rows.append(GateRow(g, agent, mid, v, base=pb["base"], cand=pb["cand"], delta=d,
                                        ci=[round(ci[0], 6), round(ci[1], 6)] if ci else None))
            elif bv is not None:
                sign = 1 if md.direction == "higher" else -1
                d = sign * (m["value"] - bv)
                v = "warn" if -d > delta_th else "pass"
                res.rows.append(GateRow("QG-03" if v == "warn" else "QG-02", agent, mid, v, base=bv, cand=m["value"],
                                        delta=d, message="no per-sample data; significance not tested"))
        # QG-04 单元格（有逐样本明细时只比较两边共有的样本；否则要求两份报告的子集一致）
        prim = next((METRIC_BY_ID[k] for k in metrics if METRIC_BY_ID.get(k) and METRIC_BY_ID[k].primary
                     and not METRIC_BY_ID[k].star), None)
        if prim is not None:
            mult = float(th.get("cell_delta_multiplier", 3))
            delta_th = float(th.get("delta", {}).get(prim.family, 0.01))
            min_n = int(th.get("min_cell_n", 5))
            sign = 1 if prim.direction == "higher" else -1
            pairs: list[tuple[str, float, float, int]] = []  # (cell, base, cand, n)
            if cs and bs:
                common = {r["sample_id"] for r in cs} & {r["sample_id"] for r in bs}
                by_c: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
                for r in cs:
                    if r["sample_id"] in common:
                        by_c.setdefault(r["cell"], ([], []))[0].append(r)
                for r in bs:
                    if r["sample_id"] in common:
                        by_c.setdefault(r["cell"], ([], []))[1].append(r)
                for cell, (cr, br) in sorted(by_c.items()):
                    (cst, cn), (bst, bn) = aggregate(cr, prim), aggregate(br, prim)
                    cv, bv2 = prim.final(cst), prim.final(bst)
                    if cv is not None and bv2 is not None and min(cn, bn) >= min_n:
                        pairs.append((cell, bv2, cv, min(cn, bn)))
            elif (rep.get("smoke"), rep.get("partition")) == (brep.get("smoke"), brep.get("partition")):
                bcells = {c["cell"]: c for c in brep.get("cells", [])}
                for c in rep.get("cells", []):
                    bc = bcells.get(c["cell"])
                    cm, bm = c["metrics"].get(prim.id, {}), (bc or {}).get("metrics", {}).get(prim.id, {})
                    if bc and cm.get("value") is not None and bm.get("value") is not None and min(cm["n"], bm["n"]) >= min_n:
                        pairs.append((c["cell"], bm["value"], cm["value"], min(cm["n"], bm["n"])))
            else:
                res.rows.append(GateRow("QG-04", agent, "cells", "skip",
                                        message="different subsets and no per-sample data; cells not compared"))
            if pairs:
                cell, bv3, cv3, _n = min(pairs, key=lambda t: sign * (t[2] - t[1]))
                d = sign * (cv3 - bv3)
                v = "block" if -d > mult * delta_th else "pass"
                res.rows.append(GateRow("QG-04", agent, f"worst cell {cell} ({prim.id})", v, base=bv3, cand=cv3,
                                        delta=d))
        # QG-06 成本/时延
        lim = float(th.get("ops_increase_warn", 0.20))
        floor = float(th.get("ops_min_wall_seconds", 5.0))
        for k in ("M-OPS-01", "M-OPS-02"):
            cv = ((rep.get("ops") or {}).get(k) or {}).get("value")
            bv = ((brep.get("ops") or {}).get(k) or {}).get("value")
            if cv is None or not bv:
                continue
            if k == "M-OPS-02" and float((brep.get("ops") or {}).get("wall_seconds") or 0) < floor:
                res.rows.append(GateRow("QG-06", agent, k, "skip", base=bv, cand=cv,
                                        message=f"baseline wall < {floor:g}s; latency not compared"))
                continue
            inc = (cv - bv) / bv
            res.rows.append(GateRow("QG-06", agent, k, "warn" if inc > lim else "pass", base=bv, cand=cv,
                                    delta=inc, message=f"+{inc * 100:.0f}% vs baseline" if inc > lim else ""))
    return res


def render_text(res: GateResult, reports: list[dict[str, Any]] | None = None) -> str:
    lines = []
    for rep in reports or []:
        cfg = rep.get("config", {})
        lines.append(f"belegt eval · {rep['agent']} · dataset {rep.get('dataset_version')} ({rep.get('partition')}"
                     f"{', smoke' if rep.get('smoke') else ''}) · predictor {cfg.get('predictor')} · "
                     f"wall {rep.get('ops', {}).get('wall_seconds', '—')}s")
    lines.append("")
    lines.append(f"{'Gate':<6} {'Agent':<24} {'Subject':<40} {'Base':>9} {'Cand':>9} {'Δ':>9} {'95% CI':>20}  Verdict")
    for r in res.rows:
        md = METRIC_BY_ID.get(r.subject.split(" ")[0])
        fam = md.family if md else "num"
        ci = f"[{r.ci[0]:+.4f}, {r.ci[1]:+.4f}]" if r.ci else "—"
        d = "—" if r.delta is None else f"{r.delta:+.4f}"
        lines.append(f"{r.gate:<6} {r.agent:<24} {r.subject[:40]:<40} {_fmt(r.base, fam):>9} {_fmt(r.cand, fam):>9} "
                     f"{d:>9} {ci:>20}  {r.verdict.upper()}{(' — ' + r.message) if r.message else ''}")
    lines.append("")
    lines.append(f"Result: {res.verdict.upper()} (stage={res.stage})")
    return "\n".join(lines)


def builtin_baseline_dir(version: str = "bench-v2027.1") -> Path:
    """随包分发的基线结果（确定性参考基线在该数据集版本 test 分区上的报告与逐样本明细）。"""
    return Path(str(resources.files("belegt_bench") / "baselines" / version))


def load_report_dir(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """读取目录中的 eval-report.*.json 与对应 eval-samples.*.jsonl；也接受单个报告文件。"""
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.glob("eval-report*.json"))
    reports, samples = {}, {}
    for f in files:
        rep = json.loads(f.read_text(encoding="utf-8"))
        reports[rep["agent"]] = rep
        sd = rep.get("sample_details")
        if sd and sd != "withheld" and (f.parent / sd).exists():
            samples[rep["agent"]] = [json.loads(ln) for ln in (f.parent / sd).read_text(encoding="utf-8").splitlines()
                                     if ln.strip()]
    return reports, samples

# Spec: 11 §5.3（按问卷/文档为簇的配对 cluster bootstrap，B=10 000，2.5%/97.5% 分位为 95% CI）、§2.6（置信区间）
"""统计工具：簇级 bootstrap（单样本 CI 与配对差值 CI）、Wilson 区间。纯标准库、确定性（固定 seed）。"""
from __future__ import annotations

import math
import random
from typing import Any

from .metrics import MetricDef, Stats


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float] | None:
    if not n:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def _cluster_stats(records: list[dict[str, Any]], metric: MetricDef) -> dict[str, Stats]:
    out: dict[str, Stats] = {}
    for r in records:
        if not metric.applies(r):
            continue
        st = out.setdefault(r["cluster"], {})
        for k, v in metric.stat(r).items():
            st[k] = st.get(k, 0.0) + v
    return out


def _sum(stats: list[Stats]) -> Stats:
    tot: Stats = {}
    for s in stats:
        for k, v in s.items():
            tot[k] = tot.get(k, 0.0) + v
    return tot


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = q * (len(xs) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def bootstrap_ci(records: list[dict[str, Any]], metric: MetricDef, *, b: int = 2000,
                 seed: int = 7) -> tuple[float, float] | None:
    cs = _cluster_stats(records, metric)
    keys = sorted(cs)
    if len(keys) < 2:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(b):
        v = metric.final(_sum([cs[keys[rng.randrange(len(keys))]] for _ in keys]))
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return (_pct(vals, 0.025), _pct(vals, 0.975))


def paired_bootstrap(cand: list[dict[str, Any]], base: list[dict[str, Any]], metric: MetricDef, *,
                     b: int = 10_000, seed: int = 11) -> dict[str, Any]:
    """Δ = metric(候选) − metric(基线)，按“越大越好”取向（lower 指标取反），只用两边共有的样本与簇。"""
    common = {r["sample_id"] for r in cand} & {r["sample_id"] for r in base}
    c = [r for r in cand if r["sample_id"] in common]
    bs = [r for r in base if r["sample_id"] in common]
    cc, bc = _cluster_stats(c, metric), _cluster_stats(bs, metric)
    keys = sorted(set(cc) & set(bc))
    sign = 1.0 if metric.direction == "higher" else -1.0
    vc = metric.final(_sum([cc[k] for k in keys]))
    vb = metric.final(_sum([bc[k] for k in keys]))
    if vc is None or vb is None or len(keys) < 2:
        return {"delta": None, "ci": None, "n_clusters": len(keys), "cand": vc, "base": vb}
    rng = random.Random(seed)
    deltas = []
    for _ in range(b):
        pick = [keys[rng.randrange(len(keys))] for _ in keys]
        x, y = metric.final(_sum([cc[k] for k in pick])), metric.final(_sum([bc[k] for k in pick]))
        if x is not None and y is not None:
            deltas.append(sign * (x - y))
    return {"delta": sign * (vc - vb), "ci": (_pct(deltas, 0.025), _pct(deltas, 0.975)), "n_clusters": len(keys),
            "cand": vc, "base": vb}

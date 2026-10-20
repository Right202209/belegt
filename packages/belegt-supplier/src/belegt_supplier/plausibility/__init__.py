# Spec: 08 §5（合理性校验 PLAUS-001…027、030…035）
"""合理性校验：`run_plausibility(values, ctx)` → `PlausReport(flag, basis, hits)`。"""
from __future__ import annotations

from .content import CONTENT_RULE_IDS, FileScan, plaus_035, scan_file, scan_text_fields, strip_controls
from .geo import BBoxGeoOracle
from .model import GeoOracle, PlausContext, PlausHit, PlausReport, PlausValue, RuleSpec
from .rules import DATA_RULES

RULES: dict[str, RuleSpec] = {r.rule_id: r for r in DATA_RULES}
CLIENT_SIDE_RULES = frozenset(r.rule_id for r in DATA_RULES if r.client_side)
ALL_RULE_IDS = [r.rule_id for r in DATA_RULES] + CONTENT_RULE_IDS


def run_plausibility(values: list[PlausValue], ctx: PlausContext | None = None, *,
                     only: frozenset[str] | None = None) -> PlausReport:
    ctx = ctx or PlausContext()
    hits: list[PlausHit] = []
    for rule in DATA_RULES:
        if only is not None and rule.rule_id not in only:
            continue
        hits.extend(rule.fn(values, ctx))
    return PlausReport(hits)


__all__ = [
    "ALL_RULE_IDS", "BBoxGeoOracle", "CLIENT_SIDE_RULES", "FileScan", "GeoOracle", "PlausContext", "PlausHit",
    "PlausReport", "PlausValue", "RULES", "RuleSpec", "plaus_035", "run_plausibility", "scan_file",
    "scan_text_fields", "strip_controls",
]

# Spec: 11（评测与质量）：黄金集、指标、评测运行器、CI 门禁 QG-01…QG-07；FR-020…FR-022
"""belegt-bench：Belegt 公共评测基准（代码 Apache-2.0，数据 CC BY 4.0）。

公开 API：
- 数据集：`load_dataset`、`verify_manifest`、`Dataset`、`DEFAULT_VERSION`；标注 schema：`MapSample`、`EvidSample`、
  `ConflictSample`、`InjSample`、`DraftSample`、`parse_sample`、`json_schemas`
- 构建与合成：`build`（确定性重建黄金集）、`question_variants`、`synthesize_questionnaire`、`render_csv`
- 一致性：`cohen_kappa`、`weighted_kappa`、`location_kappa`、`batch_kappa`、`iou`
- 指标：`METRICS`、`compute`、`score_mapping`、`score_evidence`、`score_conflicts`、`score_injection`、`score_draft`
- R-NUM：`find_new_numbers`
- 运行器：`run`、`resolve_predictor`、`smoke_subset`
- 门禁：`evaluate`、`load_thresholds`、`render_text`、`GateResult`
- 防泄漏：`check_leaks`
"""
from .dataset import DEFAULT_VERSION, Dataset, load_dataset, verify_manifest
from .gate import GateResult, evaluate, load_thresholds, render_text
from .kappa import batch_kappa, cohen_kappa, iou, location_kappa, weighted_kappa
from .leak import check_leaks
from .metrics import (
    METRICS, compute, score_conflicts, score_draft, score_evidence, score_injection, score_mapping,
)
from .rnum import find_new_numbers
from .runner import resolve_predictor, run, smoke_subset
from .schema import ConflictSample, DraftSample, EvidSample, InjSample, MapSample, json_schemas, parse_sample
from .synth import question_variants, render_csv, synthesize_questionnaire

__version__ = "0.1.0"


def build(out_dir, **kw):  # noqa: ANN001, ANN201
    from .build import build as _b
    return _b(out_dir, **kw)


__all__ = [
    "DEFAULT_VERSION", "Dataset", "load_dataset", "verify_manifest", "MapSample", "EvidSample", "ConflictSample",
    "InjSample", "DraftSample", "parse_sample", "json_schemas", "build", "question_variants",
    "synthesize_questionnaire", "render_csv", "cohen_kappa", "weighted_kappa", "location_kappa", "batch_kappa", "iou",
    "METRICS", "compute", "score_mapping", "score_evidence", "score_conflicts", "score_injection", "score_draft",
    "find_new_numbers", "run", "resolve_predictor", "smoke_subset", "evaluate", "load_thresholds", "render_text",
    "GateResult", "check_leaks",
]

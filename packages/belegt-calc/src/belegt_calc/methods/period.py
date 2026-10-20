# Spec: 05 §1.5（period-align@1.0.0）、§2.5；02 §3.4（期间对齐）
"""family ``period-align@1.0.0``。

``period.aggregate``：子期间（如月度证据）重组为计算期间：``Σ part_<id>``，所有分项量纲须一致；
  结果单位取 ``params.unit``（缺省为第一个分项单位的规范单位）。可选 ``params.expected_parts`` 校验分项数。
``period.time_proration``：按日历天折算 ``value × overlap_days / source_days``。只在客户方法论认可时使用：
  ``params.decision_ref``（CQ-METH-09 作答）必填，否则 ``CALC_PARAM_REQUIRES_DECISION``（02 §3.4、05 §2.5）。
  源期间 ``params.source_start``/``params.source_end``（闭区间）。
"""
from __future__ import annotations

import datetime as _dt

from .. import units as U
from ..dec import ZERO, Rat
from ..engine import Ctx, Item, MethodOutput
from ..errors import CALC_INPUT_MISSING, CALC_PERIOD_MISMATCH, CALC_UNIT_INCOMPATIBLE, CalcError
from ..registry import register
from ..types import MethodSpec


def _digits_for(unit: str) -> int:
    return 2 if unit == "EUR" else 3


@register(MethodSpec(
    method_id="period.aggregate", family="period-align@1.0.0",
    required_inputs=("part_<id>",), optional_inputs=(),
    required_params=(), optional_params=("unit", "expected_parts", "rounding_digits", "decision_ref"),
    default_digits=3, result_unit="(同分项)", result_datapoints=("任意数值 DataPoint",),
    description="子期间重组：Σ part_<id>（02 §3.4）",
))
def aggregate(ctx: Ctx) -> MethodOutput:
    parts = [n for n in ctx.input_names if n.startswith("part_")]
    if not parts:
        raise CalcError(CALC_INPUT_MISSING, "no part_<id> inputs")
    exp = ctx.params.get("expected_parts")
    if exp is not None and (not exp.isdigit() or int(exp) != len(parts)):
        raise CalcError(CALC_INPUT_MISSING, f"expected {exp} parts, got {len(parts)}")
    unit = ctx.params.get("unit") or U.canonical_unit(ctx.raw_unit(parts[0]))
    total = ZERO
    items = []
    for n in parts:
        if not U.compatible(ctx.raw_unit(n), unit):
            raise CalcError(CALC_UNIT_INCOMPATIBLE, f"{n} unit {ctx.raw_unit(n)!r} incompatible with {unit!r}")
        v = ctx.inp(n, unit)
        items.append(Item(n, v, unit))
        total = total + v
    items.append(Item("total_unrounded", total, unit))
    return MethodOutput(total, unit, items, default_digits=_digits_for(unit))


@register(MethodSpec(
    method_id="period.time_proration", family="period-align@1.0.0",
    required_inputs=("value",), optional_inputs=(),
    required_params=("source_start", "source_end", "decision_ref"), optional_params=("rounding_digits",),
    default_digits=3, result_unit="(同输入)", result_datapoints=("任意数值 DataPoint",),
    description="按日历天折算 value × overlap / source_days；需 CQ-METH-09 决策（02 §3.4）",
))
def time_proration(ctx: Ctx) -> MethodOutput:
    ctx.param("decision_ref", decision="CQ-METH-09")
    try:
        s = _dt.date.fromisoformat(ctx.param("source_start"))  # type: ignore[arg-type]
        e = _dt.date.fromisoformat(ctx.param("source_end"))  # type: ignore[arg-type]
    except ValueError as ex:
        raise CalcError(CALC_PERIOD_MISMATCH, "invalid source period") from ex
    if s > e:
        raise CalcError(CALC_PERIOD_MISMATCH, "source_start after source_end")
    ov_s = max(s, ctx.period.start_date)
    ov_e = min(e, ctx.period.end_date)
    if ov_s > ov_e:
        raise CalcError(CALC_PERIOD_MISMATCH, "source period does not overlap calculation period")
    src_days = (e - s).days + 1
    ov_days = (ov_e - ov_s).days + 1
    unit = ctx.raw_unit("value")
    v = ctx.inp("value", unit)
    res = v * ov_days / src_days
    items = [
        Item("source_days", Rat(src_days), "1"),
        Item("overlap_days", Rat(ov_days), "1"),
        Item("prorated_unrounded", res, unit),
    ]
    ctx.annotations["is_estimate"] = True
    return MethodOutput(res, unit, items, default_digits=_digits_for(unit))

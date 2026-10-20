# Spec: 05 §1（计算原则）、§1.6（calc-service 调用）、§8.1（API）；03 §3.6、§4.5、§4.7
"""计算引擎：``calculate``、``replay``。纯函数：不读时钟、网络、随机数或环境变量（05 §1.1）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import units as U
from ._version import ENGINE_VERSION
from .dec import Rat, breakdown_str, parse_dec, round_str, sig_digits, MAX_SIG_DIGITS, quantize
from .errors import (
    CALC_DEC_INVALID,
    CALC_FACTOR_SET_DEPRECATED,
    CALC_INPUT_MISSING,
    CALC_METHODOLOGY_UNSUPPORTED,
    CALC_PARAM_REQUIRES_DECISION,
    CALC_PRECISION_OVERFLOW,
    CalcError,
)
from .factors import select_row
from .hashing import canonical_inputs, input_hash as _input_hash, output_hash as _output_hash
from .registry import check_methodology, get_method
from .types import CalcResult, FactorRow, FactorSet, InputValue, Period, ReplayReport

BOUNDARY_RE = re.compile(r"^(organization|product_per_unit|site:[A-Za-z0-9_]+|product:[A-Za-z0-9_]+)$")
CUSTOMER_RE = re.compile(r"^cp_[0-9A-Za-z]+$")


@dataclass
class Item:
    """breakdown 项。``digits=None`` 时按 10 位小数量化（05 §1.2）。"""

    name: str
    value: Rat | str
    unit: str
    digits: int | None = None


@dataclass
class MethodOutput:
    value: Rat
    unit: str
    breakdown: list[Item] = field(default_factory=list)
    default_digits: int | None = None   # 覆盖 MethodSpec.default_digits（如按单位不同）


@dataclass
class _Input:
    name: str
    ref: str
    value: Rat
    raw: str
    unit: str


class Ctx:
    """方法执行上下文：读取输入、参数、因子（记录 factors_used）。"""

    def __init__(self, *, method_id: str, factor_set: FactorSet, inputs: list[dict], params: Mapping[str, str],
                 boundary: str, period: Period) -> None:
        self.method_id = method_id
        self.fs = factor_set
        self.params = dict(params)
        self.boundary = boundary
        self.period = period
        self._inputs: dict[str, _Input] = {}
        for i in inputs:
            if not U.is_known(i["unit"]):
                from .errors import CALC_UNIT_INCOMPATIBLE

                raise CalcError(CALC_UNIT_INCOMPATIBLE, f"input {i['name']!r}: unknown unit {i['unit']!r}")
            self._inputs[i["name"]] = _Input(i["name"], i["ref"], Rat.of(parse_dec(i["value"], what=i["name"])),
                                             i["value"], i["unit"])
        self._consumed: set[str] = set()
        self._factors: list[dict] = []
        self._factor_ids: set[str] = set()
        self.annotations: dict[str, Any] = {}
        self.notes: list[str] = []

    # ---------------- 输入 ----------------
    @property
    def input_names(self) -> list[str]:
        return sorted(self._inputs)

    def has(self, name: str) -> bool:
        return name in self._inputs

    def inp(self, name: str, unit: str, *, allow_negative: bool = False) -> Rat:
        """读取输入并精确换算到 ``unit``；缺失报 ``CALC_INPUT_MISSING``，量纲不符报 ``CALC_UNIT_INCOMPATIBLE``。"""
        i = self._inputs.get(name)
        if i is None:
            raise CalcError(CALC_INPUT_MISSING, f"{self.method_id}: required input {name!r} missing")
        self._consumed.add(name)
        v = U.convert_rat(i.value, i.unit, unit)
        if v < 0 and not allow_negative and self.params.get("delta") != "true":
            raise CalcError(CALC_DEC_INVALID, f"input {name!r} is negative; set params.delta='true' for correction deltas")
        return v

    def inp_opt(self, name: str, unit: str, default: Rat | None = None, **kw: Any) -> Rat | None:
        if name not in self._inputs:
            return default
        return self.inp(name, unit, **kw)

    def raw_unit(self, name: str) -> str:
        return self._inputs[name].unit

    def raw_value(self, name: str) -> str:
        self._consumed.add(name)
        return self._inputs[name].raw

    def consume(self, name: str) -> None:
        self._consumed.add(name)

    def unconsumed(self) -> list[str]:
        return [n for n in self._inputs if n not in self._consumed]

    # ---------------- 参数 ----------------
    def param(self, name: str, *, choices: Sequence[str] | None = None, decision: str | None = None,
              default: str | None = None, required: bool = True) -> str | None:
        v = self.params.get(name)
        if v is None:
            if default is not None:
                return default
            if not required:
                return None
            if decision:
                raise CalcError(CALC_PARAM_REQUIRES_DECISION, f"params.{name} is required ({decision})", decision=decision)
            raise CalcError(CALC_INPUT_MISSING, f"params.{name} is required")
        if choices is not None and v not in choices:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.{name}={v!r} not in {list(choices)}")
        return v

    # ---------------- 因子 ----------------
    def geography(self) -> str | None:
        return self.params.get("country") or self.params.get("grid_region")

    def factor_row(self, key: str, *, geography: str | None | bool = True, year: int | None | bool = True) -> FactorRow:
        geo = self.geography() if geography is True else (None if geography is False else geography)
        yr = self.period.year if year is True else (None if year is False else year)
        sel = select_row(self.fs, key, geography=geo, year=yr)
        r = sel.row
        if r.source_row_id not in self._factor_ids:
            self._factor_ids.add(r.source_row_id)
            self._factors.append(r.used())
        if sel.year_gap:
            gaps = self.annotations.setdefault("factor_year_gap", {})
            gaps[key] = sel.year_gap
        if r.gas_scope == "co2_only":
            self.annotations["gas_scope_co2_only"] = True
        if r.status != "final":
            self.annotations.setdefault("factor_status", {})[key] = r.status
        return r

    def factor(self, key: str, unit: str, **kw: Any) -> Rat:
        """选择因子行并换算到 ``unit``（量纲须一致）。"""
        r = self.factor_row(key, **kw)
        return U.convert_rat(Rat.of(r.value), r.unit, unit)

    def has_factor(self, key: str) -> bool:
        return any(r.factor_key == key for r in self.fs.rows)

    def factors_used(self) -> list[dict]:
        return list(self._factors)


def apply_ef(qty: Rat, qty_unit: str, ef: FactorRow) -> tuple[Rat, str]:
    """数量 × 因子：把数量换算到因子分母单位后相乘，返回 (值, 因子分子单位)。"""
    if "/" not in ef.unit:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"factor {ef.factor_key} unit {ef.unit!r} is not a rate")
    num, den = ef.unit.split("/", 1)
    q = U.convert_rat(qty, qty_unit, den)
    return q * Rat.of(ef.value), num


def _norm_customer_scope(cs: list[str] | str) -> list[str] | str:
    if cs == "all":
        return "all"
    if isinstance(cs, str):
        cs = [cs]
    cs = list(cs)
    if not cs or len(set(cs)) != len(cs) or not all(isinstance(c, str) and CUSTOMER_RE.match(c) for c in cs):
        raise CalcError(CALC_INPUT_MISSING, f"invalid customer_scope {cs!r}")
    return cs


def _digits(params: Mapping[str, str], default: int) -> int:
    rd = params.get("rounding_digits")
    if rd is None:
        return default
    if not re.fullmatch(r"(0|[1-9][0-9]?)", rd) or int(rd) > 12:
        raise CalcError(CALC_DEC_INVALID, f"params.rounding_digits={rd!r} must be an integer 0..12")
    return int(rd)


def calculate(
    method_id: str,
    methodology_version: str,
    factor_set: FactorSet,
    inputs: Iterable[InputValue | Mapping[str, Any]],
    params: Mapping[str, str] | None,
    boundary: str,
    period: Period | Mapping[str, str],
    customer_scope: list[str] | str = "all",
    *,
    allow_deprecated: bool = False,
) -> CalcResult:
    """执行一次确定性计算（05 §8.1）。

    ``allow_deprecated`` 只供 ``replay`` 使用：已弃用因子库不得被新计算引用（05 §7.3-3）。
    """
    method = get_method(method_id)
    check_methodology(method, methodology_version)
    if factor_set.deprecated and not allow_deprecated:
        raise CalcError(CALC_FACTOR_SET_DEPRECATED, f"{factor_set.id} is deprecated: {(factor_set.deprecation or {}).get('reason', '')}")
    if not isinstance(boundary, str) or not BOUNDARY_RE.match(boundary):
        raise CalcError(CALC_INPUT_MISSING, f"invalid boundary {boundary!r}")
    params = dict(params or {})
    for k, v in params.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise CalcError(CALC_DEC_INVALID, f"params[{k!r}] must be a string (05 §8.1)")
    per = Period.of(period)
    cs = _norm_customer_scope(customer_scope)
    canon = canonical_inputs(inputs)
    if not canon and method_id not in _ALLOW_NO_INPUTS:
        raise CalcError(CALC_INPUT_MISSING, f"{method_id}: at least one input is required (03 §3.6)")
    ih = _input_hash({
        "method_id": method_id,
        "methodology_version": methodology_version,
        "factor_set_version": factor_set.id,
        "factor_set_rows_hash": factor_set.rows_hash,
        "boundary": boundary,
        "period": per.to_dict(),
        "params": params,
        "inputs": canon,
    })
    ctx = Ctx(method_id=method_id, factor_set=factor_set, inputs=canon, params=params, boundary=boundary, period=per)
    out = method.fn(ctx)
    assert isinstance(out, MethodOutput)
    extra = ctx.unconsumed()
    if extra:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"{method_id}: inputs not used by this method: {extra}")
    default_digits = out.default_digits if out.default_digits is not None else method.spec.default_digits
    digits = _digits(params, default_digits)
    value_str = round_str(out.value, digits)
    if sig_digits(quantize(out.value, digits)) > MAX_SIG_DIGITS:
        raise CalcError(CALC_PRECISION_OVERFLOW, f"result {value_str} exceeds {MAX_SIG_DIGITS} significant digits")
    result = {"value": value_str, "unit": out.unit, "rounding": {"mode": "half_even", "digits": digits}}
    breakdown = []
    for it in out.breakdown:
        if isinstance(it.value, str):
            v = it.value
        elif it.digits is not None:
            v = round_str(it.value, it.digits)
        else:
            v = breakdown_str(it.value)
        breakdown.append({"name": it.name, "value": v, "unit": it.unit})
    oh = _output_hash(result, breakdown)
    ann = dict(ctx.annotations)
    if ctx.notes:
        ann["notes"] = list(ctx.notes)
    if factor_set.warnings:
        ann["factor_set_warnings"] = list(factor_set.warnings)
    return CalcResult(
        method_id=method_id,
        methodology_version=methodology_version,
        factor_set_version=factor_set.id,
        engine_version=ENGINE_VERSION,
        inputs=canon,
        factors_used=ctx.factors_used(),
        params=params,
        result=result,
        breakdown=breakdown,
        input_hash=ih,
        output_hash=oh,
        boundary=boundary,
        period=per.to_dict(),
        customer_scope=cs,
        status="computed",
        annotations=ann,
    )


_ALLOW_NO_INPUTS: frozenset[str] = frozenset()


def request_from_calculation(calc: Mapping[str, Any]) -> dict:
    return {
        "method_id": calc["method_id"],
        "methodology_version": calc["methodology_version"],
        "inputs": calc["inputs"],
        "params": calc.get("params") or {},
        "boundary": calc["boundary"],
        "period": calc["period"],
        "customer_scope": calc.get("customer_scope", "all"),
    }


def replay(calculation: Mapping[str, Any], factor_set: FactorSet) -> ReplayReport:
    """重放一个 Calculation：重算 input_hash 与 output_hash 并比较（03 §4.7；05 §1.4）。

    已弃用的因子库允许重放。``output_hash`` 不同而 ``input_hash`` 相同即"漂移"。
    """
    calc = dict(calculation)
    if calc.get("factor_set_version") not in (None, factor_set.id):
        return ReplayReport(
            calc_input_hash=calc.get("input_hash"), recomputed_input_hash="", expected_output_hash=calc.get("output_hash"),
            actual_output_hash=None, engine_version=ENGINE_VERSION, recorded_engine_version=calc.get("engine_version"),
            input_hash_match=False, output_hash_match=False,
            error={"code": "CALC_FACTOR_MISSING", "detail": f"calculation uses {calc.get('factor_set_version')}, got {factor_set.id}"},
        )
    req = request_from_calculation(calc)
    rih = _input_hash({**req, "factor_set_version": factor_set.id, "factor_set_rows_hash": factor_set.rows_hash})
    ih_match = calc.get("input_hash") == rih
    try:
        res = calculate(factor_set=factor_set, allow_deprecated=True, **req)
    except CalcError as e:
        return ReplayReport(
            calc_input_hash=calc.get("input_hash"), recomputed_input_hash=rih, expected_output_hash=calc.get("output_hash"),
            actual_output_hash=None, engine_version=ENGINE_VERSION, recorded_engine_version=calc.get("engine_version"),
            input_hash_match=ih_match, output_hash_match=False, error=e.to_dict(),
        )
    diffs: list[dict] = []
    exp_res, exp_bd = calc.get("result"), calc.get("breakdown") or []
    if exp_res is not None and exp_res != res.result:
        diffs.append({"path": "result", "expected": exp_res, "actual": res.result})
    if calc.get("breakdown") is not None and exp_bd != res.breakdown:
        diffs.append({"path": "breakdown", "expected": exp_bd, "actual": res.breakdown})
    expected_oh = calc.get("output_hash")
    if expected_oh is None and exp_res is not None:
        expected_oh = _output_hash(exp_res, exp_bd)
    return ReplayReport(
        calc_input_hash=calc.get("input_hash"),
        recomputed_input_hash=rih,
        expected_output_hash=expected_oh,
        actual_output_hash=res.output_hash,
        engine_version=ENGINE_VERSION,
        recorded_engine_version=calc.get("engine_version"),
        input_hash_match=ih_match,
        # 记录的 result/breakdown 也必须与重算一致（防止记录与其 output_hash 不自洽）
        output_hash_match=(expected_oh == res.output_hash and not diffs),
        diffs=diffs,
    )

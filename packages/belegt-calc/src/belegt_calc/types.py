# Spec: 05 §8.1（InputValue, Period, FactorSet, CalcResult）；03 §3.6（Calculation）、§3.13（FactorSet）
"""公开数据类型（纯标准库 dataclass）。"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import CALC_DEC_INVALID, CALC_PERIOD_MISMATCH, CalcError


@dataclass(frozen=True)
class InputValue:
    """计算输入：``InputValue(name, ref, value: str, unit)``，``ref`` 为钉住版本的 ObjectRef。"""

    name: str
    ref: str
    value: str
    unit: str

    def to_dict(self) -> dict:
        return {"name": self.name, "ref": self.ref, "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "InputValue":
        return cls(name=d["name"], ref=d["ref"], value=d["value"], unit=d["unit"])


@dataclass(frozen=True)
class Period:
    """闭区间期间（ISO 日期）。"""

    start: str
    end: str

    def __post_init__(self) -> None:
        try:
            s = _dt.date.fromisoformat(self.start)
            e = _dt.date.fromisoformat(self.end)
        except (TypeError, ValueError) as ex:
            raise CalcError(CALC_PERIOD_MISMATCH, f"invalid period {self.start!r}..{self.end!r}") from ex
        if s > e:
            raise CalcError(CALC_PERIOD_MISMATCH, f"period start {self.start} after end {self.end}")

    @property
    def start_date(self) -> _dt.date:
        return _dt.date.fromisoformat(self.start)

    @property
    def end_date(self) -> _dt.date:
        return _dt.date.fromisoformat(self.end)

    @property
    def year(self) -> int:
        """计算期间所属年份；跨年期间不能按年份选因子（05 §7.4-2）。"""
        if self.start_date.year != self.end_date.year:
            raise CalcError(CALC_PERIOD_MISMATCH, f"period {self.start}..{self.end} spans several years")
        return self.start_date.year

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}

    @classmethod
    def of(cls, p: "Period | Mapping[str, str]") -> "Period":
        if isinstance(p, Period):
            return p
        return cls(start=p["start"], end=p["end"])


@dataclass(frozen=True)
class FactorRow:
    """因子行 ``{factor_key, geography, year, value(DEC-STR), unit, source_row_id}`` + 可选 ``quality``、``upstream_source``。"""

    factor_key: str
    geography: str
    year: str
    value: str
    unit: str
    source_row_id: str
    quality: Mapping[str, str] | None = None
    extra: Mapping[str, Any] | None = None   # upstream_source 等其他行字段（参与 rows_hash）

    @property
    def status(self) -> str:
        return (self.quality or {}).get("status", "final")

    @property
    def gas_scope(self) -> str | None:
        return (self.quality or {}).get("gas_scope")

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "factor_key": self.factor_key,
            "geography": self.geography,
            "year": self.year,
            "value": self.value,
            "unit": self.unit,
            "source_row_id": self.source_row_id,
        }
        if self.quality:
            d["quality"] = dict(self.quality)
        if self.extra:
            d.update(self.extra)
        return d

    def used(self) -> dict:
        """写入 ``factors_used`` 的形态（03 §5.4）。"""
        return {"factor_key": self.factor_key, "value": self.value, "unit": self.unit, "source_row_id": self.source_row_id}


@dataclass
class FactorSet:
    """已冻结的因子库（05 §7.1）。``rows_hash`` 为声明值；加载时校验与行内容一致。"""

    id: str
    source: str
    license: str
    published_at: str
    valid_from: str
    rows: list[FactorRow]
    rows_hash: str
    status: str = "active"
    valid_until: str | None = None
    upstream: Mapping[str, Any] | None = None
    deprecation: Mapping[str, Any] | None = None
    row_count: int | None = None
    meta: dict = field(default_factory=dict)       # 其他元数据（注释、note 等）
    warnings: list[str] = field(default_factory=list)

    @property
    def deprecated(self) -> bool:
        return self.status == "deprecated"


@dataclass(frozen=True)
class MethodSpec:
    """``list_methods()`` 的元素（05 §8.1）。"""

    method_id: str
    family: str                       # 含版本，如 "ghg-scope1@1.0.0"
    required_inputs: tuple[str, ...]  # 名称或模式描述
    optional_inputs: tuple[str, ...]
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]
    default_digits: int
    result_unit: str
    result_datapoints: tuple[str, ...]
    description: str
    status: str = "available"         # available | reserved

    def to_dict(self) -> dict:
        return {
            "method_id": self.method_id,
            "family": self.family,
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "required_params": list(self.required_params),
            "optional_params": list(self.optional_params),
            "default_digits": self.default_digits,
            "result_unit": self.result_unit,
            "result_datapoints": list(self.result_datapoints),
            "description": self.description,
            "status": self.status,
        }


@dataclass
class CalcResult:
    """``calculate`` 的返回值：Calculation 主体字段（不含 id/version/信封，05 §8.1）。

    ``annotations`` 不属于 Calculation 字段（03 §5.4 ``additionalProperties: false``），
    只供 calc-service 定级参考：``is_estimate``、``gas_scope_co2_only``、``factor_year_gap``、法条依据等。
    """

    method_id: str
    methodology_version: str
    factor_set_version: str
    engine_version: str
    inputs: list[dict]
    factors_used: list[dict]
    params: dict
    result: dict
    breakdown: list[dict]
    input_hash: str
    output_hash: str
    boundary: str
    period: dict
    customer_scope: list[str] | str
    status: str = "computed"
    annotations: dict = field(default_factory=dict)

    @property
    def value(self) -> str:
        return self.result["value"]

    def breakdown_value(self, name: str) -> str | None:
        for b in self.breakdown:
            if b["name"] == name:
                return b["value"]
        return None

    def to_dict(self) -> dict:
        """Calculation 主体字段（可直接与信封合并后写入证据图）。"""
        return {
            "method_id": self.method_id,
            "methodology_version": self.methodology_version,
            "factor_set_version": self.factor_set_version,
            "engine_version": self.engine_version,
            "inputs": [dict(i) for i in self.inputs],
            "factors_used": [dict(f) for f in self.factors_used],
            "params": dict(self.params),
            "result": dict(self.result),
            "breakdown": [dict(b) for b in self.breakdown],
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "boundary": self.boundary,
            "period": dict(self.period),
            "customer_scope": self.customer_scope,
            "status": self.status,
        }


@dataclass
class ReplayReport:
    """``replay`` 的返回值（03 §4.7）。"""

    calc_input_hash: str | None
    recomputed_input_hash: str
    expected_output_hash: str | None
    actual_output_hash: str | None
    engine_version: str
    recorded_engine_version: str | None
    input_hash_match: bool
    output_hash_match: bool
    diffs: list[dict] = field(default_factory=list)
    error: dict | None = None

    @property
    def ok(self) -> bool:
        return self.input_hash_match and self.output_hash_match and self.error is None

    @property
    def drift(self) -> bool:
        """同一 input_hash 下结果不同 = 漂移（05 §1.4）。"""
        return self.input_hash_match and not self.output_hash_match

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "drift": self.drift,
            "input_hash_match": self.input_hash_match,
            "output_hash_match": self.output_hash_match,
            "calc_input_hash": self.calc_input_hash,
            "recomputed_input_hash": self.recomputed_input_hash,
            "expected_output_hash": self.expected_output_hash,
            "actual_output_hash": self.actual_output_hash,
            "engine_version": self.engine_version,
            "recorded_engine_version": self.recorded_engine_version,
            "diffs": self.diffs,
            "error": self.error,
        }

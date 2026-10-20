# Spec: 03 §3.1 DataPoint、§3.5 Evidence、§3.6 Calculation、§3.13 FactorSet
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import (
    Actor, Boundary, CustomerScope, DataPointRef, DateStr, DecStr, GraphObject, Locator, Model,
    ObjectRefStr, Period, Sha256, ValueType,
)


class DataPoint(Model):
    """03 §3.1 依赖的 DataPoint 字段（权威定义见 02）。图对象引用时写 ``dp:...@semver``。"""

    TYPE: ClassVar[str] = "DataPoint"
    id: str
    semver: str
    value_type: ValueType
    unit: str | None = None
    dimension: str | None = None
    allowed_boundaries: list[str] = Field(default_factory=lambda: ["organization"])
    requires_consolidation: bool = False
    status: Literal["draft", "active", "deprecated", "retired"] = "active"

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.semver}"


class Verification(Model):
    method: Literal["auto_rule", "closed_question", "human_review", "connector_attested"]
    cq_ref: str | None = None
    rule_id: str | None = None
    by: Actor


class Plausibility(Model):
    flag: Literal["ok", "outlier", "unit_suspect"]
    basis: str | None = None


class Evidence(GraphObject):
    TYPE: ClassVar[str] = "Evidence"
    PREFIX: ClassVar[str] = "ev_"

    source_type: Literal["document", "erp", "human_confirmation", "supplier_response", "historical_answer", "public_source"]
    datapoint_id: DataPointRef | None = None
    fact_key: str
    extracted_value: Any
    value_type: ValueType
    unit: str | None = None
    period: Period | None = None
    boundary: Boundary
    locator: Locator
    valid_from: DateStr
    valid_until: DateStr | None = None
    confidence: float = Field(ge=0, le=1)
    verification: Verification | None = None
    supplier_request_ref: str | None = None
    corrects: ObjectRefStr | None = None
    plausibility: Plausibility | None = None
    status: Literal["candidate", "verified", "expired", "superseded", "rejected"]


class CalcInput(Model):
    name: str
    ref: ObjectRefStr
    value: DecStr
    unit: str


class FactorUsed(Model):
    factor_key: str
    value: DecStr
    unit: str
    source_row_id: str


class Rounding(Model):
    mode: Literal["half_even", "half_up", "none"]
    digits: int = Field(ge=0, le=12)


class CalcResult(Model):
    value: DecStr
    unit: str
    rounding: Rounding


class BreakdownItem(Model):
    model_config = {"extra": "allow"}
    name: str
    value: DecStr
    unit: str


class Calculation(GraphObject):
    TYPE: ClassVar[str] = "Calculation"
    PREFIX: ClassVar[str] = "calc_"

    method_id: str
    methodology_version: str
    factor_set_version: str
    engine_version: str
    inputs: list[CalcInput] = Field(min_length=1)
    factors_used: list[FactorUsed]
    params: dict[str, Any] | None = None
    result: CalcResult
    breakdown: list[BreakdownItem] | None = None
    input_hash: Sha256
    output_hash: Sha256
    boundary: Boundary
    period: Period
    customer_scope: CustomerScope
    status: Literal["computed", "superseded"]


class FactorRow(Model):
    """``ref.factor`` 行（03 §3.13）：``{factor_key, geography, year, value(DEC-STR), unit, source_row_id}``。"""

    factor_key: str
    geography: str
    year: str
    value: DecStr
    unit: str
    source_row_id: str


class FactorSet(GraphObject):
    TYPE: ClassVar[str] = "FactorSet"
    PREFIX: ClassVar[str] = "fs:"

    source: str
    license: str
    published_at: DateStr
    valid_from: DateStr
    valid_until: DateStr | None = None
    row_count: int = Field(ge=0)
    rows_hash: Sha256
    status: Literal["active", "deprecated"]

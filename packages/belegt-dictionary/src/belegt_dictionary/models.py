# Spec: 02 §1.1 (DataPoint 字段表), §1.3 (JSON Schema), §2.5 (别名), §4.1–§4.2 (TemplateMapping)
"""Pydantic v2 models for dictionary objects. Field names are identical to 02."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ValueType = Literal["number", "text", "enum", "bool", "date", "file", "geo"]
PeriodType = Literal["calendar_year", "fiscal_year", "reporting_period", "point_in_time", "validity_range"]
BoundaryKind = Literal["organization", "site", "product", "product_per_unit"]
Consolidation = Literal["operational_control", "financial_control", "equity_share"]
Sensitivity = Literal["public", "customer_shared", "confidential", "restricted"]
SourceType = Literal["document", "erp", "human_confirmation", "supplier_response", "historical_answer", "public_source"]
Relation = Literal["component_of", "sum_of", "derived_from", "alternative_of", "see_also"]
Status = Literal["draft", "active", "deprecated", "retired"]

ID_PATTERN = r"^dp:(org|env|energy|prod|mat|supply|cert|gov|social)\.[a-z][a-z0-9_]{1,40}\.[a-z][a-z0-9_]{1,48}(\.[a-z][a-z0-9_]{0,40})?$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class I18nLabel(_Strict):
    de: str = Field(max_length=80)
    en: str = Field(max_length=80)
    zh: str = Field(max_length=80)


class I18nDefinition(_Strict):
    en: str = Field(min_length=20)
    de: str | None = None
    zh: str | None = None


class RecordField(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value_type: ValueType
    unit: str | None = None
    enum_ref: str | None = None
    required: bool


class MethodologyRef(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    ref: str
    clause: str | None = None
    note: str | None = None


class EvidenceRequirements(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    accepted_source_types: list[SourceType]
    min_sources: int = Field(ge=0)
    max_age_days: int | None = Field(default=None, ge=1)
    requires_calculation: bool
    human_confirmation_allowed: bool


class Plausibility(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    expr: str
    severity: Literal["warn", "block"]
    note: str | None = None


class ValidityRule(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    min: float | int | None = None
    max: float | int | None = None
    regex: str | None = None
    enum_ref: str | None = None
    expires_by: Literal["value", "evidence_valid_until", "period_end_plus_days"] | None = None
    expires_after_days: int | None = None
    plausibility: list[Plausibility] = Field(default_factory=list)


class RelatedDataPoint(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    id: str = Field(pattern=r"^dp:")
    relation: Relation


class Deprecation(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    since: str
    reason: str
    replaced_by: list[str]
    retire_after: str
    migration: str | None = None


class DataPoint(_Strict):
    """A dictionary DataPoint (02 §1.1)."""

    id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    introduced_in: str = Field(pattern=r"^v\d{4}\.[1-4]$")
    status: Status
    label: I18nLabel
    definition: I18nDefinition
    value_type: ValueType
    cardinality: Literal["one", "many"] = "one"
    record_fields: list[RecordField] | None = None
    enum_ref: str | None = Field(default=None, pattern=r"^enums/[a-z0-9_]+\.yaml$")
    unit: str | None = None
    allowed_units: list[str] | None = None
    period_type: PeriodType
    boundary: list[BoundaryKind] = Field(min_length=1)
    consolidation: list[Consolidation] | None = None
    methodology_refs: list[MethodologyRef] | None = None
    sensitivity: Sensitivity
    evidence_requirements: EvidenceRequirements
    validity_rule: ValidityRule | None = None
    related_datapoints: list[RelatedDataPoint] | None = None
    deprecation: Deprecation | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _conditional_required(self) -> "DataPoint":
        if self.value_type == "number" and not self.unit:
            raise ValueError("value_type=number requires unit")
        if self.value_type == "enum" and not self.enum_ref:
            raise ValueError("value_type=enum requires enum_ref")
        if self.cardinality == "many" and not self.record_fields:
            raise ValueError("cardinality=many requires record_fields")
        if self.status in ("deprecated", "retired") and self.deprecation is None:
            raise ValueError("status deprecated/retired requires deprecation")
        return self

    # convenience -----------------------------------------------------------
    @property
    def ref(self) -> str:
        """Versioned reference, e.g. ``dp:env.ghg.scope1_total@1.0.0``."""
        return f"{self.id}@{self.version}"

    @property
    def domain(self) -> str:
        return self.id[3:].split(".")[0]

    @property
    def qualifier(self) -> str | None:
        parts = self.id[3:].split(".")
        return parts[3] if len(parts) == 4 else None

    @property
    def parent_id(self) -> str | None:
        parts = self.id.split(".")
        return ".".join(parts[:3]) if len(parts) == 4 else None

    def related(self, relation: str) -> list[str]:
        return [r.id for r in (self.related_datapoints or []) if r.relation == relation]


class Locator(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    kind: Literal["paragraph", "question", "cell", "column", "json_path", "xml_path", "field"]
    ref: str
    legacy_ref: str | None = None
    column: str | None = None
    columns: list[str] | None = None
    row: str | None = None
    field: str | None = None
    per_metal: list[str] | None = None
    verify: bool = False


class SplitOutput(_Strict):
    datapoint_id: str
    selector: str


class Transform(BaseModel):
    """A deterministic transform step (02 §4.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["identity", "unit_convert", "enum_map", "aggregate", "split", "format", "pipeline"]
    from_unit: str | None = None
    to_unit: str | None = None
    round: int | None = Field(default=None, ge=0)
    map: dict[str, Any] | None = None
    default: Any = None
    op: Literal["sum", "max", "min", "concat"] | None = None
    inputs: list[str] | None = None
    outputs: list[SplitOutput] | None = None
    pattern: str | None = None
    steps: list["Transform"] | None = None

    @model_validator(mode="after")
    def _params(self) -> "Transform":
        need = {"unit_convert": ("from_unit", "to_unit"), "enum_map": ("map",), "aggregate": ("op", "inputs"),
                "split": ("outputs",), "format": ("pattern",), "pipeline": ("steps",)}.get(self.type, ())
        missing = [n for n in need if getattr(self, n) is None]
        if missing:
            raise ValueError(f"transform.type={self.type} requires {missing}")
        return self

    def walk(self):
        """Yield this step and all nested pipeline steps."""
        yield self
        for s in self.steps or []:
            yield from s.walk()

    def referenced_datapoints(self) -> list[str]:
        out: list[str] = []
        for s in self.walk():
            out += list(s.inputs or [])
            out += [o.datapoint_id for o in s.outputs or []]
        return out


class MappingEntry(_Strict):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    locator: Locator
    short_label: str = Field(min_length=1, max_length=60)
    datapoint_id: str | None = None
    direction: Literal["import", "export", "both"]
    unit_hint: str | None = None
    period_hint: PeriodType | None = None
    boundary_hint: BoundaryKind | None = None
    transform: Transform = Field(default_factory=lambda: Transform(type="identity"))
    condition: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _dp_required(self) -> "MappingEntry":
        if self.datapoint_id is None and self.transform.type not in ("split", "aggregate"):
            raise ValueError("datapoint_id required unless transform is split/aggregate")
        return self

    def datapoint_ids(self) -> list[str]:
        """All DataPoints this entry reads or writes (own id + transform refs), de-duplicated in order."""
        ids = ([self.datapoint_id] if self.datapoint_id else []) + self.transform.referenced_datapoints()
        return list(dict.fromkeys(ids))


class TemplateMapping(_Strict):
    """A TemplateMapping file ``tm:<template_id>@<template_version>`` (02 §4.2)."""

    template_mapping: str
    template_id: str
    template_version: str
    template_source: str
    dictionary_min_version: str
    license_note: str | None = None
    maintainers: list[str] | None = None
    status: Literal["active", "frozen"] | None = None
    notes: str | None = None
    entries: list[MappingEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _id(self) -> "TemplateMapping":
        expect = f"tm:{self.template_id}@{self.template_version}"
        if self.template_mapping != expect:
            raise ValueError(f"template_mapping must be {expect}")
        return self

    @property
    def id(self) -> str:
        return self.template_mapping

    def entry(self, entry_id: str) -> MappingEntry | None:
        return next((e for e in self.entries if e.entry_id == entry_id), None)


class Alias(_Strict):
    alias: str = Field(pattern=ID_PATTERN)
    target: str = Field(pattern=ID_PATTERN)
    kind: Literal["deprecated_id", "community_alias"]
    since: str
    note: str | None = None


class Unit(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    code: str
    ucum: str | None = None
    dimension: str
    iso4217: str | None = None
    calc_internal: bool = False
    note: str | None = None

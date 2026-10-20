# Spec: 05 §4.1、§4.4、§4.6（PACT v3.0.3 必填字段对应）；02 §4.3.3；01 FR-503 / AC-503-1
"""PACT Tech Spec v3.0.3 `ProductFootprint` 导出与校验。

校验分两层：
1. 结构：随包 `schemas/pact-productfootprint-3.0.3.schema.json`（由官方 PACT OpenAPI 3.0.3 `components/schemas` 派生）；
2. 规则（本包 `PACT-R-*`）：SHALL 字段、`exemptedEmissionsPercent ≤ 3`（PACT Methodology v3，05 §4.1）、
   `referencePeriodEnd` 开区间 = Belegt 闭区间 end + 1 天（05 §4.6）、DQI 自 2027 起必填、生物源一致性等。
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from ..hashing import canonical_hash
from ..models import ExportResult, Period
from ..numbers import to_dec_str
from .common import ExportValidationError, rfc3339, schema_errors

PACT_SCHEMA = "pact-productfootprint-3.0.3.schema.json"
PACT_SPEC_VERSION = "3.0.3"
BELEGT_NS = uuid.UUID("6f1c3d2e-9b7a-4c51-8e0f-2a4b6c8d0e1f")

DECLARED_UNIT_MAP = {
    "kg": "kilogram", "kilogram": "kilogram",
    "L": "liter", "liter": "liter",
    "m3": "cubic meter", "cubic meter": "cubic meter",
    "kWh": "kilowatt hour", "kilowatt hour": "kilowatt hour",
    "MJ": "megajoule", "megajoule": "megajoule",
    "tkm": "ton kilometer", "ton kilometer": "ton kilometer",
    "m2": "square meter", "square meter": "square meter",
    "1": "piece", "piece": "piece", "pcs": "piece", "Stück": "piece",
    "h": "hour", "hour": "hour",
    "megabit second": "megabit second",
}
MASS_OPTIONAL_UNITS = {"kilowatt hour", "megajoule", "ton kilometer", "hour", "megabit second"}


class DQI(BaseModel):
    technological: Decimal
    geographical: Decimal
    temporal: Decimal


class EmissionFactorSource(BaseModel):
    name: str
    version: str


class PcfInput(BaseModel):
    """一个产品 PCF 的导出输入（值来自 approved Answer / Calculation；本包不计算）。

    字段 ← Belegt 来源（05 §4.6）：
    pcf_excluding_biogenic_uptake ← dp:prod.pcf.cradle_to_gate.excluding_biogenic；
    pcf_including_biogenic_uptake ← dp:prod.pcf.cradle_to_gate；declared_unit ← dp:prod.pcf.declared_unit；
    product_mass_per_declared_unit ← dp:prod.mass.net_per_unit；reference_period ← dp:prod.pcf.reference_period；
    primary_data_share ← dp:prod.pcf.primary_data_share；fossil_ghg_emissions / biogenic_* / exempted_emissions_percent
    ← Calculation.breakdown；ipcc_characterization_factors ← params.gwp_set。
    """

    id: uuid.UUID | None = None
    spec_version: str = PACT_SPEC_VERSION
    created: datetime
    status: Literal["Active", "Deprecated"] = "Active"
    status_comment: str | None = None
    validity_period: Period | None = None
    preceding_pf_ids: list[uuid.UUID] = Field(default_factory=list)
    company_name: str
    company_ids: list[str]
    product_description: str
    product_ids: list[str]
    product_classifications: list[str] = Field(default_factory=list)
    product_name_company: str
    comment: str | None = None
    # CarbonFootprint
    declared_unit: str
    declared_unit_amount: Decimal = Decimal(1)
    product_mass_per_declared_unit: Decimal
    reference_period: Period
    geography_country: str | None = None
    geography_country_subdivision: str | None = None
    geography_region_or_subregion: str | None = None
    boundary_processes_description: str | None = None
    pcf_excluding_biogenic_uptake: Decimal
    pcf_including_biogenic_uptake: Decimal
    fossil_ghg_emissions: Decimal
    fossil_carbon_content: Decimal
    biogenic_carbon_content: Decimal | None = None
    recycled_carbon_content: Decimal | None = None
    biogenic_co2_uptake: Decimal | None = None
    biogenic_non_co2_emissions: Decimal | None = None
    land_use_change_ghg_emissions: Decimal | None = None
    aircraft_ghg_emissions: Decimal | None = None
    packaging_emissions_included: bool
    packaging_ghg_emissions: Decimal | None = None
    outbound_logistics_ghg_emissions: Decimal | None = None
    ccs_technological_co2_capture_included: bool = False
    ipcc_characterization_factors: list[str] = Field(default_factory=lambda: ["AR6"])
    cross_sectoral_standards: list[str] = Field(default_factory=lambda: ["ISO14067"])
    product_or_sector_specific_rules: list[dict[str, Any]] = Field(default_factory=list)
    exempted_emissions_percent: Decimal = Decimal(0)
    exempted_emissions_description: str | None = None
    allocation_rules_description: str | None = None
    secondary_emission_factor_sources: list[EmissionFactorSource] = Field(default_factory=list)
    primary_data_share: Decimal | None = None
    dqi: DQI | None = None
    # 溯源（进入 export_input_hash，不进导出文档）
    source_refs: dict[str, str] = Field(default_factory=dict)

    def resolved_id(self) -> uuid.UUID:
        if self.id:
            return self.id
        seed = "|".join([*self.company_ids, *self.product_ids, self.reference_period.start.isoformat(),
                         self.reference_period.end.isoformat(), self.created.isoformat()])
        return uuid.uuid5(BELEGT_NS, seed)


DP_TO_FIELD = {
    "dp:prod.pcf.cradle_to_gate.excluding_biogenic": "pcf_excluding_biogenic_uptake",
    "dp:prod.pcf.cradle_to_gate": "pcf_including_biogenic_uptake",
    "dp:prod.pcf.declared_unit": "declared_unit",
    "dp:prod.mass.net_per_unit": "product_mass_per_declared_unit",
    "dp:prod.pcf.reference_period": "reference_period",
    "dp:prod.pcf.primary_data_share": "primary_data_share",
}


def pcf_input_from_datapoints(values: Mapping[str, Any], **meta: Any) -> PcfInput:
    """按 05 §4.6 把 `{datapoint_id(@semver 可选): value}` 映射为 `PcfInput`；其余字段由 `meta` 给出。"""
    data: dict[str, Any] = dict(meta)
    for k, v in values.items():
        base = k.split("@", 1)[0]
        f = DP_TO_FIELD.get(base)
        if f:
            data[f] = v
    return PcfInput(**data)


def _d(v: Decimal | None) -> str | None:
    return None if v is None else to_dec_str(v)


def build_pact(inp: PcfInput) -> dict[str, Any]:
    du = DECLARED_UNIT_MAP.get(inp.declared_unit, inp.declared_unit)
    pcf: dict[str, Any] = {
        "declaredUnitOfMeasurement": du,
        "declaredUnitAmount": _d(inp.declared_unit_amount),
        "productMassPerDeclaredUnit": _d(inp.product_mass_per_declared_unit),
        "referencePeriodStart": rfc3339(inp.reference_period.start),
        # PACT End 为开区间：Belegt 闭区间 end + 1 天（05 §4.6）
        "referencePeriodEnd": rfc3339(inp.reference_period.end + timedelta(days=1)),
    }
    if inp.geography_country_subdivision:
        pcf["geographyCountrySubdivision"] = inp.geography_country_subdivision
    elif inp.geography_country:
        pcf["geographyCountry"] = inp.geography_country
    elif inp.geography_region_or_subregion:
        pcf["geographyRegionOrSubregion"] = inp.geography_region_or_subregion
    optional = {
        "boundaryProcessesDescription": inp.boundary_processes_description,
    }
    pcf.update({k: v for k, v in optional.items() if v})
    pcf.update({
        "pcfExcludingBiogenicUptake": _d(inp.pcf_excluding_biogenic_uptake),
        "pcfIncludingBiogenicUptake": _d(inp.pcf_including_biogenic_uptake),
        "fossilCarbonContent": _d(inp.fossil_carbon_content),
    })
    for key, val in (
        ("biogenicCarbonContent", inp.biogenic_carbon_content),
        ("recycledCarbonContent", inp.recycled_carbon_content),
    ):
        if val is not None:
            pcf[key] = _d(val)
    pcf["fossilGhgEmissions"] = _d(inp.fossil_ghg_emissions)
    for key, val in (
        ("landUseChangeGhgEmissions", inp.land_use_change_ghg_emissions),
        ("biogenicCO2Uptake", inp.biogenic_co2_uptake),
        ("biogenicNonCO2Emissions", inp.biogenic_non_co2_emissions),
        ("aircraftGhgEmissions", inp.aircraft_ghg_emissions),
    ):
        if val is not None:
            pcf[key] = _d(val)
    pcf["packagingEmissionsIncluded"] = inp.packaging_emissions_included
    if inp.packaging_ghg_emissions is not None:
        pcf["packagingGhgEmissions"] = _d(inp.packaging_ghg_emissions)
    if inp.outbound_logistics_ghg_emissions is not None:
        pcf["outboundLogisticsGhgEmissions"] = _d(inp.outbound_logistics_ghg_emissions)
    pcf["ccsTechnologicalCO2CaptureIncluded"] = inp.ccs_technological_co2_capture_included
    pcf["ipccCharacterizationFactors"] = list(inp.ipcc_characterization_factors)
    pcf["crossSectoralStandards"] = list(inp.cross_sectoral_standards)
    if inp.product_or_sector_specific_rules:
        pcf["productOrSectorSpecificRules"] = inp.product_or_sector_specific_rules
    pcf["exemptedEmissionsPercent"] = _d(inp.exempted_emissions_percent)
    if inp.exempted_emissions_description is not None:
        pcf["exemptedEmissionsDescription"] = inp.exempted_emissions_description
    if inp.allocation_rules_description:
        pcf["allocationRulesDescription"] = inp.allocation_rules_description
    if inp.secondary_emission_factor_sources:
        pcf["secondaryEmissionFactorSources"] = [s.model_dump() for s in inp.secondary_emission_factor_sources]
    if inp.primary_data_share is not None:
        pcf["primaryDataShare"] = _d(inp.primary_data_share)
    if inp.dqi:
        pcf["dqi"] = {"technologicalDQR": _d(inp.dqi.technological), "geographicalDQR": _d(inp.dqi.geographical),
                      "temporalDQR": _d(inp.dqi.temporal)}
    doc: dict[str, Any] = {
        "id": str(inp.resolved_id()),
        "specVersion": inp.spec_version,
    }
    if inp.preceding_pf_ids:
        doc["precedingPfIds"] = [str(x) for x in inp.preceding_pf_ids]
    doc["created"] = rfc3339(inp.created)
    doc["status"] = inp.status
    if inp.validity_period:
        doc["validityPeriodStart"] = rfc3339(inp.validity_period.start)
        doc["validityPeriodEnd"] = rfc3339(inp.validity_period.end + timedelta(days=1))
    doc.update({
        "companyName": inp.company_name,
        "companyIds": list(inp.company_ids),
        "productDescription": inp.product_description,
        "productIds": list(inp.product_ids),
    })
    if inp.product_classifications:
        doc["productClassifications"] = list(inp.product_classifications)
    doc["productNameCompany"] = inp.product_name_company
    if inp.comment:
        doc["comment"] = inp.comment
    doc["pcf"] = pcf
    return doc


_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")


def _decimal_fields() -> set[str]:
    from .common import load_schema

    props = load_schema(PACT_SCHEMA)["$defs"]["CarbonFootprint"]["properties"]
    return {k for k, v in props.items() if str(v.get("$ref", "")).endswith("Decimal")}


def validate_pact(doc: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 非空即不通过（AC-503-1）。"""
    errors = schema_errors(doc, PACT_SCHEMA)
    warnings: list[str] = []
    pcf = doc.get("pcf") or {}
    if not _UUID.match(str(doc.get("id", ""))):
        errors.append("$.id: not a UUID (PACT-R-ID)")
    for path, v in [("$.created", doc.get("created")), ("$.pcf.referencePeriodStart", pcf.get("referencePeriodStart")),
                    ("$.pcf.referencePeriodEnd", pcf.get("referencePeriodEnd")),
                    ("$.validityPeriodStart", doc.get("validityPeriodStart")),
                    ("$.validityPeriodEnd", doc.get("validityPeriodEnd"))]:
        if v is not None and not _RFC3339.match(str(v)):
            errors.append(f"{path}: not RFC 3339 date-time (PACT-R-DATETIME)")
    for k, v in pcf.items():
        if k in _decimal_fields() and isinstance(v, str) and not _DEC.match(v):
            errors.append(f"$.pcf.{k}: not a normalized decimal string (PACT-R-DEC)")
    # SHALL 字段（Tech Spec 3.0.3 x-rule=SHALL）
    for k in ("pcfExcludingBiogenicUptake", "pcfIncludingBiogenicUptake", "fossilCarbonContent", "fossilGhgEmissions",
              "packagingEmissionsIncluded", "ccsTechnologicalCO2CaptureIncluded", "primaryDataShare"):
        if k not in pcf:
            errors.append(f"$.pcf.{k}: required by reporting rule SHALL (PACT-R-SHALL)")
    try:
        ex = Decimal(str(pcf.get("exemptedEmissionsPercent", "0")))
        if ex > 3:
            errors.append("$.pcf.exemptedEmissionsPercent: > 3 % not allowed by PACT Methodology v3 (PACT-R-EXEMPT)")
        if ex < 0:
            errors.append("$.pcf.exemptedEmissionsPercent: negative (PACT-R-EXEMPT)")
    except Exception:
        pass
    try:
        pds = pcf.get("primaryDataShare")
        if pds is not None and not (Decimal(pds) >= 0 and Decimal(pds) <= 100):
            errors.append("$.pcf.primaryDataShare: must be within 0–100 (PACT-R-PDS)")
    except Exception:
        pass
    if "dqi" not in pcf:
        created = str(doc.get("created", ""))
        if created[:4].isdigit() and int(created[:4]) >= 2027:
            errors.append("$.pcf.dqi: required from 2027 (SHALL-2027, PACT-R-DQI)")
        else:
            warnings.append("$.pcf.dqi: missing; becomes mandatory in 2027 (SHALL-2027)")
    else:
        for k, v in pcf["dqi"].items():
            try:
                if not (Decimal(v) >= 1 and Decimal(v) <= 5):
                    errors.append(f"$.pcf.dqi.{k}: must be within 1–5 (PACT-R-DQI)")
            except Exception:
                pass
    du = pcf.get("declaredUnitOfMeasurement")
    try:
        mass = Decimal(str(pcf.get("productMassPerDeclaredUnit", "0")))
        if du not in MASS_OPTIONAL_UNITS and mass <= 0:
            errors.append("$.pcf.productMassPerDeclaredUnit: must be > 0 for mass-relevant declared units (PACT-R-MASS)")
    except Exception:
        pass
    try:
        if "biogenicCO2Uptake" in pcf:
            excl, incl = Decimal(pcf["pcfExcludingBiogenicUptake"]), Decimal(pcf["pcfIncludingBiogenicUptake"])
            if incl != excl + Decimal(pcf["biogenicCO2Uptake"]):
                warnings.append("pcfIncludingBiogenicUptake != pcfExcludingBiogenicUptake + biogenicCO2Uptake (05 §4.4)")
        if "biogenicCarbonContent" not in pcf:
            warnings.append("$.pcf.biogenicCarbonContent: not reported (rule BIO; 05 §4.4 requires explicit evidence even if 0)")
    except Exception:
        pass
    try:
        rs = datetime.fromisoformat(str(pcf["referencePeriodStart"]).replace("Z", "+00:00"))
        re_ = datetime.fromisoformat(str(pcf["referencePeriodEnd"]).replace("Z", "+00:00"))
        if re_ <= rs:
            errors.append("$.pcf.referencePeriodEnd: must be after referencePeriodStart (exclusive end)")
        if doc.get("validityPeriodEnd"):
            ve = datetime.fromisoformat(str(doc["validityPeriodEnd"]).replace("Z", "+00:00"))
            if ve > re_.replace(year=re_.year + 3) + timedelta(days=1):
                warnings.append("validity period exceeds 3 years after reference period end (PACT Methodology, 05 §4.1)")
    except Exception:
        pass
    return errors, warnings


def export_pact(inp: PcfInput | Mapping[str, Any], *, validate: bool = True) -> ExportResult:
    """导出 PACT v3.0.3 `ProductFootprint`。校验失败抛 `ExportValidationError`。"""
    if not isinstance(inp, PcfInput):
        inp = PcfInput(**inp)
    doc = build_pact(inp)
    warnings: list[str] = []
    if validate:
        errors, warnings = validate_pact(doc)
        if errors:
            raise ExportValidationError("pact", errors)
    return ExportResult(format=f"pact@{PACT_SPEC_VERSION}", document=doc,
                        export_input_hash=canonical_hash({"format": "pact", "doc": doc, "refs": inp.source_refs}),
                        warnings=warnings)

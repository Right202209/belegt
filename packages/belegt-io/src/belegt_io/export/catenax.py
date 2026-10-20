# Spec: 02 §4.3.3（Catena-X 复用 PACT 数据模型）；01 FR-503；_baseline L（Catena-X PCF Rulebook v4.0）
"""Catena-X PCF 兼容 JSON：语义模型 `urn:samm:io.catenax.pcf:10.0.0`（Tractus-X，status=release）。

- 结构由随包 `schemas/catenax-pcf-10.0.0.schema.json`（官方生成的 draft-04 JSON Schema）校验；
- 数值字段为 JSON number（模型要求），本包以 `Decimal` 精确输出（`dumps_json`），不经 float；
- 输入复用 `PcfInput`，另加 Catena-X 专有字段（`CatenaxExtras`）。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from ..hashing import canonical_hash
from ..models import ExportResult
from .common import ExportValidationError, loads_json, dumps_json, rfc3339, schema_errors
from .pact import DECLARED_UNIT_MAP, PcfInput

CATENAX_SCHEMA = "catenax-pcf-10.0.0.schema.json"
CATENAX_MODEL = "urn:samm:io.catenax.pcf:10.0.0"

STANDARD_MAP = {
    "ISO14067": "ISO 14067", "ISO14040-44": "ISO 14040-44", "GHGP-Product": "GHG Protocol Product", "PEF": "PEF",
    "PACT-1.0": "PACT-1.0", "PACT-2.0": "PACT-2.0", "PACT-3.0": "PACT-3.0", "PAS2050": "PAS 2050",
}
# UN M49 子区域（常用国家；其余须显式给出 geography_region_or_subregion）
COUNTRY_SUBREGION = {
    "DE": "Western Europe", "AT": "Western Europe", "CH": "Western Europe", "FR": "Western Europe",
    "NL": "Western Europe", "BE": "Western Europe", "LU": "Western Europe", "PL": "Eastern Europe",
    "CZ": "Eastern Europe", "SK": "Eastern Europe", "HU": "Eastern Europe", "RO": "Eastern Europe",
    "BG": "Eastern Europe", "IT": "Southern Europe", "ES": "Southern Europe", "PT": "Southern Europe",
    "SI": "Southern Europe", "HR": "Southern Europe", "GR": "Southern Europe", "SE": "Northern Europe",
    "DK": "Northern Europe", "FI": "Northern Europe", "IE": "Northern Europe", "GB": "Northern Europe",
    "CN": "Eastern Asia", "JP": "Eastern Asia", "KR": "Eastern Asia", "TW": "Eastern Asia", "IN": "Southern Asia",
    "TR": "Western Asia", "US": "Northern America", "CA": "Northern America", "MX": "Latin America and the Caribbean",
    "BR": "Latin America and the Caribbean", "VN": "South-eastern Asia", "TH": "South-eastern Asia",
    "MY": "South-eastern Asia", "ID": "South-eastern Asia", "ZA": "Sub-Saharan Africa", "EG": "Northern Africa",
    "MA": "Northern Africa", "UA": "Eastern Europe", "RS": "Southern Europe",
}


class CatenaxExtras(BaseModel):
    version: int = 1
    partial_full_pcf: Literal["Cradle-to-gate", "Cradle-to-grave"] = "Cradle-to-gate"
    retro_or_prospective_pcf_type: Literal[
        "Retrospective PCF", "Prospective PCF without forerunner",
        "Prospective PCF of further developed product with forerunner",
        "Prospective PCF for current product for future production date", "Progressive PCF",
    ] = "Retrospective PCF"
    allocation_waste_incineration: Literal["cut-off", "reverse cut-off", "system expansion",
                                           "polluter pays principle"] = "cut-off"
    mass_balancing_used: bool = False
    distribution_stage_included: bool = False
    pcf_legal_statement: str | None = None
    product_or_sector_specific_rules: list[str] = Field(default_factory=list)


def _n(v: Decimal | None) -> Decimal | None:
    return v


def build_catenax(inp: PcfInput, ex: CatenaxExtras) -> dict[str, Any]:
    region = inp.geography_region_or_subregion or COUNTRY_SUBREGION.get((inp.geography_country or "")[:2])
    geography: dict[str, Any] = {"geographyRegionOrSubregion": region}
    if inp.geography_country:
        geography["geographyCountry"] = inp.geography_country
    if inp.geography_country_subdivision:
        geography["geographyCountrySubdivision"] = inp.geography_country_subdivision
    time: dict[str, Any] = {
        "referencePeriodStart": rfc3339(inp.reference_period.start),
        "referencePeriodEnd": rfc3339(inp.reference_period.end + timedelta(days=1)),
        "created": rfc3339(inp.created),
    }
    if inp.validity_period:
        time["validityPeriodStart"] = rfc3339(inp.validity_period.start)
        time["validityPeriodEnd"] = rfc3339(inp.validity_period.end + timedelta(days=1))
    id_and_version: dict[str, Any] = {"id": str(inp.resolved_id())}
    if inp.preceding_pf_ids:
        id_and_version["precedingPfIds"] = [str(x) for x in inp.preceding_pf_ids]
    id_and_version.update({"version": ex.version, "status": inp.status,
                           "retroOrProspectivePcfType": ex.retro_or_prospective_pcf_type})
    boundary: dict[str, Any] = {"exemptedEmissionsPercent": inp.exempted_emissions_percent}
    if inp.exempted_emissions_description is not None:
        boundary["exemptedEmissionsDescription"] = inp.exempted_emissions_description
    technology: dict[str, Any] = {"ccsTechnologicalCO2CaptureIncluded": inp.ccs_technological_co2_capture_included}
    if inp.boundary_processes_description:
        technology["boundaryProcessesDescription"] = inp.boundary_processes_description
    rules = ex.product_or_sector_specific_rules or [
        n for r in inp.product_or_sector_specific_rules for n in r.get("ruleNames", [])
    ]
    standards = [{"crossSectoralStandards": STANDARD_MAP.get(s, s), "productOrSectorSpecificRules": rules}
                 for s in inp.cross_sectoral_standards]
    alloc: dict[str, Any] = {"allocationWasteIncineration": ex.allocation_waste_incineration}
    if inp.allocation_rules_description:
        alloc["allocationRulesDescription"] = inp.allocation_rules_description
    dsq: dict[str, Any] = {}
    if inp.primary_data_share is not None:
        dsq["primaryDataShare"] = inp.primary_data_share
    if inp.secondary_emission_factor_sources:
        dsq["secondaryEmissionFactorSources"] = [f"{s.name} {s.version}" for s in inp.secondary_emission_factor_sources]
    if inp.dqi:
        dsq.update({"technologicalDQR": inp.dqi.technological, "temporalDQR": inp.dqi.temporal,
                    "geographicalDQR": inp.dqi.geographical})
    production: dict[str, Any] = {
        "pcfIncludingBiogenicUptake": inp.pcf_including_biogenic_uptake,
        "pcfExcludingBiogenicUptake": inp.pcf_excluding_biogenic_uptake,
        "fossilGhgEmissions": inp.fossil_ghg_emissions,
    }
    for k, v in (("biogenicNonCO2Emissions", inp.biogenic_non_co2_emissions),
                 ("biogenicCO2Uptake", inp.biogenic_co2_uptake),
                 ("landUseChangeGhgEmissions", inp.land_use_change_ghg_emissions),
                 ("aircraftGhgEmissions", inp.aircraft_ghg_emissions)):
        if v is not None:
            production[k] = v
    packaging: dict[str, Any] = {"packagingEmissionsIncluded": inp.packaging_emissions_included}
    if inp.packaging_ghg_emissions is not None:
        packaging["packagingFossilGhgEmissions"] = inp.packaging_ghg_emissions
    carbon: dict[str, Any] = {"fossilCarbonContent": inp.fossil_carbon_content}
    if inp.biogenic_carbon_content is not None:
        carbon["biogenicCarbonContent"] = inp.biogenic_carbon_content
        carbon["carbonContentTotal"] = inp.fossil_carbon_content + inp.biogenic_carbon_content
    if inp.recycled_carbon_content is not None:
        carbon["recycledCarbonContent"] = inp.recycled_carbon_content
    general: dict[str, Any] = {}
    if inp.comment:
        general["comment"] = inp.comment
    if ex.pcf_legal_statement:
        general["pcfLegalStatement"] = ex.pcf_legal_statement
    product_info: dict[str, Any] = {
        "productNameCompany": inp.product_name_company,
        "productIds": list(inp.product_ids),
        "productDescription": inp.product_description,
    }
    if inp.product_classifications:
        product_info["productClassifications"] = list(inp.product_classifications)
    product_info.update({
        "declaredUnitOfMeasurement": DECLARED_UNIT_MAP.get(inp.declared_unit, inp.declared_unit),
        "declaredUnitAmount": inp.declared_unit_amount,
        "productMassPerDeclaredUnit": inp.product_mass_per_declared_unit,
    })
    return {
        "scopeOfPcfForm": [{"specVersion": CATENAX_MODEL, "partialFullPcf": ex.partial_full_pcf}],
        "companyAndProductInformation": [{
            "companyInformation": [{"companyName": inp.company_name, "companyIds": list(inp.company_ids)}],
            "productInformation": [product_info],
        }],
        "pcfAssessmentAndMethodology": [{
            "pcfAssessmentInformation": [{
                "idAndVersion": [id_and_version],
                "boundarySpecifications": [boundary],
                "technology": [technology],
                "geography": [geography],
                "time": [time],
            }],
            "pcfMethodology": [{
                "standards": standards,
                "gwpCharacterizationFactorDetails": [
                    {"ipccCharacterizationFactors": f} for f in inp.ipcc_characterization_factors
                ],
                "allocationInForeground": [alloc],
                "massBalancingInformation": [{"massBalancingUsed": ex.mass_balancing_used}],
            }],
            "dataSourcesAndQuality": [dsq],
        }],
        "general": [general],
        "productLifeCycleStagesAndEmissions": [{
            "productionStage": [production],
            "packagingStage": [packaging],
            "distributionStage": [{"distributionStageIncluded": ex.distribution_stage_included}],
        }],
        "carbonContent": [carbon],
    }


def validate_catenax(doc: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    parsed = loads_json(dumps_json(doc))  # 以 Decimal 回读，等价于接收方解析
    errors = schema_errors(parsed, CATENAX_SCHEMA, draft="4")
    warnings: list[str] = []
    try:
        geo = doc["pcfAssessmentAndMethodology"][0]["pcfAssessmentInformation"][0]["geography"][0]
        if not geo.get("geographyRegionOrSubregion"):
            errors.append("geographyRegionOrSubregion: required (give geography_region_or_subregion or a known country)")
        t = doc["pcfAssessmentAndMethodology"][0]["pcfAssessmentInformation"][0]["time"][0]
        if "validityPeriodEnd" not in t:
            errors.append("time.validityPeriodEnd: required by io.catenax.pcf:10.0.0 (give validity_period)")
        ex = doc["pcfAssessmentAndMethodology"][0]["pcfAssessmentInformation"][0]["boundarySpecifications"][0]
        if Decimal(str(ex["exemptedEmissionsPercent"])) > 3:
            warnings.append("exemptedEmissionsPercent > 3 %: allowed by Catena-X model (≤10) but not by PACT v3")
    except (KeyError, IndexError, TypeError):
        pass
    return errors, warnings


def export_catenax_pcf(inp: PcfInput | Mapping[str, Any], extras: CatenaxExtras | Mapping[str, Any] | None = None, *,
                       validate: bool = True) -> ExportResult:
    if not isinstance(inp, PcfInput):
        inp = PcfInput(**inp)
    ex = extras if isinstance(extras, CatenaxExtras) else CatenaxExtras(**(extras or {}))
    doc = build_catenax(inp, ex)
    warnings: list[str] = []
    if validate:
        errors, warnings = validate_catenax(doc)
        if errors:
            raise ExportValidationError("catenax_pcf", errors)
    return ExportResult(format="catenax_pcf@io.catenax.pcf:10.0.0", document=doc,
                        export_input_hash=canonical_hash({"format": "catenax", "doc": doc, "refs": inp.source_refs}),
                        warnings=warnings)

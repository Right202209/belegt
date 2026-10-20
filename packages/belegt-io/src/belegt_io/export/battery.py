# Spec: 02 §4.3.8；Reg. (EU) 2023/1542 Art. 7(1)(a)–(g)、Annex II §4（生命周期阶段）、Annex XIII 1(b)(c)；01 AC-503-2
"""电池护照碳足迹字段 JSON 片段。

两种模式：
- `mode="upstream"`（默认）：供应链内部交换用的碳足迹数据（Art. 7(1)(d)(e) 的数值与阶段拆分），
  明确标注 `declarationStatus="not_a_declaration"`——便于二级供应商先行收集（02 §4.3.8）。
- `mode="passport"`：面向护照的导出。按 AC-503-2 **不含碳足迹声明字段**（委员会授权/实施法案未定），
  只输出化学体系等已适用字段，并在 `omitted_fields` 中注明原因供界面展示。
字段名参考 Battery Pass / DIN DKE SPEC 99100 [需核对]。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from ..hashing import canonical_hash
from ..models import ExportResult
from .common import ExportValidationError, loads_json, dumps_json, rfc3339, schema_errors

BATTERY_SCHEMA = "battery-passport-cf-fragment.schema.json"
OMIT_REASON = ("Carbon footprint declaration (Reg. (EU) 2023/1542 Art. 7) not exported: the delegated/implementing "
               "acts defining calculation method and declaration format are not yet adopted (01 AC-503-2).")
CHEMISTRY_MAP = {"NMC": "nmc", "LFP": "lfp", "NCA": "nca", "nmc": "nmc", "lfp": "lfp", "nca": "nca"}


class LifecycleStages(BaseModel):
    """Annex II §4 系统边界的生命周期阶段，单位 kgCO2e/kWh。"""

    raw_material_acquisition_and_pre_processing: Decimal
    main_product_production: Decimal
    distribution: Decimal
    end_of_life_and_recycling: Decimal


class BatteryCfInput(BaseModel):
    manufacturer_name: str
    manufacturer_id: str | None = None
    battery_model_id: str
    battery_model_name: str | None = None
    battery_category: Literal["ev", "lmt", "industrial"] = "ev"
    manufacturing_plant_name: str | None = None
    manufacturing_plant_country: str  # ISO 3166-1 alpha-2
    manufacturing_plant_address: str | None = None
    carbon_footprint_per_kwh: Decimal  # dp:prod.battery.carbon_footprint_per_kwh
    lifecycle_stages: LifecycleStages | None = None
    chemistry: str | None = None  # dp:prod.battery.chemistry
    eu_declaration_of_conformity_id: str | None = None
    study_web_link: str | None = None
    created: datetime
    source_refs: dict[str, str] = Field(default_factory=dict)


def build_battery_fragment(inp: BatteryCfInput, mode: str = "upstream") -> tuple[dict[str, Any], dict[str, str]]:
    omitted: dict[str, str] = {}
    doc: dict[str, Any] = {
        "fragment": "eu_battery_passport.carbon_footprint",
        "templateMapping": "tm:eu_battery_passport@2023-1542",
        "mode": mode,
        "created": rfc3339(inp.created),
        "manufacturer": {"name": inp.manufacturer_name, **({"id": inp.manufacturer_id} if inp.manufacturer_id else {})},
        "batteryModel": {"id": inp.battery_model_id,
                         **({"name": inp.battery_model_name} if inp.battery_model_name else {}),
                         "category": inp.battery_category},
        "manufacturingPlant": {"country": inp.manufacturing_plant_country,
                               **({"name": inp.manufacturing_plant_name} if inp.manufacturing_plant_name else {}),
                               **({"address": inp.manufacturing_plant_address} if inp.manufacturing_plant_address else {})},
    }
    if inp.chemistry:
        doc["batteryChemistry"] = CHEMISTRY_MAP.get(inp.chemistry, inp.chemistry)
    cf: dict[str, Any] = {
        "declarationStatus": "not_a_declaration",
        "functionalUnit": "kWh of total energy provided over expected service life",
        "unit": "kgCO2e/kWh",
        "carbonFootprintPerFunctionalUnit": inp.carbon_footprint_per_kwh,
    }
    if inp.lifecycle_stages:
        ls = inp.lifecycle_stages
        cf["lifecycleStages"] = {
            "rawMaterialAcquisitionAndPreProcessing": ls.raw_material_acquisition_and_pre_processing,
            "mainProductProduction": ls.main_product_production,
            "distribution": ls.distribution,
            "endOfLifeAndRecycling": ls.end_of_life_and_recycling,
        }
    if mode == "upstream":
        doc["batteryCarbonFootprint"] = cf
        omitted["carbonFootprintPerformanceClass"] = "Art. 7(2) performance classes not yet defined"
        omitted["euDeclarationOfConformityId"] = "declaration-level field; not part of upstream exchange"
        omitted["studyWebLink"] = "declaration-level field; not part of upstream exchange"
    else:
        for k in ("batteryCarbonFootprint", "carbonFootprintPerformanceClass", "euDeclarationOfConformityId",
                  "studyWebLink"):
            omitted[k] = OMIT_REASON
    doc["omittedFields"] = [{"field": k, "reason": v} for k, v in omitted.items()]
    return doc, omitted


def validate_battery(doc: Mapping[str, Any]) -> list[str]:
    parsed = loads_json(dumps_json(doc))
    errors = schema_errors(parsed, BATTERY_SCHEMA)
    cf = doc.get("batteryCarbonFootprint")
    if cf and cf.get("lifecycleStages"):
        total = sum(Decimal(str(v)) for v in cf["lifecycleStages"].values())
        if total != Decimal(str(cf["carbonFootprintPerFunctionalUnit"])):
            errors.append("lifecycleStages do not sum to carbonFootprintPerFunctionalUnit")
    if doc.get("mode") == "passport" and "batteryCarbonFootprint" in doc:
        errors.append("passport mode must not contain carbon footprint declaration fields (AC-503-2)")
    return errors


def export_battery_passport_cf(inp: BatteryCfInput | Mapping[str, Any], *, mode: Literal["upstream", "passport"] = "upstream",
                               validate: bool = True) -> ExportResult:
    if not isinstance(inp, BatteryCfInput):
        inp = BatteryCfInput(**inp)
    doc, omitted = build_battery_fragment(inp, mode)
    if validate:
        errors = validate_battery(doc)
        if errors:
            raise ExportValidationError("eu_battery_passport", errors)
    return ExportResult(format=f"eu_battery_passport_cf:{mode}", document=doc, omitted_fields=omitted,
                        export_input_hash=canonical_hash({"format": "battery", "doc": doc, "refs": inp.source_refs}))

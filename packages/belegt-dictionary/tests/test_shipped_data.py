# Spec: 02 §5 (91 个初始 DataPoint), §1.3 (JSON Schema), §4 (9 个模板 ID 的映射), §6.2 (全部数据通过校验)
import json
from collections import Counter
from importlib import resources

import pytest
from jsonschema import Draft202012Validator

from belegt_dictionary import default_root
from belegt_dictionary.yamlio import load_yaml

EXPECTED_IDS = [
    "dp:org.identity.legal_name",
    "dp:org.identity.registration_number",
    "dp:org.identity.eori_number",
    "dp:org.identity.nace_code",
    "dp:org.identity.hq_country",
    "dp:org.employees.headcount",
    "dp:org.employees.fte",
    "dp:org.financials.turnover",
    "dp:org.reporting.module_option",
    "dp:env.ghg.scope1_total",
    "dp:env.ghg.scope2_location_based",
    "dp:env.ghg.scope2_market_based",
    "dp:env.ghg.scope3_total",
    "dp:env.ghg.scope3_category.c01",
    "dp:env.ghg.scope3_category.c04",
    "dp:env.ghg.intensity_turnover",
    "dp:env.ghg_target.has_target",
    "dp:env.ghg_target.reduction_pct",
    "dp:env.ghg_target.target_year",
    "dp:env.pollution.emissions_reported",
    "dp:env.water.withdrawal_total",
    "dp:env.waste.generated",
    "dp:env.waste.generated.hazardous",
    "dp:env.waste.generated.non_hazardous",
    "dp:env.waste.diverted_share",
    "dp:env.biodiversity.sites_near_sensitive_area",
    "dp:energy.consumption.total",
    "dp:energy.consumption.electricity_total",
    "dp:energy.consumption.fuels_total",
    "dp:energy.consumption.natural_gas",
    "dp:energy.consumption.renewable_share",
    "dp:energy.generation.onsite_renewable",
    "dp:energy.procurement.eac_volume",
    "dp:prod.identity.product_id",
    "dp:prod.identity.cn_code",
    "dp:prod.mass.net_per_unit",
    "dp:prod.pcf.cradle_to_gate",
    "dp:prod.pcf.cradle_to_gate.excluding_biogenic",
    "dp:prod.pcf.declared_unit",
    "dp:prod.pcf.primary_data_share",
    "dp:prod.pcf.reference_period",
    "dp:prod.cbam.installation_id",
    "dp:prod.cbam.production_route",
    "dp:prod.cbam.see_direct",
    "dp:prod.cbam.see_indirect",
    "dp:prod.cbam.specific_electricity_consumption",
    "dp:prod.cbam.carbon_price_paid",
    "dp:prod.battery.carbon_footprint_per_kwh",
    "dp:prod.battery.recycled_content.cobalt",
    "dp:prod.battery.recycled_content.lithium",
    "dp:prod.battery.recycled_content.nickel",
    "dp:prod.battery.recycled_content.lead",
    "dp:prod.battery.chemistry",
    "dp:mat.reach.svhc_present",
    "dp:mat.reach.svhc_list",
    "dp:mat.reach.svhc_concentration",
    "dp:mat.reach.candidate_list_version",
    "dp:mat.scip.scip_number",
    "dp:mat.rohs.compliant",
    "dp:mat.conflict_minerals.intentionally_added.tin",
    "dp:mat.conflict_minerals.intentionally_added.tantalum",
    "dp:mat.conflict_minerals.intentionally_added.tungsten",
    "dp:mat.conflict_minerals.intentionally_added.gold",
    "dp:mat.conflict_minerals.all_smelters_identified",
    "dp:mat.conflict_minerals.smelter_list",
    "dp:mat.conflict_minerals.policy_url",
    "dp:mat.recycled_content.steel_share",
    "dp:supply.eudr.geolocation",
    "dp:supply.eudr.production_date",
    "dp:supply.eudr.country_of_production",
    "dp:supply.eudr.commodity_quantity",
    "dp:supply.eudr.dds_reference",
    "dp:supply.eudr.dds_verification_number",
    "dp:supply.code_of_conduct.supplier_coc_in_place",
    "dp:cert.iso14001.valid_until",
    "dp:cert.iso14001.certificate_file",
    "dp:cert.iso50001.valid_until",
    "dp:cert.iso45001.valid_until",
    "dp:cert.iatf16949.valid_until",
    "dp:gov.anticorruption.convictions_count",
    "dp:gov.anticorruption.fines_amount",
    "dp:gov.policy.climate_transition_plan",
    "dp:gov.policy.human_rights_policy",
    "dp:social.workforce.headcount.female",
    "dp:social.workforce.headcount.male",
    "dp:social.workforce.temporary_count",
    "dp:social.health_safety.recordable_accidents",
    "dp:social.health_safety.recordable_accident_rate",
    "dp:social.remuneration.min_wage_compliance",
    "dp:social.collective_bargaining.coverage_rate",
    "dp:social.training.avg_hours_per_employee",
]

TEMPLATE_IDS = {"vsme", "cdp_climate", "pact_pcf", "catenax_pcf", "cbam_communication", "eudr_dds", "reach_scip", "cmrt",
                "eu_battery_passport"}


def test_exactly_91_datapoints(shipped_dict):
    assert len(EXPECTED_IDS) == 91 == len(set(EXPECTED_IDS))
    assert set(shipped_dict.datapoints) == set(EXPECTED_IDS)


def test_domain_counts(shipped_dict):
    c = Counter(dp.domain for dp in shipped_dict.datapoints.values())
    assert c == {"org": 9, "env": 17, "energy": 7, "prod": 20, "mat": 14, "supply": 7, "cert": 5, "gov": 4, "social": 8}


@pytest.mark.parametrize("dp_id", EXPECTED_IDS)
def test_each_datapoint_schema_valid_and_complete(dp_id, shipped_dict):
    root = default_root()
    p = dp_id[3:].split(".")
    data = load_yaml(root / "datapoints" / p[0] / p[1] / (".".join(p[2:]) + ".yaml"))
    schema = json.loads((root / "schema" / "datapoint.schema.json").read_text(encoding="utf-8"))
    errs = list(Draft202012Validator(schema).iter_errors(data))
    assert not errs, [e.message for e in errs]
    dp = shipped_dict.datapoints[dp_id]
    assert dp.status == "draft" and dp.introduced_in == "v2027.1" and dp.version == "1.0.0"
    assert dp.label.de and dp.label.en and dp.label.zh
    assert dp.definition.en and dp.definition.de and dp.definition.zh
    assert dp.evidence_requirements.accepted_source_types
    assert dp.sensitivity and dp.period_type and dp.boundary
    if dp.value_type == "number":
        assert dp.unit
    if dp.cardinality == "many":
        assert dp.record_fields


def test_shipped_tree_passes_all_checks(shipped_report):
    assert shipped_report.ok, "\n".join(map(str, shipped_report.errors))
    assert not shipped_report.warnings, "\n".join(map(str, shipped_report.warnings))
    assert shipped_report.counts["datapoints"] == 91


def test_all_nine_template_ids_have_mappings(shipped_dict):
    tids = {tm.template_id for tm in shipped_dict.template_mappings.values()}
    assert tids == TEMPLATE_IDS
    assert set(shipped_dict.template_mappings) == {
        "tm:vsme@eu-2026-1560", "tm:cdp_climate@2025", "tm:pact_pcf@2.3.0", "tm:pact_pcf@3.0.0", "tm:catenax_pcf@cx-0136",
        "tm:cbam_communication@2024", "tm:eudr_dds@2023-1115", "tm:reach_scip@echa-scip-3", "tm:cmrt@6.5",
        "tm:eu_battery_passport@2023-1542"}


def test_every_mapping_reference_exists(shipped_dict):
    missing = [(tm.id, e.entry_id, i) for tm in shipped_dict.template_mappings.values() for e in tm.entries
               for i in e.datapoint_ids() if i not in shipped_dict.datapoints]
    assert not missing
    conds = [e.condition for tm in shipped_dict.template_mappings.values() for e in tm.entries if e.condition]
    assert conds  # conditional entries exist (02 §4.4)


def test_doc_examples_present_in_mappings(shipped_dict):
    d = shipped_dict
    assert d.entry("tm:vsme@eu-2026-1560#b3_33a_scope1").datapoint_ids == ["dp:env.ghg.scope1_total"]
    assert d.entry("tm:pact_pcf@3.0.0#pcf_excl_biogenic_uptake").entry.locator.ref == "$.pcf.pcfExcludingBiogenicUptake"
    assert d.entry("tm:cdp_climate@2025#m7_6_scope1").entry.locator.legacy_ref == "C6.1"
    split = d.entry("tm:cmrt@6.5#decl_q1_intentional")
    assert len(split.datapoint_ids) == 4


def test_short_labels_within_60_chars(shipped_dict):
    for tm in shipped_dict.template_mappings.values():
        for e in tm.entries:
            assert len(e.short_label) <= 60


def test_package_files_present():
    root = default_root()
    for rel in ("schema/datapoint.schema.json", "schema/template_mapping.schema.json", "schema/migration.schema.json",
                "units/units.yaml", "units/conversions.yaml", "aliases/aliases.yaml", "refs/methodologies.yaml",
                "refs/abbreviations.yaml", "enums/qualifiers.yaml", "enums/iso3166_alpha2.yaml", "migrations/v2027.1.yaml",
                "migrations/v2027.1.md", "release.yaml"):
        assert (root / rel).exists(), rel
    assert resources.files("belegt_dictionary").joinpath("data/release.yaml").is_file()


def test_gj_to_mwh_factor_is_exact_rational():
    conv = load_yaml(default_root() / "units/conversions.yaml")["conversions"]
    gj = next(c for c in conv if c["from"] == "GJ" and c["to"] == "MWh")
    assert gj["factor"] == "1/3.6"


def test_emission_numbers_never_human_confirmable(shipped_dict):
    for dp in shipped_dict.datapoints.values():
        if dp.unit and ("CO2e" in dp.unit):
            assert dp.evidence_requirements.human_confirmation_allowed is False, dp.id


def test_iso3166_has_249_codes(shipped_dict):
    assert len(shipped_dict.enum_values("enums/iso3166_alpha2.yaml")) == 249

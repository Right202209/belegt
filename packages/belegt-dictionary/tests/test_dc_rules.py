# Spec: 02 §6.2 — one positive (shipped tree clean) and at least one negative case per DC-01…DC-12
from __future__ import annotations

import pytest

from conftest import minimal_dp

ALL_RULES = [f"DC-{i:02d}" for i in range(1, 13)]


def errs(rep, rule):
    return [f for f in rep.by_rule(rule) if f.level == "error"]


@pytest.mark.parametrize("rule", ALL_RULES[:11])
def test_positive_shipped_tree_has_no_errors(shipped_report, rule):
    assert not errs(shipped_report, rule)


# ------------------------------------------------------------------ DC-01 schema lint
def test_dc01_schema_violation(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(sensitivity="secret"))
    rep = tree.validate()
    assert any("sensitivity" in f.message for f in errs(rep, "DC-01"))


def test_dc01_unknown_field_rejected(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(value=42))
    assert any("Additional properties" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_number_requires_unit(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d.pop("unit"))
    assert any("unit" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_yaml_format(tree):
    p = tree.path(tree.dp_rel("dp:env.ghg.scope1_total"))
    p.write_text(p.read_text(encoding="utf-8").replace("version: 1.0.0", "version:   1.0.0"), encoding="utf-8")
    assert any("canonical format" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_yaml_parse_error(tree):
    p = tree.path(tree.dp_rel("dp:env.ghg.scope1_total"))
    p.write_text("id: [unclosed\n", encoding="utf-8")
    assert any("YAML parse error" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_emission_human_confirmation_forbidden(tree):
    def f(d):
        d["evidence_requirements"]["human_confirmation_allowed"] = True
        d["evidence_requirements"]["accepted_source_types"].append("human_confirmation")
    tree.edit_dp("dp:env.ghg.scope1_total", f)
    assert any("emission numbers" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_human_number_needs_tag(tree):
    tree.edit_dp("dp:org.employees.headcount", lambda d: d["tags"].remove("human_confirmable_number"))
    assert any("human_confirmable_number" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_product_per_unit_needs_composite_unit(tree):
    tree.edit_dp("dp:prod.mass.net_per_unit", lambda d: d.update(boundary=["product_per_unit"]))
    assert any("product_per_unit" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_migration_schema(tree):
    tree.write("migrations/v2027.2.yaml", {"release": "v2027.2", "package_version": "0.2.0", "previous": "v2027.1",
                                           "changes": [{"kind": "split", "from": "dp:env.ghg.scope1_total", "auto_migrate": False}]})
    assert any("action_required" in f.message for f in errs(tree.validate(), "DC-01"))


def test_dc01_mapping_schema(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: m["entries"][0].update(direction="sideways"))
    assert any("direction" in f.message or "sideways" in f.message for f in errs(tree.validate(), "DC-01"))


# ------------------------------------------------------------------ DC-02 ID unique / path
def test_dc02_duplicate_id(tree):
    d = tree.dp("dp:env.ghg.scope1_total")
    tree.write("datapoints/env/ghg/copy_of_scope1.yaml", d)
    msgs = [f.message for f in errs(tree.validate(), "DC-02")]
    assert any("duplicate DataPoint id" in m for m in msgs)
    assert any("path does not match id" in m for m in msgs)


def test_dc02_path_mismatch(tree):
    d = tree.dp("dp:cert.iso45001.valid_until")
    tree.path(tree.dp_rel(d["id"])).unlink()
    tree.write("datapoints/cert/iso45001/expiry.yaml", d)
    assert any("expected datapoints/cert/iso45001/valid_until.yaml" in f.message for f in errs(tree.validate(), "DC-02"))


def test_dc02_retired_id_not_reused(make_tree):
    base = make_tree("base")
    base.edit_dp("dp:mat.rohs.compliant", lambda d: d.update(
        status="retired", deprecation={"since": "v2027.1", "reason": "test", "replaced_by": [], "retire_after": "v2027.3"}))
    cur = make_tree("cur")  # same id back as draft
    rep = cur.validate(baseline=base.root)
    assert any("retired id" in f.message for f in errs(rep, "DC-02"))


def test_dc02_duplicate_entry_id(tree):
    tree.edit_mapping("cmrt", "6.5", lambda m: m["entries"].append(dict(m["entries"][0])))
    assert any("duplicate entry_id company_name" in f.message for f in errs(tree.validate(), "DC-02"))


# ------------------------------------------------------------------ DC-03 ID naming
@pytest.mark.parametrize("bad_id,needle", [
    ("dp:env.ghg.cdp_scope1", "template/regulation name 'cdp'"),
    ("dp:env.ghg.scope1_total_t", "unit 't'"),
    ("dp:energy.consumption.electricity_mwh", "unit 'mwh'"),
    ("dp:env.ghg.scope1_2025", "year/period '2025'"),
    ("dp:env.ghg.scope1_site_a", "boundary/tenant"),
    ("dp:mat.reach.no_svhc", "negative form"),
    ("dp:env.ghg.scope1_xyz", "unregistered abbreviation"),
    ("dp:env.waste.generated.radioactive", "qualifier 'radioactive' not registered"),
])
def test_dc03_naming_taboos(tree, bad_id, needle):
    tree.add_dp(minimal_dp(bad_id))
    rep = tree.validate(only=["DC-03"])
    assert any(needle in f.message for f in errs(rep, "DC-03")), [str(f) for f in rep.findings]


def test_dc03_regex(tree):
    tree.add_dp(minimal_dp("dp:Env.ghg.Scope1"), rel="datapoints/env/ghg/bad.yaml")
    assert any("does not match" in f.message for f in errs(tree.validate(only=["DC-03"]), "DC-03"))


def test_dc03_allowed_patterns(tree):
    # regulation name as topic, per-unit functional descriptor, registered abbreviation
    for ok in ("dp:prod.cbam.cbam_route_variant", "dp:prod.battery.energy_per_kwh", "dp:org.identity.eori_issuer"):
        tree.add_dp(minimal_dp(ok))
    assert not errs(tree.validate(only=["DC-03"]), "DC-03")


# ------------------------------------------------------------------ DC-04 mapping references
def test_dc04_missing_datapoint(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: m["entries"][0].update(datapoint_id="dp:org.identity.does_not_exist"))
    assert any("does not exist" in f.message for f in errs(tree.validate(), "DC-04"))


def test_dc04_missing_in_transform(tree):
    def f(m):
        e = next(x for x in m["entries"] if x["entry_id"] == "b7_39a_waste")
        e["transform"]["outputs"][0]["datapoint_id"] = "dp:env.waste.generated.radioactive"
    tree.edit_mapping("vsme", "eu-2026-1560", f)
    assert any("radioactive" in f.message for f in errs(tree.validate(), "DC-04"))


def test_dc04_retired_is_error_deprecated_is_warn(tree):
    tree.add_dp(minimal_dp("dp:mat.rohs.exemption_used"))
    tree.edit_dp("dp:mat.rohs.compliant", lambda d: d.update(
        status="deprecated", deprecation={"since": "v2027.1", "reason": "test", "replaced_by": ["dp:mat.rohs.exemption_used"],
                                          "retire_after": "v2027.3"}))
    tree.edit_dp("dp:mat.scip.scip_number", lambda d: d.update(
        status="retired", deprecation={"since": "v2027.1", "reason": "test", "replaced_by": [], "retire_after": "v2027.1"}))
    tree.edit_mapping("cmrt", "6.5", lambda m: m["entries"].append(
        {"entry_id": "rohs_test", "locator": {"kind": "field", "ref": "x"}, "short_label": "RoHS test",
         "datapoint_id": "dp:mat.rohs.compliant", "direction": "both", "transform": {"type": "identity"}}))
    rep = tree.validate()
    assert any("retired" in f.message for f in errs(rep, "DC-04"))
    assert any("deprecated" in f.message for f in rep.by_rule("DC-04", "warn"))


def test_dc04_condition_reference(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: next(e for e in m["entries"] if e["entry_id"] == "c3_reduction_pct")
                      .update(condition="dp:env.ghg_target.has_goal == true"))
    assert any("condition references unknown" in f.message for f in errs(tree.validate(), "DC-04"))


def test_dc04_boundary_hint_not_allowed(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: m["entries"][0].update(boundary_hint="product"))
    assert any("boundary_hint" in f.message for f in errs(tree.validate(), "DC-04"))


# ------------------------------------------------------------------ DC-05 units
def test_dc05_allowed_unit_cross_dimension(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["allowed_units"].append("t"))
    assert any("not same dimension" in f.message for f in errs(tree.validate(), "DC-05"))


def test_dc05_unknown_unit(tree):
    tree.edit_dp("dp:env.water.withdrawal_total", lambda d: d["allowed_units"].append("gallon"))
    assert any("gallon" in f.message for f in errs(tree.validate(), "DC-05"))


def test_dc05_unit_convert_cross_dimension(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: next(e for e in m["entries"] if e["entry_id"] == "b3_32_energy_total")
                      .update(transform={"type": "unit_convert", "from_unit": "MWh", "to_unit": "tCO2e"}))
    assert any("across dimensions" in f.message for f in errs(tree.validate(), "DC-05"))


def test_dc05_no_conversion_path(tree):
    # same dimension but no edge: kgCO2e/kWh → tCO2e/MWh is not in conversions.yaml
    tree.edit_dp("dp:prod.battery.carbon_footprint_per_kwh", lambda d: d.update(allowed_units=["tCO2e/MWh"]))
    rep = tree.validate()
    assert any("no conversion path" in f.message for f in errs(rep, "DC-05"))


def test_dc05_calc_internal_unit_not_allowed(tree):
    tree.edit_dp("dp:prod.battery.carbon_footprint_per_kwh", lambda d: d.update(unit="tCO2e/MWh"))
    assert any("calc-internal" in f.message for f in errs(tree.validate(), "DC-05"))


def test_dc05_unit_hint_not_convertible(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: next(e for e in m["entries"] if e["entry_id"] == "b3_33a_scope1")
                      .update(unit_hint="MWh"))
    assert any("unit_hint MWh not convertible" in f.message for f in errs(tree.validate(), "DC-05"))


# ------------------------------------------------------------------ DC-06 enum maps
def test_dc06_enum_target_missing(tree):
    tree.edit_mapping("cbam_communication", "2024", lambda m: next(e for e in m["entries"] if e["entry_id"] == "production_route")
                      ["transform"]["map"].update({"Hydrogen DRI": "h2_dri"}))
    assert any("h2_dri" in f.message for f in errs(tree.validate(), "DC-06"))


def test_dc06_bool_target_must_be_bool(tree):
    tree.edit_mapping("reach_scip", "echa-scip-3", lambda m: next(e for e in m["entries"] if e["entry_id"] == "svhc_present")
                      ["transform"]["map"].update({"Unknown": "maybe"}))
    assert any("must be true/false" in f.message for f in errs(tree.validate(), "DC-06"))


def test_dc06_enum_ref_file_missing(tree):
    tree.path("enums/battery_chemistry.yaml").unlink()
    assert any("enums/battery_chemistry.yaml not found" in f.message for f in errs(tree.validate(), "DC-06"))


# ------------------------------------------------------------------ DC-07 aliases
def test_dc07_alias_chain(tree):
    a = tree.read("aliases/aliases.yaml")
    a["aliases"].append({"alias": "dp:env.ghg.scope2_old", "target": "dp:env.ghg.scope2_total", "kind": "deprecated_id",
                         "since": "v2027.1"})
    tree.write("aliases/aliases.yaml", a)
    assert any("points to another alias" in f.message for f in errs(tree.validate(), "DC-07"))


def test_dc07_alias_collides_with_current_id(tree):
    a = tree.read("aliases/aliases.yaml")
    a["aliases"].append({"alias": "dp:env.ghg.scope1_total", "target": "dp:env.ghg.scope3_total", "kind": "community_alias",
                         "since": "v2027.1"})
    tree.write("aliases/aliases.yaml", a)
    assert any("collides" in f.message for f in errs(tree.validate(), "DC-07"))


def test_dc07_alias_target_missing(tree):
    a = tree.read("aliases/aliases.yaml")
    a["aliases"][0]["target"] = "dp:env.ghg.scope2_nowhere"
    tree.write("aliases/aliases.yaml", a)
    assert any("does not exist" in f.message for f in errs(tree.validate(), "DC-07"))


def test_dc07_datapoint_alias_conflicts_with_table(tree):
    tree.edit_dp("dp:env.ghg.scope3_total", lambda d: d.update(aliases=["dp:env.ghg.scope2_total"]))
    assert any("also declared in aliases.yaml" in f.message for f in errs(tree.validate(), "DC-07"))


# ------------------------------------------------------------------ DC-08 i18n
def test_dc08_missing_label_language(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["label"].pop("de"))
    assert any("label.de" in f.message for f in errs(tree.validate(), "DC-08"))


def test_dc08_blank_label(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["label"].update(zh="   "))
    assert any("label.zh" in f.message for f in errs(tree.validate(), "DC-08"))


def test_dc08_missing_definition_en(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["definition"].pop("en"))
    assert any("definition.en" in f.message for f in errs(tree.validate(), "DC-08"))


# ------------------------------------------------------------------ DC-09 version bump
def test_dc09_no_baseline_no_findings(shipped_report):
    assert not shipped_report.by_rule("DC-09")


def test_dc09_patch_change_requires_bump(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    cur.edit_dp("dp:env.ghg.scope1_total", lambda d: d["label"].update(en="Scope 1 GHG emissions, gross"))
    rep = cur.validate(baseline=base.root)
    assert any("PATCH change" in f.message for f in errs(rep, "DC-09"))
    cur.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(version="1.0.1"))
    assert not errs(cur.validate(baseline=base.root), "DC-09")


def test_dc09_removed_allowed_unit_is_major(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    cur.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(allowed_units=["tCO2e"], version="1.1.0"))
    rep = cur.validate(baseline=base.root)
    assert any("MAJOR change" in f.message for f in errs(rep, "DC-09"))
    cur.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(version="2.0.0"))
    assert not errs(cur.validate(baseline=base.root), "DC-09")


def test_dc09_added_allowed_unit_is_minor(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    cur.edit_dp("dp:env.water.withdrawal_total", lambda d: d.update(allowed_units=["m3", "L"], version="1.0.1"))
    assert not errs(cur.validate(baseline=base.root), "DC-09")  # no change (same set) → bump only warns
    cur.edit_dp("dp:energy.procurement.eac_volume", lambda d: d.update(allowed_units=["kWh", "MWh", "GJ"]))
    rep = cur.validate(baseline=base.root)
    assert any("MINOR change" in f.message for f in errs(rep, "DC-09"))


def test_dc09_redefinition_needs_new_id(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    cur.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(unit="kgCO2e", version="2.0.0"))
    assert any("requires a new id" in f.message for f in errs(cur.validate(baseline=base.root), "DC-09"))


def test_dc09_version_decrease_and_removal(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    base.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(version="1.2.0"))
    cur.path(cur.dp_rel("dp:mat.rohs.compliant")).unlink()
    rep = cur.validate(baseline=base.root)
    msgs = [f.message for f in errs(rep, "DC-09")]
    assert any("version decreased" in m for m in msgs)
    assert any("removed" in m for m in msgs)


def test_dc09_frozen_mapping_modified(make_tree):
    base, cur = make_tree("base"), make_tree("cur")
    base.edit_mapping("cmrt", "6.5", lambda m: m.update(status="frozen"))
    cur.edit_mapping("cmrt", "6.5", lambda m: m.update(status="frozen", template_source="RMI CMRT 6.5 (changed)"))
    assert any("frozen mapping" in f.message for f in errs(cur.validate(baseline=base.root), "DC-09"))


# ------------------------------------------------------------------ DC-10 original text detection
def test_dc10_detects_copied_definition(tree, tmp_path):
    copied = tree.dp("dp:env.ghg.scope1_total")["definition"]["en"]
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "template.txt").write_text("Official questionnaire text. " + copied, encoding="utf-8")
    rep = tree.validate(corpus=corpus)
    assert any("dp:env.ghg.scope1_total" in f.path or "scope1_total" in f.path for f in errs(rep, "DC-10"))


def test_dc10_detects_copied_short_label(tree, tmp_path):
    corpus = tmp_path / "c.txt"
    corpus.write_text("Please state the Scope 2 location-based, tCO2e figure for the year.", encoding="utf-8")
    rep = tree.validate(corpus=corpus)
    assert any("short_label" in f.message for f in errs(rep, "DC-10"))


def test_dc10_unrelated_corpus_clean(tree, tmp_path):
    corpus = tmp_path / "c.txt"
    corpus.write_text("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor.", encoding="utf-8")
    assert not errs(tree.validate(corpus=corpus), "DC-10")


def test_dc10_cjk_tokens():
    from belegt_dictionary.validate import build_corpus, similarity
    c = build_corpus(["报告边界内消耗的外购电力所产生的间接排放"])
    assert similarity("报告边界内消耗的外购电力所产生的间接排放", c, 5) == 1.0


# ------------------------------------------------------------------ DC-11 relations
def test_dc11_sum_of_unit_mismatch(tree):
    tree.edit_dp("dp:env.waste.generated.hazardous", lambda d: d.update(unit="kg"))
    assert any("sum_of component" in f.message for f in errs(tree.validate(), "DC-11"))


def test_dc11_replaced_by_missing(tree):
    tree.edit_dp("dp:mat.rohs.compliant", lambda d: d.update(
        status="deprecated", deprecation={"since": "v2027.1", "reason": "x", "replaced_by": ["dp:mat.rohs.gone"],
                                          "retire_after": "v2027.3"}))
    assert any("replaced_by dp:mat.rohs.gone" in f.message for f in errs(tree.validate(), "DC-11"))


def test_dc11_related_missing_and_bad_expr(tree):
    def f(d):
        d["related_datapoints"].append({"id": "dp:env.ghg.scope9_total", "relation": "see_also"})
        d["validity_rule"]["plausibility"] = [{"expr": "value <= http_get(dp:energy.consumption.total)", "severity": "warn"}]
    tree.edit_dp("dp:env.ghg.scope1_total", f)
    msgs = [x.message for x in errs(tree.validate(), "DC-11")]
    assert any("scope9_total" in m for m in msgs)
    assert any("CEL subset" in m for m in msgs)


def test_dc11_unregistered_methodology(tree):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["methodology_refs"].append({"ref": "my_secret_method"}))
    assert any("my_secret_method" in f.message for f in errs(tree.validate(), "DC-11"))


def test_dc11_missing_slice_link_warns(tree):
    tree.edit_dp("dp:env.waste.generated", lambda d: d.update(related_datapoints=[
        {"id": "dp:env.waste.generated.hazardous", "relation": "sum_of"}]))
    rep = tree.validate()
    assert any("non_hazardous" in f.message for f in rep.by_rule("DC-11", "warn"))


# ------------------------------------------------------------------ DC-12 coverage
def test_dc12_coverage_report(shipped_report):
    cov = shipped_report.coverage["templates"]
    assert set(cov) >= {"vsme", "cdp_climate", "pact_pcf", "catenax_pcf", "cbam_communication", "eudr_dds", "reach_scip",
                        "cmrt", "eu_battery_passport"}
    assert all(f.level == "info" for f in shipped_report.by_rule("DC-12"))
    assert cov["vsme"]["coverage_pct"] == 100.0
    assert shipped_report.coverage["external_contribution_share_pct"] is None


def test_dc12_external_share(tree):
    tree.write("contributions.yaml", {"contributions": [{"path": "a", "external": True}, {"path": "b", "external": False},
                                                        {"path": "c", "external": True}, {"path": "d", "external": False}]})
    rep = tree.validate()
    assert rep.coverage["external_contribution_share_pct"] == 50.0


def test_dc12_never_fails(tree):
    tree.edit_mapping("vsme", "eu-2026-1560", lambda m: m.update(entries=m["entries"][:1]))
    rep = tree.validate()
    assert rep.coverage["templates"]["vsme"]["coverage_pct"] < 100
    assert not [f for f in rep.by_rule("DC-12") if f.level != "info"]

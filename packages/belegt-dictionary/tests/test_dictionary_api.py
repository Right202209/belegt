# Spec: 02 §2.5 别名解析、§4.2 tm 引用、§4.4 命中优先级；CONTRACTS.md load_dictionary
import logging

import pytest

from belegt_dictionary import DataPoint, Dictionary, DictionaryError, TemplateMapping, load_dictionary


def test_contract_shape(shipped_dict):
    assert isinstance(shipped_dict, Dictionary)
    assert isinstance(shipped_dict.datapoints["dp:env.ghg.scope1_total"], DataPoint)
    assert isinstance(shipped_dict.template_mappings["tm:vsme@eu-2026-1560"], TemplateMapping)
    assert "tCO2e" in shipped_dict.units
    assert shipped_dict.release["release"] == "v2027.1"


def test_get_with_version(shipped_dict):
    assert shipped_dict.get("dp:env.ghg.scope1_total@1.0.0").ref == "dp:env.ghg.scope1_total@1.0.0"
    with pytest.raises(KeyError):
        shipped_dict.get("dp:env.ghg.scope1_total@9.0.0")


def test_alias_resolution_logs_warning(shipped_dict, caplog):
    with caplog.at_level(logging.WARNING, logger="belegt_dictionary"):
        assert shipped_dict.resolve_id("dp:env.ghg.scope2_total") == "dp:env.ghg.scope2_location_based"
        assert shipped_dict.get("dp:energy.consumption.green_power_share").id == "dp:energy.consumption.renewable_share"
    assert any("alias" in r.message for r in caplog.records)
    assert "dp:env.ghg.scope2_total" in shipped_dict
    assert "dp:env.ghg.nothing_here" not in shipped_dict
    with pytest.raises(KeyError):
        shipped_dict.get("dp:env.ghg.scope2_total", follow_aliases=False)


def test_find_by_template_field(shipped_dict):
    m = shipped_dict.find_by_template_field("vsme", "B3 Para 33(a)")
    assert [x.datapoint_ids for x in m] == [["dp:env.ghg.scope1_total"]]
    assert m[0].ref == "tm:vsme@eu-2026-1560#b3_33a_scope1"
    # legacy CDP numbering (FR-201)
    assert shipped_dict.find_by_template_field("cdp_climate", "C6.1")[0].datapoint_ids == ["dp:env.ghg.scope1_total"]
    assert shipped_dict.find_by_template_field("cdp_climate", "C6.1", include_legacy=False) == []
    # column narrowing
    lb = shipped_dict.find_by_template_field("cdp_climate", "7.7", column="location-based")
    assert lb[0].datapoint_ids == ["dp:env.ghg.scope2_location_based"]
    # version narrowing
    v3 = shipped_dict.find_by_template_field("pact_pcf", "$.pcf.declaredUnitOfMeasurement", template_version="3.0.0")
    assert v3[0].datapoint_ids == ["dp:prod.pcf.declared_unit"]
    assert shipped_dict.find_by_template_field("pact_pcf", "$.pcf.declaredUnitOfMeasurement", template_version="2.3.0") == []
    # split entry returns all outputs
    w = shipped_dict.find_by_template_field("vsme", "B7 Para 39(a)", direction="import")
    assert set(w[0].datapoint_ids) == {"dp:env.waste.generated", "dp:env.waste.generated.hazardous",
                                       "dp:env.waste.generated.non_hazardous"}
    # direction filter: export matches export + both
    exp = shipped_dict.find_by_template_field("vsme", "B7 Para 39(a)", direction="export")
    assert [e.entry.entry_id for e in exp] == ["b7_39a_waste_total"]


def test_entry_ref_errors(shipped_dict):
    with pytest.raises(KeyError):
        shipped_dict.entry("tm:vsme@eu-2026-1560")
    with pytest.raises(KeyError):
        shipped_dict.entry("tm:vsme@eu-2026-1560#nope")


def test_datapoints_for_template_and_tags(shipped_dict):
    ids = shipped_dict.datapoints_for_template("cbam_communication")
    assert {"dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect", "dp:prod.identity.cn_code"} <= ids
    assert {d.id for d in shipped_dict.by_tag("eudr_dds")} >= {"dp:supply.eudr.geolocation"}


def test_datapoint_helpers(shipped_dict):
    dp = shipped_dict.get("dp:env.waste.generated.hazardous")
    assert dp.qualifier == "hazardous" and dp.parent_id == "dp:env.waste.generated"
    parent = shipped_dict.get("dp:env.waste.generated")
    assert set(parent.related("sum_of")) == {"dp:env.waste.generated.hazardous", "dp:env.waste.generated.non_hazardous"}


def test_strict_load_raises(tmp_path):
    import shutil
    from belegt_dictionary import default_root
    root = tmp_path / "d"
    shutil.copytree(default_root(), root)
    (root / "datapoints/env/ghg/scope1_total.yaml").write_text("id: dp:env.ghg.scope1_total\n", encoding="utf-8")
    with pytest.raises(DictionaryError):
        load_dictionary(root)
    d = load_dictionary(root, strict=False)
    assert "dp:env.ghg.scope1_total" not in d.datapoints and len(d.datapoints) == 90
    with pytest.raises(DictionaryError):
        load_dictionary(tmp_path / "missing")


def test_models_enforce_conditionals():
    from pydantic import ValidationError
    from conftest import minimal_dp
    with pytest.raises(ValidationError):
        DataPoint.model_validate(minimal_dp("dp:env.ghg.x_total", value_type="number"))
    with pytest.raises(ValidationError):
        DataPoint.model_validate(minimal_dp("dp:env.ghg.x_total", value_type="enum"))
    with pytest.raises(ValidationError):
        DataPoint.model_validate(minimal_dp("dp:env.ghg.x_total", cardinality="many"))
    with pytest.raises(ValidationError):
        DataPoint.model_validate(minimal_dp("dp:env.ghg.x_total", status="deprecated"))
    with pytest.raises(ValidationError):
        TemplateMapping.model_validate({"template_mapping": "tm:vsme@x", "template_id": "vsme", "template_version": "y",
                                        "template_source": "src", "dictionary_min_version": "v2027.1",
                                        "entries": [{"entry_id": "a", "locator": {"kind": "field", "ref": "r"},
                                                     "short_label": "s", "datapoint_id": "dp:org.identity.legal_name",
                                                     "direction": "both"}]})

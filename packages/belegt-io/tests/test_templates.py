# Spec: 02 §4（9 个 template_id、TemplateMapping 格式、命中优先级 §4.4）；04 §2.2 指纹
from __future__ import annotations

import json

from openpyxl import Workbook

from belegt_io import TEMPLATE_IDS, detect_json_template, load_templates, parse_questionnaire
from belegt_io.examples import sample_pcf_input
from belegt_io.export import export_catenax_pcf, export_pact


def test_all_nine_templates_load():
    t = load_templates()
    assert set(t) == set(TEMPLATE_IDS) and len(t) == 9
    for tid, doc in t.items():
        assert doc["template_mapping"] == f"tm:{tid}@{doc['template_version']}"
        ids = [e["entry_id"] for e in doc["entries"]]
        assert len(ids) == len(set(ids)) and ids
        for e in doc["entries"]:
            assert e["datapoint_id"].startswith("dp:")
            assert e["locator"]["kind"] in {"paragraph", "question", "cell", "column", "json_path", "xml_path", "field"}
            assert e.get("transform", {"type": "identity"})["type"] in {
                "identity", "unit_convert", "enum_map", "aggregate", "split", "format", "pipeline"}


def test_cbam_fingerprint_and_exact_mapping(ex_dir):
    p = parse_questionnaire(ex_dir / "cbam.xlsx")
    assert p.template.template_id == "cbam_communication" and p.template.confidence >= 0.85
    assert p.template_ref == "tm:cbam_communication@2024"
    hits = {h.template_mapping_ref for q in p.questions for h in q.exact_mappings}
    assert hits == {f"tm:cbam_communication@2024#{e}" for e in
                    ("inst_id", "cn_code", "see_direct", "see_indirect", "sec", "production_route")}
    inst = [q for q in p.questions if q.hints.get("label") == "Installation identifier"][0]
    assert inst.locator.sheet == "A_InstData" and inst.locator.cell == "C3"
    assert inst.exact_mappings[0].datapoint_id == "dp:prod.cbam.installation_id"
    assert inst.exact_mappings[0].method == "exact_template"
    see = [q for q in p.questions if q.native_id == "SEE (direct)#1"][0]
    assert see.unit_requested == "tCO2e/t" and see.exact_mappings[0].transform["round"] == 6


def test_cmrt_fingerprint_and_split(ex_dir):
    p = parse_questionnaire(ex_dir / "cmrt.xlsx")
    assert p.template.template_id == "cmrt" and p.template.is_exact
    q = p.by_native_id()
    assert q["Q1.Tin"].exact_mappings[0].datapoint_id == "dp:mat.conflict_minerals.intentionally_added.tin"
    assert q["Q1.Gold"].options == ["Yes", "No", "Unknown"]
    assert q["Q5.Tin"].exact_mappings[0].datapoint_id == "dp:mat.conflict_minerals.all_smelters_identified"
    assert q["B"].exact_mappings[0].datapoint_id == "dp:mat.conflict_minerals.policy_url"


def test_cdp_and_vsme_by_question_ids(tmp_path):
    def wb_with(rows, title):
        wb = Workbook()
        ws = wb.active
        ws.title = "Questionnaire"
        ws["A1"] = title
        ws.append([])
        ws.append(["Question number", "Question", "Answer", "Unit"])
        for r in rows:
            ws.append(r)
        return wb

    cdp = tmp_path / "cdp.xlsx"
    wb_with([["7.6", "Gross global Scope 1 emissions", None, "tCO2e"],
             ["7.7", "Gross global Scope 2 emissions", None, "tCO2e"],
             ["7.30.1", "Total energy consumption", None, "MWh"]], "CDP Climate change questionnaire 2025").save(cdp)
    p = parse_questionnaire(cdp)
    assert p.template.template_id == "cdp_climate" and p.template.is_exact
    assert p.by_native_id()["7.6"].exact_mappings[0].datapoint_id == "dp:env.ghg.scope1_total"
    assert p.by_native_id()["7.30.1"].exact_mappings[0].datapoint_id == "dp:energy.consumption.total"

    vsme = tmp_path / "vsme.xlsx"
    wb_with([["B3 Para 32", "Total energy consumption", None, "MWh"],
             ["B3 Para 33(a)", "Scope 1 GHG emissions", None, "tCO2e"]], "VSME Basic Module report").save(vsme)
    p = parse_questionnaire(vsme)
    assert p.template.template_id == "vsme" and p.template.is_exact
    assert p.by_native_id()["B3 Para 33(a)"].exact_mappings[0].template_mapping_ref == "tm:vsme@eu-2026-1560#b3_33a_scope1"


def test_low_confidence_does_not_use_exact(oem_parsed):
    assert oem_parsed.template is None or oem_parsed.template.confidence < 0.85
    assert oem_parsed.template_ref is None


def test_json_template_detection():
    pact = export_pact(sample_pcf_input()).document
    assert detect_json_template(pact).template_id == "pact_pcf"
    assert detect_json_template({"data": [pact]}).template_id == "pact_pcf"
    cx = json.loads(export_catenax_pcf(sample_pcf_input()).to_json())
    assert detect_json_template(cx).template_id == "catenax_pcf"
    assert detect_json_template({"batteryCarbonFootprint": {}, "batteryChemistry": "nmc"}).template_id == "eu_battery_passport"

# Spec: 03 §3.5 Evidence.locator（文件哈希 + 页/单元格/bbox）、R-LOC；02 §3.6
from __future__ import annotations

from decimal import Decimal

from openpyxl import Workbook

from belegt_io import Locator, extract_pdf_evidence, extract_text_evidence, extract_xlsx_evidence, file_sha256


def test_pdf_evidence_with_page_and_bbox(ex_dir):
    pdf = ex_dir / "strom.pdf"
    cands = extract_pdf_evidence(pdf)
    sha = file_sha256(pdf)
    by_val = {(c.extracted_value, c.unit): c for c in cands}
    elec = by_val[("4820000", "kWh")]
    assert elec.fact_key == "activity.electricity.purchased"
    assert elec.locator.blob_sha256 == sha and elec.locator.page == 1 and elec.locator.mime == "application/pdf"
    assert elec.locator.bbox is not None and elec.locator.bbox[0] == 72.0 and elec.locator.bbox[3] > elec.locator.bbox[1]
    assert elec.locator.text_excerpt_hash.startswith("sha256:")
    assert elec.confidence >= 0.9 and not elec.needs_confirmation
    assert elec.period.start.isoformat() == "2025-01-01" and elec.period.end.isoformat() == "2025-12-31"
    gas = by_val[("2150", "MWh")]  # "2.150" 歧义，由同文档德式数字解决
    assert gas.fact_key == "activity.natural_gas.consumed" and "resolved_by_document_style" in gas.reasons
    assert ("1234.5", "MWh") in by_val and ("1835.2", "tCO2e") in by_val and ("1234567.89", "EUR") in by_val
    # R-LOC：document 证据必须有 blob_sha256 + page/cell/bbox
    for c in cands:
        f = c.to_evidence_fields()
        assert f["source_type"] == "document" and f["locator"]["blob_sha256"] == sha and "page" in f["locator"]
        assert f["status"] == "candidate"


def test_pdf_keyword_filter(ex_dir):
    cands = extract_pdf_evidence(ex_dir / "strom.pdf", keywords=["Erdgas"])
    assert [(c.extracted_value, c.unit) for c in cands] == [("2150", "MWh")]


def test_xlsx_evidence(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Energie"
    ws.append(["Kennzahl", "Wert", "Einheit"])
    ws.append(["Stromverbrauch 2025", 4820, "MWh"])
    ws.append(["Anteil Ökostrom", 0.425, None])
    ws["B3"].number_format = "0.0%"
    ws.append(["Erdgas", "2.150,5 MWh", None])
    ws.append(["Anteil Recycling", 0.35, "%"])
    p = tmp_path / "e.xlsx"
    wb.save(p)
    cands = extract_xlsx_evidence(p)
    by_cell = {c.locator.cell: c for c in cands}
    assert by_cell["B2"].extracted_value == "4820" and by_cell["B2"].unit == "MWh"
    assert by_cell["B2"].fact_key == "activity.electricity.purchased" and by_cell["B2"].locator.sheet == "Energie"
    assert by_cell["B3"].extracted_value == "42.5" and by_cell["B3"].unit == "%"  # 百分比格式 ×100
    assert by_cell["B4"].extracted_value == "2150.5" and by_cell["B4"].unit == "MWh"
    assert by_cell["B5"].needs_confirmation and "percent_value_le_1" in by_cell["B5"].reasons
    only = extract_xlsx_evidence(p, cells=["Energie!B2"])
    assert len(only) == 1 and only[0].locator == Locator(blob_sha256=file_sha256(p), mime=only[0].locator.mime,
                                                          sheet="Energie", cell="B2")


def test_text_evidence():
    loc = Locator(blob_sha256="sha256:" + "c" * 64, mime="message/rfc822", page=1)
    c = extract_text_evidence("Unser Scope-1-Wert 2025 beträgt 1.200 t CO2e.", loc, lang="de")[0]
    assert c.unit == "tCO2e" and c.extracted_value == "1200" and c.needs_confirmation  # 无交叉验证的 "1.200"
    assert c.fact_key == "emissions.scope1.total"

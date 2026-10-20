from __future__ import annotations

from pathlib import Path

import pytest

from belegt_connectors import ConnectorError, UtilityBillPdfConnector, record_to_evidence, run_compliance
from belegt_connectors.connectors.utility_bills import _pages_text_fallback, belegt_io_available
from belegt_connectors.pdfgen import build_pdf

MODES = [False] + ([True] if belegt_io_available() else [])


@pytest.mark.parametrize("use_io", MODES)
def test_files_list(sample_root: Path, use_io: bool) -> None:
    c = UtilityBillPdfConnector(sample_root / "bills", use_belegt_io=use_io)
    env = c.call("docs.utility_bills.files.list")
    by = {r["fields"]["path"]: r["fields"] for r in env["records"]}
    assert by["2025/Stromrechnung_2025.pdf"]["commodity"] == "electricity"
    assert by["2025/Erdgasrechnung_2025.pdf"]["commodity"] == "natural_gas"
    assert by["2025/Fernwaerme_2025.pdf"]["commodity"] == "district_heat"
    assert by["2025/Fernwaerme_2025.pdf"]["pages"] == "2"
    assert by["2025/Stromrechnung_2025.pdf"]["billing_period"] == {"start": "2025-01-01", "end": "2025-12-31"}


@pytest.mark.parametrize("use_io", MODES)
def test_facts_with_locators(sample_root: Path, use_io: bool) -> None:
    c = UtilityBillPdfConnector(sample_root / "bills", use_belegt_io=use_io)
    env = c.call("docs.utility_bills.facts.query", {"fact_key": "activity.electricity.purchased"})
    vals = {(r["fields"]["extracted_value"], r["unit"]) for r in env["records"]}
    assert ("4820000", "kWh") in vals
    rec = next(r for r in env["records"] if r["fields"]["extracted_value"] == "4820000")
    assert rec["locator"]["page"] == 1 and rec["locator"]["blob_sha256"].startswith("sha256:")
    assert rec["period"] == {"start": "2025-01-01", "end": "2025-12-31"}
    if use_io:
        assert len(rec["locator"]["bbox"]) == 4
    else:
        assert float(rec["fields"]["confidence"]) <= 0.85  # 退回提取：不满足自动核验阈值
    heat = c.call("docs.utility_bills.facts.query", {"fact_key": "activity.district_heat.purchased"})
    assert [(r["fields"]["extracted_value"], r["locator"]["page"]) for r in heat["records"]] == [("320", 2)]
    ev = record_to_evidence(env, rec, fact_key="activity.electricity.purchased")
    assert ev["source_type"] == "document" and ev["status"] == "candidate"
    core = pytest.importorskip("belegt_core.models")
    core.Locator.model_validate(ev["locator"])


def test_page_get_and_errors(sample_root: Path) -> None:
    c = UtilityBillPdfConnector(sample_root / "bills", use_belegt_io=False)
    page = c.call("docs.utility_bills.pages.get", {"path": "2025/Fernwaerme_2025.pdf", "page": 2})
    assert "320 MWh" in page["records"][0]["fields"]["text"]
    with pytest.raises(ConnectorError):
        c.call("docs.utility_bills.pages.get", {"path": "2025/Fernwaerme_2025.pdf", "page": 9})
    with pytest.raises(ConnectorError):
        c.call("docs.utility_bills.pages.get", {"page": 1})  # 多个文件 → 必须指定


def test_fallback_text_parser_without_pypdf() -> None:
    data = build_pdf([["Wirkarbeit gesamt: 1.000 kWh"], ["Seite (2)"]])
    pages = _pages_text_fallback(data)
    assert "Wirkarbeit gesamt: 1.000 kWh" in pages[0]


def test_min_confidence_filter(sample_root: Path) -> None:
    c = UtilityBillPdfConnector(sample_root / "bills", use_belegt_io=False)
    assert c.call("docs.utility_bills.facts.query", {"min_confidence": 0.9})["records"] == []


@pytest.mark.parametrize("use_io", MODES)
def test_bills_compliance(sample_root: Path, use_io: bool) -> None:
    rep = run_compliance(UtilityBillPdfConnector(sample_root / "bills", use_belegt_io=use_io))
    assert rep.ok, rep.to_text()

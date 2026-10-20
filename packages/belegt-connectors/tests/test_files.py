from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from belegt_connectors import ConnectorError, FileFolderConnector, record_to_evidence, run_compliance


@pytest.fixture()
def files_root(sample_root: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "files"
    shutil.copytree(sample_root / "files", dst)
    return dst


def test_files_list_and_describe(files_root: Path) -> None:
    c = FileFolderConnector(files_root)
    env = c.call("file.folder.files.list")
    paths = [r["fields"]["path"] for r in env["records"]]
    assert sorted(paths) == ["Produktion/produktionsdaten_2025.xlsx", "zaehlerstaende_2025.csv"]
    for r in env["records"]:
        assert r["locator"]["blob_sha256"] == r["fields"]["blob_sha256"]
    d = c.call("file.folder.files.describe", {"path": "Produktion/produktionsdaten_2025.xlsx"})
    sheets = {r["fields"]["sheet"]: r["fields"] for r in d["records"]}
    assert set(sheets) == {"Produktion 2025", "Energie"}
    assert sheets["Produktion 2025"]["header"][:3] == ["Monat", "Artikel", "Menge"]


def test_excel_range_get_cell_locators(files_root: Path) -> None:
    c = FileFolderConnector(files_root)
    env = c.call("file.excel.range.get", {"path": "Produktion/produktionsdaten_2025.xlsx", "sheet": "Energie",
                                          "range": "A2:C4"})
    by_ref = {r["fields"]["ref"]: r for r in env["records"]}
    assert by_ref["B2"]["fields"]["value"] == "4820"
    assert by_ref["B4"]["fields"]["value"] == "25.6"
    assert by_ref["B2"]["locator"]["sheet"] == "Energie" and by_ref["B2"]["locator"]["cell"] == "B2"
    single = c.call("file.excel.range.get", {"path": "Produktion/produktionsdaten_2025.xlsx", "sheet": "Energie",
                                             "range": "B3"})
    assert [r["fields"]["value"] for r in single["records"]] == ["2150"]
    # 证据：document + cell
    ev = record_to_evidence(env, by_ref["B2"], fact_key="activity.electricity.purchased", unit="MWh")
    assert ev["source_type"] == "document" and ev["extracted_value"] == "4820"
    assert ev["locator"]["cell"] == "B2"
    # 通过 blob_sha256 定位同一文件
    blob = by_ref["B2"]["locator"]["blob_sha256"]
    again = c.call("file.excel.range.get", {"blob_sha256": blob, "sheet": "Energie", "range": "B2"})
    assert again["records"][0]["fields"]["value"] == "4820"


def test_rows_query_xlsx_and_csv(files_root: Path) -> None:
    c = FileFolderConnector(files_root)
    env = c.call("file.excel.rows.query", {"path": "Produktion/produktionsdaten_2025.xlsx",
                                           "sheet": "Produktion 2025", "where": {"Monat": "2025-03"}})
    assert env["total"] == 1
    rec = env["records"][0]
    assert rec["fields"]["Menge"] == "41500" and rec["locator"]["cell"] == "A4:E4"
    csv_env = c.call("file.csv.rows.query", {"path": "zaehlerstaende_2025.csv", "page_size": 5})
    assert csv_env["total"] == 12 and len(csv_env["records"]) == 5 and csv_env["next_cursor"]
    assert csv_env["records"][0]["fields"]["Verbrauch"] == "420000"
    assert csv_env["records"][0]["locator"]["cell"] == "A2:D2"
    assert "sheet" not in csv_env["records"][0]["locator"]


def test_incremental_files(files_root: Path) -> None:
    c = FileFolderConnector(files_root)
    recs, token = c.sync("file.folder.files.list")
    assert len(recs) == 2
    recs2, _ = c.sync("file.folder.files.list", sync_token=token)
    assert recs2 == []
    p = files_root / "neu.csv"
    p.write_text("a;b\n1;2\n", encoding="utf-8")
    st = (files_root / "zaehlerstaende_2025.csv").stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    recs3, _ = c.sync("file.folder.files.list", sync_token=token)
    assert [r["record_key"] for r in recs3] == ["neu.csv"]


def test_path_escape_denied(files_root: Path) -> None:
    c = FileFolderConnector(files_root)
    with pytest.raises(ConnectorError) as ei:
        c.call("file.csv.rows.query", {"path": "../../etc/passwd"})
    assert ei.value.code == "CONNECTOR_PATH_DENIED"
    with pytest.raises(ConnectorError):
        c.call("file.excel.range.get", {"path": "zaehlerstaende_2025.csv", "sheet": "x", "range": "A1"})


def test_files_compliance(files_root: Path) -> None:
    rep = run_compliance(FileFolderConnector(files_root))
    assert rep.ok, rep.to_text()

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from belegt_connectors import ConnectorError, MockErpConnector, record_to_evidence, run_compliance
from belegt_connectors.connectors.erp_mock import default_data_dir
from belegt_connectors.samples import build_erp_sample


def test_shipped_sample_matches_generator(tmp_path: Path) -> None:
    build_erp_sample(tmp_path)
    shipped = default_data_dir()
    for p in sorted(tmp_path.rglob("*")):
        if p.is_file():
            assert (shipped / p.relative_to(tmp_path)).read_bytes() == p.read_bytes(), p


def test_purchase_invoices_energy_totals() -> None:
    c = MockErpConnector()
    recs = [r for env in c.iter_all("erp.sap_b1.purchase_invoices.query",
                                    {"date_from": "2025-01-01", "date_to": "2026-01-31", "item_code": "E-STROM"})
            for r in env["records"]]
    in_2025 = [r for r in recs if r["period"]["start"].startswith("2025")]
    assert len(in_2025) == 12
    assert sum(Decimal(r["fields"]["quantity"]) for r in in_2025) == Decimal("4820000")
    assert all(r["unit"] == "kWh" for r in in_2025)
    assert in_2025[-1]["period"] == {"start": "2025-12-01", "end": "2025-12-31"}


def test_steel_imports_cbam_fields() -> None:
    c = MockErpConnector()
    env = c.call("erp.sap_b1.purchase_invoices.query", {"date_from": "2025-01-01", "date_to": "2025-12-31",
                                                        "item_group": "STAHL"})
    assert env["attested"] is True
    qty = sum(Decimal(r["fields"]["quantity"]) for r in env["records"])
    assert qty == Decimal("420.5") + Decimal("380.25") + Decimal("455") + Decimal("398.75") + Decimal("96.4")
    r = env["records"][0]
    assert r["fields"]["cn_code"] == "72085120" and r["fields"]["country_of_origin"] == "TR" and r["unit"] == "t"
    diesel = c.call("erp.sap_b1.purchase_invoices.query", {"date_from": "2025-01-01", "date_to": "2025-12-31",
                                                           "item_code": "F-DIESEL"})
    assert {x["unit"] for x in diesel["records"]} == {"L"}


def test_items_get_spec_shape() -> None:
    c = MockErpConnector()
    rec = c.call("erp.sap_b1.items.get", {"item_code": "AL-INGOT"})["records"][0]
    assert {"item_code", "description", "customs_group", "weight_kg"} <= set(rec["fields"])
    assert rec["fields"]["customs_group"] == "76011000" and rec["fields"]["weight_kg"] == "1"
    with pytest.raises(ConnectorError) as ei:
        c.call("erp.sap_b1.items.get", {"item_code": "NOPE"})
    assert ei.value.code == "CONNECTOR_NOT_FOUND"
    assert len(c.call("erp.sap_b1.items.list", {"item_group": "ALUMINIUM"})["records"]) == 2
    sup = c.call("erp.sap_b1.business_partners.list", {"card_type": "cSupplier"})
    assert len(sup["records"]) == 7


def test_datev_bookings() -> None:
    c = MockErpConnector()
    env = c.call("erp.datev.accounts_bookings.query", {"account_from": "6325", "account_to": "6325",
                                                       "fiscal_year": 2025})
    assert env["total"] == 12
    assert sum(Decimal(r["fields"]["quantity"]) for r in env["records"]) == Decimal("4820000")
    dec = [r for r in env["records"] if r["period"] and r["period"]["start"] == "2025-12-01"][0]
    assert dec["fields"]["booking_date"] == "2025-12-31" and dec["unit"] == "kWh"
    steel = c.call("erp.datev.accounts_bookings.query", {"account_from": "5425", "account_to": "5425",
                                                         "fiscal_year": 2025})
    assert {r["unit"] for r in steel["records"]} == {"t", "kg"}
    assert c.call("erp.datev.accounts_bookings.query", {"account_from": "6000", "account_to": "6999",
                                                        "fiscal_year": 2024})["records"] == []
    accts = {r["fields"]["account"] for r in c.call("erp.datev.accounts.list")["records"]}
    assert {"6325", "6326", "5425"} <= accts


def test_incremental_invoice_sync(tmp_path: Path) -> None:
    data = tmp_path / "erp"
    shutil.copytree(default_data_dir(), data)
    args = {"date_from": "2025-01-01", "date_to": "2026-12-31"}
    c = MockErpConnector(data)
    recs, token = c.sync("erp.sap_b1.purchase_invoices.query", args)
    assert len(recs) == 39  # 12 电 + 12 气 + 4 热 + 2 柴油 + 5 钢 + 3 铝 + 1（2026-01 电）
    p = data / "sap_b1" / "purchase_invoices.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["value"].append({**doc["value"][-1], "DocEntry": 9999, "DocNum": 269999, "DocDate": "2026-03-05",
                         "UpdateDate": "2026-03-10T08:00:00Z", "U_ServiceFrom": "2026-02-01",
                         "U_ServiceTo": "2026-02-28"})
    p.write_text(json.dumps(doc), encoding="utf-8")
    c2 = MockErpConnector(data)
    new, token2 = c2.sync("erp.sap_b1.purchase_invoices.query", args, token)
    assert [r["record_key"] for r in new] == ["OPCH:9999:0"] and token2 != token


def test_erp_record_to_evidence_valid_for_core() -> None:
    c = MockErpConnector()
    env = c.call("erp.sap_b1.purchase_invoices.query", {"date_from": "2025-02-01", "date_to": "2025-02-28",
                                                        "item_code": "E-STROM"})
    rec = env["records"][0]
    ev = record_to_evidence(env, rec, fact_key="activity.electricity.purchased")
    assert ev["source_type"] == "erp" and ev["extracted_value"] == "420000" and ev["unit"] == "kWh"
    assert ev["locator"]["connector"]["record_key"] == "OPCH:5001:0"
    assert ev["period"] == {"start": "2025-01-01", "end": "2025-01-31"} and ev["valid_from"] == "2025-01-01"
    core = pytest.importorskip("belegt_core.models")
    core.Locator.model_validate(ev["locator"])
    obj = {**ev, "id": "ev_01JD8Z1A2B3C4D5E6F7G8H9J0K", "org_id": "org_01JD8Z1A2B3C4D5E6F7G8H9J0K",
           "created_by": "agent:evidence-miner", "trust_zone": "trusted"}
    core.Evidence.model_validate(obj)


def test_erp_compliance() -> None:
    rep = run_compliance(MockErpConnector())
    assert rep.ok, rep.to_text()
    m = MockErpConnector().manifest()
    assert m.attested is True and all(s.endswith(".read") for s in m.scopes)
    assert all(t["annotations"]["readOnlyHint"] for t in m.tools)

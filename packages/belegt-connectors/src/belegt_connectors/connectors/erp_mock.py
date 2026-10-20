# Spec: 04 §6.1 示例表（erp.sap_b1.purchase_invoices.query / erp.sap_b1.items.get / erp.datev.accounts_bookings.query）；
#       FR-203；03 §3.5 Locator.connector、§3.1 EV-AUTO-ERP-ATTEST（manifest attested=true）
"""模拟 ERP 连接器：SAP Business One（Service Layer 风格）与 DATEV（EXTF 导出文件）的能源与采购数据。

数据默认取随包样例 `data/erp_sample/`（`samples.build_erp_sample` 生成，虚构企业），也可指向同结构的目录：
- `sap_b1/{business_partners,item_groups,items,purchase_invoices}.json`（`{"value": [...]}`，Service Layer 形状）
- `datev/EXTF_Buchungsstapel_<YYYY>.csv`（cp1252、`;` 分隔、首行 EXTF 头）、`datev/Kontenbeschriftungen_*.csv`

真实部署时把同名工具接到 SAP B1 Service Layer（`/b1s/v1/PurchaseInvoices`，只读技术用户）或 DATEV 数据服务；
工具名、输入 schema 与 envelope 不变，evidence-miner 无需改动。
"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from ..base import BaseConnector, CallContext
from ..spec import CURSOR_PROPS, SYNC_PROPS, AuthSpec, ConnectorError, object_schema

#: SAP UoM → 02 §3.1 单位代码
UOM_MAP = {"kWh": "kWh", "MWh": "MWh", "Ltr": "L", "L": "L", "t": "t", "kg": "kg", "Stk": "1", "m3": "m3"}
DATEV_UNIT_MAP = {"kWh": "kWh", "MWh": "MWh", "Liter": "L", "Tonnen": "t", "kg": "kg"}
DATE = {"type": "string", "format": "date"}


def default_data_dir() -> Path:
    return Path(str(resources.files("belegt_connectors") / "data" / "erp_sample"))


def _de_dec(s: str) -> str:
    s = (s or "").strip().replace(".", "").replace(",", ".")
    return f"{Decimal(s).normalize():f}" if s else "0"


class MockErpConnector(BaseConnector):
    kind = "erp-mock"
    category = "erp"
    system = "sap_b1"
    title = "Mock ERP (SAP Business One + DATEV), read-only"
    source_system = "sap_b1+datev (mock)"
    attested = True  # 04 §2.1 EV-AUTO-ERP-ATTEST：manifest 声明 attested=true

    def __init__(self, data_dir: str | Path | None = None, connector_id: str | None = None, **kw: Any) -> None:
        kw.setdefault("auth", AuthSpec(mode="oauth2_client_credentials", secret_ref="vault:erp/sap-b1",
                                       notes="mock：不校验令牌；真实 SAP B1 Service Layer 用只读技术用户 + 会话令牌"))
        super().__init__(connector_id or "conn:erp:sap_b1-mock", **kw)
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self._load()
        self.register(
            "erp.sap_b1.purchase_invoices.query",
            "采购发票行（A/P Invoice，OPCH）：能源采购（电、气、热、燃料）与钢铝进口量；按日期、供应商、物料组过滤。"
            "每行一条记录，unit 为 02 单位代码，period 为服务期（无则为单据日）。",
            object_schema({"vendor_code": {"type": "string"}, "item_group": {"type": "string"},
                           "item_code": {"type": "string"}, "date_from": DATE, "date_to": DATE,
                           **CURSOR_PROPS, **SYNC_PROPS}, ["date_from", "date_to"]),
            self._invoices, paginated=True, incremental=True, evidence_source_type="erp")
        self.register(
            "erp.sap_b1.items.get", "物料主数据：`{item_code}` → `{item_code, description, customs_group, weight_kg}`",
            object_schema({"item_code": {"type": "string"}}, ["item_code"]), self._item_get,
            evidence_source_type="erp")
        self.register(
            "erp.sap_b1.items.list", "物料清单（可按物料组过滤）",
            object_schema({"item_group": {"type": "string"}, **CURSOR_PROPS}), self._items_list, paginated=True)
        self.register(
            "erp.sap_b1.business_partners.list", "业务伙伴（供应商/客户）",
            object_schema({"card_type": {"enum": ["cSupplier", "cCustomer"]}, **CURSOR_PROPS}),
            self._partners, paginated=True)
        self.register(
            "erp.datev.accounts_bookings.query",
            "DATEV 记账（EXTF 导出）：按科目区间与会计年度查询能源费用科目、燃料账户；数量取自 Zusatzinformation。",
            object_schema({"account_from": {"type": "string", "pattern": "^[0-9]{4,8}$"},
                           "account_to": {"type": "string", "pattern": "^[0-9]{4,8}$"},
                           "fiscal_year": {"type": "integer", "minimum": 2000, "maximum": 2100},
                           **CURSOR_PROPS}, ["account_from", "account_to", "fiscal_year"]),
            self._bookings, paginated=True, evidence_source_type="erp")
        self.register(
            "erp.datev.accounts.list", "DATEV 科目表（Kontenbeschriftungen）",
            object_schema({**CURSOR_PROPS}), self._accounts, paginated=True)

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        return {
            "erp.sap_b1.purchase_invoices.query": {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            "erp.sap_b1.items.get": {"item_code": "E-STROM"},
            "erp.sap_b1.items.list": {},
            "erp.sap_b1.business_partners.list": {},
            "erp.datev.accounts_bookings.query": {"account_from": "6300", "account_to": "6599", "fiscal_year": 2025},
            "erp.datev.accounts.list": {},
        }

    # —— 数据 ——
    def _json(self, name: str) -> list[dict[str, Any]]:
        p = self.data_dir / "sap_b1" / f"{name}.json"
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))["value"]

    def _load(self) -> None:
        self.partners = self._json("business_partners")
        self.groups = {g["Number"]: g["GroupName"] for g in self._json("item_groups")}
        self.items = {i["ItemCode"]: i for i in self._json("items")}
        self.invoices = self._json("purchase_invoices")
        self.bookings: dict[int, list[dict[str, Any]]] = {}
        ddir = self.data_dir / "datev"
        for p in sorted(ddir.glob("EXTF_Buchungsstapel_*.csv")) if ddir.exists() else []:
            text = p.read_bytes().decode("cp1252")
            lines = text.splitlines()
            head = lines[0].split(";")
            fy = int(head[12][:4]) if len(head) > 12 and head[12][:4].isdigit() else int(p.stem[-4:])
            rows = list(csv.DictReader(io.StringIO("\n".join(lines[1:])), delimiter=";"))
            self.bookings.setdefault(fy, []).extend(rows)
        self.accounts: list[dict[str, str]] = []
        for p in sorted(ddir.glob("Kontenbeschriftungen_*.csv")) if ddir.exists() else []:
            self.accounts += list(csv.DictReader(io.StringIO(p.read_bytes().decode("cp1252")), delimiter=";"))

    # —— handlers ——
    def _invoices(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        df, dt = args["date_from"], args["date_to"]
        if df > dt:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "date_from > date_to")
        out = []
        for inv in self.invoices:
            if not (df <= inv["DocDate"] <= dt):
                continue
            if args.get("vendor_code") and inv["CardCode"] != args["vendor_code"]:
                continue
            for ln in inv["DocumentLines"]:
                item = self.items.get(ln["ItemCode"], {})
                group = self.groups.get(item.get("ItemsGroupCode"), "")
                if args.get("item_group") and group != args["item_group"]:
                    continue
                if args.get("item_code") and ln["ItemCode"] != args["item_code"]:
                    continue
                if inv.get("U_ServiceFrom") and inv.get("U_ServiceTo"):
                    period = {"start": inv["U_ServiceFrom"], "end": inv["U_ServiceTo"]}
                else:
                    period = {"start": inv["DocDate"], "end": inv["DocDate"]}
                fields = {
                    "doc_entry": inv["DocEntry"], "doc_num": inv["DocNum"], "vendor_code": inv["CardCode"],
                    "vendor_name": inv["CardName"], "doc_date": inv["DocDate"], "item_code": ln["ItemCode"],
                    "item_description": ln["ItemDescription"], "item_group": group,
                    "quantity": Decimal(ln["Quantity"]), "uom": ln["UnitMsr"],
                    "line_total": Decimal(ln["LineTotal"]), "currency": inv["DocCurrency"],
                    "cn_code": item.get("CustomsGroupCode"), "country_of_origin": ln.get("CountryOfOrigin"),
                    "cbam_installation": ln.get("U_CBAM_Installation"), "energy_basis": item.get("U_EnergyBasis"),
                }
                out.append({
                    "record_key": f"OPCH:{inv['DocEntry']}:{ln['LineNum']}",
                    "fields": {k: v for k, v in fields.items() if v is not None},
                    "unit": UOM_MAP.get(ln["UnitMsr"], ln["UnitMsr"]),
                    "period": period,
                    "_wm": f"{inv['UpdateDate']}|{inv['DocEntry']:08d}",
                })
        return out

    def _item_get(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        item = self.items.get(args["item_code"])
        if item is None:
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"item {args['item_code']!r} not found")
        w = item.get("SalesUnitWeight")
        return [{"record_key": f"OITM:{item['ItemCode']}",
                 "fields": {"item_code": item["ItemCode"], "description": item["ItemName"],
                            "customs_group": item.get("CustomsGroupCode"),
                            "weight_kg": Decimal(w) if w is not None else None,
                            "item_group": self.groups.get(item["ItemsGroupCode"]),
                            "inventory_uom": UOM_MAP.get(item["InventoryUOM"], item["InventoryUOM"])},
                 "unit": "kg" if w is not None else None}]

    def _items_list(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        out = []
        for code in sorted(self.items):
            item = self.items[code]
            group = self.groups.get(item["ItemsGroupCode"])
            if args.get("item_group") and group != args["item_group"]:
                continue
            out.append({"record_key": f"OITM:{code}",
                        "fields": {"item_code": code, "description": item["ItemName"], "item_group": group,
                                   "customs_group": item.get("CustomsGroupCode")}})
        return out

    def _partners(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        return [{"record_key": f"OCRD:{p['CardCode']}",
                 "fields": {"card_code": p["CardCode"], "card_name": p["CardName"], "card_type": p["CardType"],
                            "country": p["Country"]}}
                for p in self.partners if not args.get("card_type") or p["CardType"] == args["card_type"]]

    def _bookings(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        fy = int(args["fiscal_year"])
        a0, a1 = int(args["account_from"]), int(args["account_to"])
        out = []
        for i, r in enumerate(self.bookings.get(fy, []), start=1):
            acct = int(r["Konto"])
            if not (a0 <= acct <= a1):
                continue
            ddmm = r["Belegdatum"].zfill(4)
            day, month = int(ddmm[:2]), int(ddmm[2:])
            booking_date = f"{fy:04d}-{month:02d}-{day:02d}"  # EXTF Belegdatum 为 DDMM，年份取批次会计年度
            qty_kind = (r.get("Zusatzinformation - Art 1") or "").replace("Menge ", "").strip()
            qty = r.get("Zusatzinformation- Inhalt 1") or ""
            unit = DATEV_UNIT_MAP.get(qty_kind)
            period = None
            sp = r.get("Zusatzinformation- Inhalt 2") or ""
            if "/" in sp and sp.strip("/"):
                s, e = sp.split("/", 1)
                if s and e:
                    period = {"start": s, "end": e}
            fields: dict[str, Any] = {
                "amount_eur": Decimal(_de_dec(r["Umsatz (ohne Soll/Haben-Kz)"])),
                "debit_credit": r["Soll/Haben-Kennzeichen"], "account": r["Konto"],
                "contra_account": r["Gegenkonto (ohne BU-Schlüssel)"], "tax_key": r["BU-Schlüssel"],
                "booking_date": booking_date, "document_no": r["Belegfeld 1"], "booking_text": r["Buchungstext"],
            }
            if qty:
                fields["quantity"] = Decimal(_de_dec(qty))
            out.append({"record_key": f"DATEV:{fy}:{r['Belegfeld 1']}:{i}", "fields": fields, "unit": unit,
                        "period": period})
        return out

    def _accounts(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        return [{"record_key": f"SKR:{a['Konto']}", "fields": {"account": a["Konto"], "label": a["Beschriftung"],
                                                               "chart": a.get("Kontenrahmen")}}
                for a in self.accounts]

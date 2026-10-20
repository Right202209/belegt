# Spec: 11 §2.2(3)（合成单据：值由生成器写入）、§2.4 LBL-EVID / LBL-LOC / LBL-VALID 及边界案例
#       （摘要页与明细页重复 → 一个事实两个可接受定位；合计与明细冲突 → 都标并标 internal_inconsistency；
#        “三年有效” → 推算并标 derived；无到期日 → null）
"""证据提取黄金样例：确定性生成电费单、燃气单、供热单、水费单与证书 PDF，同时写出事实与定位金标准。

定位金标准的 bbox 采用生成器的行几何估算（`belegt_connectors.pdfgen.line_bbox`：x1 = x0 + 0.5·字号·字符数），
与 belegt-io 的估算口径一致；判定用 IoU ≥ 0.5（M-EVI-02），对字宽估算误差有容忍。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from belegt_connectors.pdfgen import build_pdf, line_bbox

Y = 2025
PERIOD_Y = {"start": f"{Y}-01-01", "end": f"{Y}-12-31"}


@dataclass
class DocSpec:
    doc_id: str
    kind: str
    lang: str
    pages: list[list[Any]]  # 行：str 或 (str, fact_dict | None, flags)
    validity: dict[str, Any] | None = None
    notes: str | None = None
    distractors: list[str] = field(default_factory=list)


def F(text: str, fact_key: str, value: str, unit: str, period: dict[str, str] | None = None,
      flags: list[str] | None = None, fact_id: str | None = None) -> tuple[str, dict[str, Any]]:
    return (text, {"fact_key": fact_key, "extracted_value": value, "unit": unit, "period": period or PERIOD_Y,
                   "flags": flags or [], "fact_id": fact_id})


def _de(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _en(n: int) -> str:
    return f"{n:,}"


SUPPLIERS_EL = ["Stadtwerke Musterstadt GmbH", "Energieversorgung Nordheim AG", "Rheinstrom Vertrieb GmbH",
                "Alpen Energie GmbH", "Hansa Power Ltd.", "Westfalen Netz & Energie GmbH"]
EL = "activity.electricity.purchased"
GAS = "activity.natural_gas.consumed"
HEAT = "activity.district_heat.purchased"
WATER = "activity.water.withdrawn"


def doc_specs() -> list[DocSpec]:
    docs: list[DocSpec] = []
    # —— 电费单 12 份 ——
    vals = [4820000, 1385400, 2716350, 912000, 3456789, 1250000, 6034500, 784210, 2000000, 1540000, 2345678, 3102000]
    for i, v in enumerate(vals):
        sup = SUPPLIERS_EL[i % len(SUPPLIERS_EL)]
        did = f"EL-{i + 1:02d}"
        if i in (0, 1, 2, 3):  # 德式单页
            lines = [sup, f"Jahresabrechnung Strom {Y}", "Kunde: Musterteile GmbH, Werk Nord",
                     f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                     F(f"Wirkarbeit gesamt: {_de(v)} kWh", EL, str(v), "kWh"),
                     f"Rechnungsbetrag: {_de(v // 5)},00 EUR"]
            docs.append(DocSpec(did, "electricity_bill", "de", [lines], distractors=[str(v // 5)]))
        elif i in (4, 5):  # 摘要页 + 明细页重复（一个事实、两个可接受定位）
            p1 = [sup, f"Stromrechnung {Y} - Zusammenfassung", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                  F(f"Gesamtverbrauch: {_de(v)} kWh", EL, str(v), "kWh", fact_id="total")]
            p2 = ["Verbrauchsdetails", "Zaehler 1ESY1160234567",
                  F(f"Verbrauch Strom gesamt: {_de(v)} kWh", EL, str(v), "kWh", fact_id="total"),
                  f"Arbeitspreis netto: {_de(v // 4)},00 EUR"]
            docs.append(DocSpec(did, "electricity_bill", "de", [p1, p2], distractors=[str(v // 4)],
                                notes="summary+detail duplicate"))
        elif i in (6, 7):  # 英文格式
            lines = [sup, f"Annual electricity statement {Y}", f"Billing period {Y}-01-01 to {Y}-12-31",
                     F(f"Total electricity consumption: {_en(v)} kWh", EL, str(v), "kWh"),
                     f"Amount due: {_en(v // 6)}.00 EUR"]
            docs.append(DocSpec(did, "electricity_bill", "en", [lines], distractors=[str(v // 6)]))
        elif i == 8:  # 合计与明细不一致（internal_inconsistency：两者都标）
            lines = [sup, f"Jahresabrechnung Strom {Y}", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                     F("Verbrauch Hochtarif: 1.200.000 kWh", EL, "1200000", "kWh", flags=["internal_inconsistency"]),
                     F("Verbrauch Niedertarif: 750.000 kWh", EL, "750000", "kWh", flags=["internal_inconsistency"]),
                     F(f"Summe Wirkarbeit: {_de(v)} kWh", EL, str(v), "kWh", flags=["internal_inconsistency"])]
            docs.append(DocSpec(did, "electricity_bill", "de", [lines], notes="parts 1.95 GWh != total 2.0 GWh"))
        elif i == 9:  # 季度明细（事实期间为季度）+ 年度合计
            q = [380000, 360000, 390000, 410000]
            qp = [(f"{Y}-01-01", f"{Y}-03-31"), (f"{Y}-04-01", f"{Y}-06-30"), (f"{Y}-07-01", f"{Y}-09-30"),
                  (f"{Y}-10-01", f"{Y}-12-31")]
            lines = [sup, f"Jahresabrechnung Strom {Y}", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}"]
            for k, (qv, (s, e)) in enumerate(zip(q, qp), start=1):
                lines.append(F(f"Verbrauch Q{k}/{Y}: {_de(qv)} kWh", EL, str(qv), "kWh", {"start": s, "end": e}))
            lines.append(F(f"Wirkarbeit gesamt: {_de(sum(q))} kWh", EL, str(sum(q)), "kWh"))
            docs.append(DocSpec(did, "electricity_bill", "de", [lines], notes="quarterly facts"))
        elif i == 10:  # MWh 小数 + 干扰数字（kW、EUR/kWh、电表号）
            lines = [sup, f"Jahresabrechnung Strom {Y}", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                     "Zaehlernummer 1ESY1160998877", "Hoechstleistung: 1.250 kW",
                     F("Stromverbrauch gesamt: 2.345,678 MWh", EL, "2345.678", "MWh"),
                     "Arbeitspreis: 0,1873 EUR/kWh"]
            docs.append(DocSpec(did, "electricity_bill", "de", [lines], distractors=["1250", "0.1873"]))
        else:  # 财年账单期
            lines = [sup, "Jahresabrechnung Strom 2024/2025", "Abrechnungszeitraum 01.10.2024 - 30.09.2025",
                     F(f"Wirkarbeit gesamt: {_de(v)} kWh", EL, str(v), "kWh",
                       {"start": "2024-10-01", "end": "2025-09-30"})]
            docs.append(DocSpec(did, "electricity_bill", "de", [lines], notes="fiscal-year period"))
    # —— 燃气单 8 份 ——
    gas = [(2150000, 197531), (980500, 90012), (3420000, 314200), (1210000, 111150), (560000, 51420),
           (2780000, 255300), (1675000, 153900), (4012000, 368500)]
    for i, (kwh, m3) in enumerate(gas):
        did = f"GAS-{i + 1:02d}"
        if i % 2 == 0:
            lines = ["Gasversorgung Sued AG", f"Jahresabrechnung Erdgas {Y}", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                     F(f"Erdgas Verbrauch (Brennwert Hs): {_de(kwh)} kWh", GAS, str(kwh), "kWh"),
                     F(f"Erdgas Volumen: {_de(m3)} m3", GAS, str(m3), "m3"),
                     "Zustandszahl 0,9531",
                     f"Rechnungsbetrag: {_de(kwh // 12)},00 EUR"]
            docs.append(DocSpec(did, "gas_bill", "de", [lines], distractors=["0.9531", str(kwh // 12)]))
        else:
            lines = ["Nordgas Supply plc", f"Natural gas annual statement {Y}", f"Billing period {Y}-01-01 to {Y}-12-31",
                     F(f"Natural gas consumption: {_en(kwh)} kWh", GAS, str(kwh), "kWh"),
                     f"Amount due: {_en(kwh // 11)}.00 EUR"]
            docs.append(DocSpec(did, "gas_bill", "en", [lines], distractors=[str(kwh // 11)]))
    # —— 供热单 4 份 ——
    for i, mwh in enumerate([320, 1150, 87, 640]):
        did = f"HEAT-{i + 1:02d}"
        p1 = ["Fernwaerme Musterstadt GmbH", f"Abrechnung Fernwaerme {Y}", f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
              "Seite 1 von 2"]
        p2 = ["Verbrauchsuebersicht", F(f"Fernwaerme Bezug gesamt: {_de(mwh)} MWh", HEAT, str(mwh), "MWh"),
              f"Rechnungsbetrag: {_de(mwh * 95)},00 EUR"]
        docs.append(DocSpec(did, "heat_bill", "de", [p1, p2], distractors=[str(mwh * 95)]))
    # —— 水费单 2 份 ——
    for i, m3 in enumerate([9120, 15240]):
        did = f"WATER-{i + 1:02d}"
        lines = ["Wasserverband Musterstadt", f"Jahresabrechnung Trinkwasser {Y}",
                 f"Abrechnungszeitraum 01.01.{Y} - 31.12.{Y}",
                 F(f"Frischwasser Verbrauch: {_de(m3)} m3", WATER, str(m3), "m3"),
                 f"Rechnungsbetrag: {_de(m3 * 3)},00 EUR"]
        docs.append(DocSpec(did, "water_bill", "de", [lines], distractors=[str(m3 * 3)]))
    # —— 证书 6 份（LBL-VALID） ——
    certs = [
        ("CERT-01", "de", ["TUEV Zertifizierung GmbH", "Zertifikat", "Managementsystem nach ISO 14001:2015",
                           "Musterteile GmbH, Werk Nord", "Gueltig von 01.04.2025 bis 31.03.2028"],
         {"valid_from": "2025-04-01", "valid_until": "2028-03-31", "derived": False}),
        ("CERT-02", "en", ["Global Certification Ltd.", "Certificate of Registration", "ISO 50001:2018",
                           "Musterteile GmbH", "Issue date: 2024-06-15", "Valid until: 2027-06-14"],
         {"valid_from": "2024-06-15", "valid_until": "2027-06-14", "derived": False}),
        ("CERT-03", "de", ["DEKRA Certification", "Zertifikat ISO 45001:2018", "Musterteile GmbH",
                           "Ausstellungsdatum: 15.06.2025", "Laufzeit: drei Jahre ab Ausstellungsdatum"],
         {"valid_from": "2025-06-15", "valid_until": "2028-06-14", "derived": True}),
        ("CERT-04", "en", ["IATF Oversight", "IATF 16949:2016 Certificate", "Musterteile GmbH",
                           "Certificate valid from 2024-11-01", "Expiry date: 2027-10-31"],
         {"valid_from": "2024-11-01", "valid_until": "2027-10-31", "derived": False}),
        ("CERT-05", "de", ["Umweltgutachter Dr. Beispiel", "Bestaetigung EMAS-Registrierung",
                           "Musterteile GmbH", "Registriert seit 12.03.2019"],
         {"valid_from": "2019-03-12", "valid_until": None, "derived": False}),
        ("CERT-06", "de", ["TUEV Zertifizierung GmbH", "Zertifikat ISO 14001:2015", "Musterteile GmbH, Werk Sued",
                           "Ausgestellt am 01.09.2025", "Gueltig bis: 31.08.2028"],
         {"valid_from": "2025-09-01", "valid_until": "2028-08-31", "derived": False}),
    ]
    for did, lang, lines, validity in certs:
        docs.append(DocSpec(did, "certificate", lang, [lines], validity=validity))
    return docs


def render(spec: DocSpec, canary: str) -> tuple[bytes, list[dict[str, Any]]]:
    """返回 (pdf_bytes, facts)。facts 已合并同一 fact_id 的多个定位。"""
    pages_text: list[list[str]] = []
    facts: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for pno, page in enumerate(spec.pages, start=1):
        texts = []
        for idx, line in enumerate(page):
            if isinstance(line, tuple):
                text, fact = line
                loc = {"page": pno, "bbox": list(line_bbox(idx, text))}
                fid = fact.get("fact_id")
                if fid and fid in by_id:
                    by_id[fid]["locators"].append(loc)
                else:
                    f = {k: v for k, v in fact.items() if k != "fact_id"}
                    f["locators"] = [loc]
                    facts.append(f)
                    if fid:
                        by_id[fid] = f
                texts.append(text)
            else:
                texts.append(line)
        pages_text.append(texts)
    data = build_pdf(pages_text, info={"Subject": canary, "Producer": "belegt-bench-synth"})
    return data, facts

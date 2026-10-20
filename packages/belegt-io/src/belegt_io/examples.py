# Spec: 01 §2.1（demo 旅程）；05 §4.6、§6.9
"""生成 demo / 测试用示例文件：`python -m belegt_io.examples <out_dir> [<out_dir2> ...]`。

示例数值为虚构演示数据（Musterfirma），不是任何真实企业的排放数据。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .models import Period
from .workbooks import (
    build_cbam_template, build_cmrt_template, build_csv_questionnaire, build_oem_questionnaire_de, build_sample_pdf,
)

CREATED = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)


def sample_pcf_input():
    from .export.pact import DQI, PcfInput

    return PcfInput(
        id="3f2b6c1e-7d4a-4e8b-9a10-2c5d7e9f1a3b",
        created=CREATED,
        company_name="Musterfirma Präzisionsteile GmbH",
        company_ids=["urn:pact:company:customcode:buyer-assigned:MUSTER-0042"],
        product_description="Kaltgewalztes Stahlblech, verzinkt, 1,5 mm",
        product_ids=["urn:pact:product-id:customcode:vendor-assigned:BLECH-150-ZN"],
        product_name_company="Stahlblech DX51D+Z 1,5 mm",
        declared_unit="kg",
        declared_unit_amount=Decimal("1"),
        product_mass_per_declared_unit=Decimal("1"),
        reference_period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31)),
        validity_period=Period(start=date(2026, 1, 1), end=date(2027, 12, 31)),
        geography_country="DE",
        pcf_excluding_biogenic_uptake=Decimal("2.4137"),
        pcf_including_biogenic_uptake=Decimal("2.4137"),
        fossil_ghg_emissions=Decimal("2.4137"),
        fossil_carbon_content=Decimal("0"),
        biogenic_carbon_content=Decimal("0"),
        packaging_emissions_included=False,
        exempted_emissions_percent=Decimal("1.2"),
        exempted_emissions_description="Hilfsstoffe < 1 % Masse",
        allocation_rules_description="Physikalische Allokation nach Maschinenstunden (template text)",
        primary_data_share=Decimal("38.5"),
        dqi=DQI(technological=Decimal("2.1"), geographical=Decimal("1.6"), temporal=Decimal("1.4")),
        source_refs={"pcf_excluding_biogenic_uptake": "calc_01JD9000000000000000000000#v1"},
    )


def sample_cbam():
    from .export.cbam import CbamCommunication, CbamGood, CbamInstallation, CbamProcess

    return CbamCommunication(
        installation=CbamInstallation(
            installation_id="CN-TJ-STEEL-0815", name="Tianjin Cold Rolling Plant No. 2", operator_name="Demo Steel Co., Ltd.",
            country="CN", address="Binhai New Area, Tianjin", un_locode="CNTSN",
            reporting_period=Period(start=date(2026, 1, 1), end=date(2026, 12, 31)), contact_email="cbam@demo-steel.example",
        ),
        goods=[
            CbamGood(cn_code="72091610", description="Cold-rolled flat steel, 1–3 mm", production_route="bf_bof",
                     see_direct=Decimal("2.0412345"), see_indirect=Decimal("0.3120004"),
                     specific_electricity_consumption=Decimal("0.5123"), determination_method="actual"),
            CbamGood(cn_code="72104900", description="Galvanised flat steel", production_route="bf_bof",
                     see_direct=Decimal("2.1123"), see_indirect=Decimal("0.3305"),
                     specific_electricity_consumption=Decimal("0.587"), determination_method="actual"),
        ],
        processes=[CbamProcess(process="Cold rolling", production_route="bf_bof", cn_code="72091610",
                               activity_level_t=Decimal("182500"))],
    )


def sample_cmrt():
    from .export.cmrt import CmrtDeclaration, CmrtSmelter

    return CmrtDeclaration(
        company_name="Musterfirma Präzisionsteile GmbH", authorized_representative="Petra Beispiel",
        contact_email="compliance@musterfirma.example", date_of_completion=date(2026, 10, 19),
        metal_answers={"Q1": {"Tantalum": "No", "Tin": "Yes", "Gold": "No", "Tungsten": "No"},
                       "Q2": {"Tantalum": "No", "Tin": "Yes", "Gold": "No", "Tungsten": "No"}},
        company_answers={"A": "Yes", "B": "https://musterfirma.example/policy", "C": "Yes"},
        smelters=[CmrtSmelter(metal="Tin", smelter_name="Demo Tin Smelter", smelter_country="Indonesia",
                              smelter_id="CID000001")],
    )


def generate(out_dir: str | Path) -> list[Path]:
    from .export import (
        BatteryCfInput, LifecycleStages, dumps_json, export_battery_passport_cf, export_catenax_pcf,
        export_cbam_communication, export_cmrt, export_pact,
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = [
        build_oem_questionnaire_de(out / "oem_fragebogen_de_100.xlsx"),
        build_cbam_template(out / "cbam_communication_template.xlsx"),
        build_cmrt_template(out / "cmrt_simplified_template.xlsx"),
        build_csv_questionnaire(out / "oem_fragebogen_kurz.csv"),
        build_sample_pdf(out / "stromrechnung_2025.pdf"),
    ]
    export_cbam_communication(sample_cbam(), out / "cbam_communication_sample.xlsx")
    files.append(out / "cbam_communication_sample.xlsx")
    export_cmrt(sample_cmrt(), out / "cmrt_sample.xlsx")
    files.append(out / "cmrt_sample.xlsx")
    pcf = sample_pcf_input()
    (out / "pact_productfootprint_sample.json").write_text(export_pact(pcf).to_json() + "\n", encoding="utf-8")
    cx = export_catenax_pcf(pcf, {"product_or_sector_specific_rules": ["Catena-X PCF Rulebook"]})
    (out / "catenax_pcf_sample.json").write_text(cx.to_json() + "\n", encoding="utf-8")
    bat = export_battery_passport_cf(BatteryCfInput(
        manufacturer_name="Demo Cells GmbH", battery_model_id="DC-NMC-77", battery_model_name="Pack 77 kWh",
        manufacturing_plant_country="DE", carbon_footprint_per_kwh=Decimal("61.4"), chemistry="NMC",
        lifecycle_stages=LifecycleStages(raw_material_acquisition_and_pre_processing=Decimal("44.1"),
                                         main_product_production=Decimal("14.2"), distribution=Decimal("1.3"),
                                         end_of_life_and_recycling=Decimal("1.8")),
        created=CREATED))
    (out / "battery_passport_cf_fragment_sample.json").write_text(dumps_json(bat.document) + "\n", encoding="utf-8")
    files += [out / "pact_productfootprint_sample.json", out / "catenax_pcf_sample.json",
              out / "battery_passport_cf_fragment_sample.json"]
    return files


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        args = ["examples"]
    for d in args:
        for f in generate(d):
            print(f)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

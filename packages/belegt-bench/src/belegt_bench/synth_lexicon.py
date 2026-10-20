# Spec: 11 §2.2(3)（bench-synth：题干由我方改写，不复制受版权保护的问卷原文）、§2.4 LBL-MAP、§2.7 语言分层
"""bench-synth 的改写词库（CC BY 4.0，作为数据随 belegt-bench 发布）。

- `PARAPHRASES`：DataPoint → 各语言的我方改写题干模板（`{y}` = 报告年份，`{u}` = 题目要求单位）。
  这些是“像真实问卷那样问”的说法，与字典 `label` 措辞刻意不同，用于 medium 难度。
- `HARD_CASES`：手写难例（含混题、OOD、干扰项、否定、缩写、德英混排、错别字、单位不一致、跨语种）。
  `acceptable=[]` 且 `ood=True` 表示字典中无对应数据点，正确行为只有“转人工”（11 §2.4、§3.2）。

所有文本均为本项目撰写，不来自任何受版权保护的问卷模板。
"""
from __future__ import annotations

PARAPHRASES: dict[str, dict[str, list[str]]] = {
    "dp:energy.consumption.electricity_total": {
        "de": ["Wie hoch war Ihr gesamter Strombezug im Jahr {y} ({u})?", "Stromverbrauch aller Standorte {y} in {u}",
               "Bitte nennen Sie die im Berichtsjahr {y} verbrauchte elektrische Energie."],
        "en": ["How much electricity did your company consume in {y} ({u})?",
               "Total power consumption across all sites, reporting year {y}"],
        "fr": ["Consommation totale d'électricité en {y} ({u})"],
        "pl": ["Całkowite zużycie energii elektrycznej w roku {y} ({u})"],
    },
    "dp:energy.consumption.natural_gas": {
        "de": ["Erdgasverbrauch {y} ({u})", "Welche Menge Erdgas wurde {y} an Ihren Standorten eingesetzt?"],
        "en": ["Natural gas used in {y}, in {u}", "What was your natural gas consumption during {y}?"],
    },
    "dp:energy.consumption.fuels_total": {
        "de": ["Gesamter Brennstoffeinsatz (Erdgas, Heizöl, Diesel usw.) {y} in {u}"],
        "en": ["Total fuel consumption from all combustion sources in {y} ({u})"],
    },
    "dp:energy.consumption.total": {
        "de": ["Gesamtenergieverbrauch inkl. Strom, Wärme und Brennstoffe {y} ({u})"],
        "en": ["Total energy consumption (electricity, heat and fuels) in {y}"],
    },
    "dp:energy.consumption.renewable_share": {
        "de": ["Welcher Anteil Ihres Energieverbrauchs stammt aus erneuerbaren Quellen ({u})?",
               "Anteil erneuerbarer Energien am Gesamtenergieverbrauch {y}"],
        "en": ["What percentage of your total energy use came from renewable sources in {y}?"],
    },
    "dp:energy.generation.onsite_renewable": {
        "de": ["Eigenerzeugter Strom aus PV- oder Windanlagen am Standort {y} ({u})"],
        "en": ["Electricity generated on site from your own solar or wind installations in {y}"],
    },
    "dp:energy.procurement.eac_volume": {
        "de": ["Strommenge, für die Herkunftsnachweise entwertet wurden, {y} ({u})"],
        "en": ["Volume of electricity covered by cancelled guarantees of origin or RECs in {y} ({u})"],
    },
    "dp:env.ghg.scope1_total": {
        "de": ["Direkte Treibhausgasemissionen (Scope 1) {y} in {u}", "Wie hoch waren Ihre Scope-1-Emissionen {y}?"],
        "en": ["Gross direct (Scope 1) greenhouse gas emissions in {y} ({u})",
               "Please report your Scope 1 emissions for {y}."],
        "fr": ["Émissions directes de GES (scope 1) en {y} ({u})"],
    },
    "dp:env.ghg.scope2_location_based": {
        "de": ["Scope-2-Emissionen nach standortbasierter Methode {y} ({u})",
               "Indirekte Emissionen aus eingekauftem Strom, location-based, {y}"],
        "en": ["Scope 2 emissions using the location-based method, {y} ({u})"],
    },
    "dp:env.ghg.scope2_market_based": {
        "de": ["Scope-2-Emissionen marktbasiert (unter Berücksichtigung von Herkunftsnachweisen) {y}"],
        "en": ["Market-based Scope 2 emissions for {y}, reflecting your contractual instruments ({u})"],
    },
    "dp:env.ghg.scope3_total": {
        "de": ["Summe der Scope-3-Emissionen über alle relevanten Kategorien {y} ({u})"],
        "en": ["Total Scope 3 (value chain) emissions in {y}"],
    },
    "dp:env.ghg.scope3_category.c01": {
        "de": ["Scope 3 Kategorie 1 – eingekaufte Waren und Dienstleistungen {y} ({u})"],
        "en": ["Emissions from purchased goods and services (Scope 3, category 1) in {y}"],
    },
    "dp:env.ghg.scope3_category.c04": {
        "de": ["Emissionen aus vorgelagertem Transport und Verteilung (Scope 3 Kat. 4) {y}"],
        "en": ["Upstream transportation and distribution emissions (category 4), {y}"],
    },
    "dp:env.ghg.intensity_turnover": {
        "de": ["THG-Intensität bezogen auf den Umsatz ({u})"],
        "en": ["GHG emissions intensity per euro of net revenue in {y}"],
    },
    "dp:env.ghg_target.has_target": {
        "de": ["Haben Sie sich ein Ziel zur Reduktion Ihrer Treibhausgasemissionen gesetzt?"],
        "en": ["Has your company set a greenhouse gas emissions reduction target?"],
    },
    "dp:env.ghg_target.reduction_pct": {
        "de": ["Um wie viel Prozent wollen Sie Ihre Emissionen gegenüber dem Basisjahr senken?"],
        "en": ["By what percentage do you aim to reduce emissions versus your base year?"],
    },
    "dp:env.ghg_target.target_year": {
        "de": ["Bis zu welchem Jahr soll das Reduktionsziel erreicht werden?"],
        "en": ["In which year should your reduction target be achieved?"],
    },
    "dp:env.waste.generated": {
        "de": ["Gesamtes Abfallaufkommen {y} ({u})"],
        "en": ["Total waste generated by your operations in {y} ({u})"],
    },
    "dp:env.waste.generated.hazardous": {
        "de": ["Davon gefährliche Abfälle {y} ({u})"],
        "en": ["Amount of hazardous waste produced in {y}"],
    },
    "dp:env.waste.diverted_share": {
        "de": ["Anteil der Abfälle, die recycelt oder wiederverwendet wurden ({u})"],
        "en": ["Share of waste diverted from disposal through recycling or reuse in {y}"],
    },
    "dp:env.water.withdrawal_total": {
        "de": ["Gesamte Wasserentnahme {y} in {u}", "Wie viel Frischwasser haben Sie {y} entnommen?"],
        "en": ["Total water withdrawal in {y} ({u})"],
    },
    "dp:org.employees.headcount": {
        "de": ["Anzahl der Beschäftigten zum Stichtag 31.12.{y}", "Mitarbeiterzahl (Köpfe) am Jahresende {y}"],
        "en": ["Number of employees (headcount) at the end of {y}"],
        "cs": ["Počet zaměstnanců k 31. 12. {y}"],
    },
    "dp:org.employees.fte": {
        "de": ["Beschäftigte in Vollzeitäquivalenten (VZÄ) im Jahresdurchschnitt {y}"],
        "en": ["Average number of full-time equivalent employees in {y}"],
    },
    "dp:org.financials.turnover": {
        "de": ["Umsatzerlöse im Geschäftsjahr {y} ({u})", "Jahresumsatz {y}"],
        "en": ["Net revenue for financial year {y} ({u})"],
    },
    "dp:org.identity.legal_name": {
        "de": ["Vollständige Firmierung laut Handelsregister", "Name des Unternehmens"],
        "en": ["Registered legal name of your company"],
    },
    "dp:org.identity.registration_number": {
        "de": ["Handelsregisternummer"],
        "en": ["Company registration number"],
    },
    "dp:org.identity.eori_number": {
        "de": ["EORI-Nummer Ihres Unternehmens"],
        "en": ["Economic Operators Registration and Identification (EORI) number"],
    },
    "dp:org.identity.hq_country": {
        "de": ["Land des Hauptsitzes"],
        "en": ["Country where your headquarters is located"],
    },
    "dp:org.identity.nace_code": {
        "de": ["Haupttätigkeit nach NACE-Code"],
        "en": ["Primary NACE activity code"],
    },
    "dp:cert.iso14001.valid_until": {
        "de": ["Bis wann ist Ihr ISO-14001-Zertifikat gültig?"],
        "en": ["Expiry date of your ISO 14001 certificate"],
    },
    "dp:cert.iso14001.certificate_file": {
        "de": ["Bitte laden Sie Ihr aktuelles ISO-14001-Zertifikat hoch."],
        "en": ["Please upload a copy of your valid ISO 14001 certificate."],
    },
    "dp:cert.iso50001.valid_until": {
        "de": ["Gültigkeit des Energiemanagement-Zertifikats nach ISO 50001 (Ablaufdatum)"],
        "en": ["ISO 50001 energy management certificate – valid until"],
    },
    "dp:cert.iatf16949.valid_until": {
        "de": ["Ablaufdatum IATF-16949-Zertifikat"],
        "en": ["When does your IATF 16949 certification expire?"],
    },
    "dp:cert.iso45001.valid_until": {
        "de": ["Ablaufdatum des Arbeitsschutz-Zertifikats ISO 45001"],
        "en": ["Occupational health and safety certificate (ISO 45001) expiry date"],
    },
    "dp:social.health_safety.recordable_accidents": {
        "de": ["Anzahl meldepflichtiger Arbeitsunfälle {y}"],
        "en": ["Number of recordable work-related injuries in {y}"],
    },
    "dp:social.health_safety.recordable_accident_rate": {
        "de": ["Unfallquote (meldepflichtige Unfälle je 1 Mio. Arbeitsstunden) {y}"],
        "en": ["Recordable accident rate per million hours worked, {y}"],
    },
    "dp:social.training.avg_hours_per_employee": {
        "de": ["Durchschnittliche Weiterbildungsstunden pro Mitarbeiter {y}"],
        "en": ["Average hours of training per employee in {y}"],
    },
    "dp:social.workforce.headcount.female": {
        "de": ["Anzahl weiblicher Beschäftigter zum Jahresende {y}"],
        "en": ["Number of female employees at year end {y}"],
    },
    "dp:social.collective_bargaining.coverage_rate": {
        "de": ["Anteil der Beschäftigten mit Tarifbindung ({u})"],
        "en": ["Percentage of employees covered by collective bargaining agreements"],
    },
    "dp:gov.policy.human_rights_policy": {
        "de": ["Verfügen Sie über eine Grundsatzerklärung zur Achtung der Menschenrechte?"],
        "en": ["Do you have a human rights policy in place?"],
    },
    "dp:gov.policy.climate_transition_plan": {
        "de": ["Liegt ein Klima-Transitionsplan für Ihr Unternehmen vor?"],
        "en": ["Has your company adopted a climate transition plan?"],
    },
    "dp:gov.anticorruption.convictions_count": {
        "de": ["Anzahl rechtskräftiger Verurteilungen wegen Korruption oder Bestechung {y}"],
        "en": ["Number of convictions for corruption or bribery in {y}"],
    },
    "dp:supply.code_of_conduct.supplier_coc_in_place": {
        "de": ["Gibt es einen Verhaltenskodex für Ihre Lieferanten?"],
        "en": ["Do you require your suppliers to sign a supplier code of conduct?"],
    },
    "dp:mat.reach.svhc_present": {
        "de": ["Enthält das Erzeugnis SVHC-Stoffe der Kandidatenliste über 0,1 Massenprozent?"],
        "en": ["Does the article contain any Candidate List substance above 0.1 % w/w?"],
    },
    "dp:mat.reach.candidate_list_version": {
        "de": ["Stand der verwendeten REACH-Kandidatenliste (Datum)"],
        "en": ["Which version (date) of the REACH Candidate List did you check against?"],
    },
    "dp:mat.rohs.compliant": {
        "de": ["Ist das Produkt RoHS-konform?"],
        "en": ["Is the product compliant with the RoHS directive?"],
    },
    "dp:mat.scip.scip_number": {
        "de": ["SCIP-Nummer der ECHA-Meldung"],
        "en": ["SCIP notification number"],
    },
    "dp:mat.conflict_minerals.intentionally_added.tin": {
        "de": ["Wird Zinn (3TG) Ihren Produkten absichtlich zugesetzt?"],
        "en": ["Is tin intentionally added to your products?"],
    },
    "dp:mat.conflict_minerals.intentionally_added.tantalum": {
        "de": ["Ist Tantal für die Funktion oder Herstellung Ihrer Produkte notwendig?"],
        "en": ["Is tantalum necessary to the functionality or production of your products?"],
    },
    "dp:mat.conflict_minerals.all_smelters_identified": {
        "de": ["Haben Sie alle Schmelzen in Ihrer Lieferkette identifiziert?"],
        "en": ["Have you identified all smelters in your supply chain?"],
    },
    "dp:mat.recycled_content.steel_share": {
        "de": ["Recyclinganteil im eingesetzten Stahl ({u})"],
        "en": ["Recycled content share of the steel you use (%)"],
    },
    "dp:prod.pcf.cradle_to_gate": {
        "de": ["Product Carbon Footprint (cradle-to-gate) je kg Produkt, inkl. biogener Emissionen"],
        "en": ["Cradle-to-gate carbon footprint per declared unit, including biogenic emissions"],
    },
    "dp:prod.pcf.cradle_to_gate.excluding_biogenic": {
        "de": ["PCF cradle-to-gate ohne biogene Emissionen ({u})"],
        "en": ["Product footprint excluding biogenic carbon, cradle to gate"],
    },
    "dp:prod.pcf.primary_data_share": {
        "de": ["Primärdatenanteil des PCF ({u})"],
        "en": ["Share of primary data used in the PCF calculation"],
    },
    "dp:prod.pcf.declared_unit": {
        "de": ["Deklarierte Einheit, auf die sich der PCF bezieht"],
        "en": ["What is the declared unit of the footprint (e.g. kg, piece)?"],
    },
    "dp:prod.mass.net_per_unit": {
        "de": ["Nettogewicht je Stück ({u})"],
        "en": ["Net mass per unit of product"],
    },
    "dp:prod.identity.cn_code": {
        "de": ["Zolltarifnummer (KN-Code) des Produkts"],
        "en": ["Combined Nomenclature (CN) code of the goods"],
    },
    "dp:prod.cbam.see_direct": {
        "de": ["Spezifische direkte graue Emissionen der Ware {y} ({u})"],
        "en": ["Specific direct embedded emissions of the goods in {y} ({u})"],
    },
    "dp:prod.cbam.see_indirect": {
        "de": ["Spezifische indirekte graue Emissionen (Strom) {y} ({u})"],
        "en": ["Specific indirect embedded emissions from electricity consumed, {y}"],
    },
    "dp:prod.cbam.installation_id": {
        "de": ["Kennung der Anlage, in der die Ware hergestellt wurde"],
        "en": ["Identifier of the production installation"],
    },
    "dp:prod.cbam.production_route": {
        "de": ["Herstellungsverfahren (z. B. Hochofen, Elektrolichtbogenofen)"],
        "en": ["Production route used (e.g. BF-BOF, EAF)"],
    },
    "dp:prod.cbam.carbon_price_paid": {
        "de": ["Im Ursprungsland gezahlter CO2-Preis {y} ({u})"],
        "en": ["Carbon price effectively paid in the country of origin in {y}"],
    },
    "dp:prod.cbam.specific_electricity_consumption": {
        "de": ["Spezifischer Stromverbrauch je Tonne Ware ({u})"],
        "en": ["Electricity consumed per tonne of goods ({u})"],
    },
    "dp:prod.battery.carbon_footprint_per_kwh": {
        "de": ["CO2-Fußabdruck der Batterie je kWh Gesamtenergie über den Lebenszyklus"],
        "en": ["Battery carbon footprint per kWh of total energy delivered over service life"],
    },
    "dp:prod.battery.chemistry": {
        "de": ["Zellchemie der Batterie"],
        "en": ["Battery cell chemistry"],
    },
    "dp:prod.battery.recycled_content.cobalt": {
        "de": ["Rezyklatanteil Kobalt in der Batterie ({u})"],
        "en": ["Share of recycled cobalt in active materials"],
    },
    "dp:supply.eudr.geolocation": {
        "de": ["Geokoordinaten der Erzeugungsflächen"],
        "en": ["Geolocation of all plots of land where the commodity was produced"],
    },
    "dp:supply.eudr.country_of_production": {
        "de": ["Erzeugungsland des Rohstoffs"],
        "en": ["Country of production of the relevant commodity"],
    },
    "dp:supply.eudr.dds_reference": {
        "de": ["Referenznummer der Sorgfaltserklärung (EUDR)"],
        "en": ["Reference number of the due diligence statement"],
    },
    "dp:supply.eudr.commodity_quantity": {
        "de": ["Nettomasse des relevanten Rohstoffs ({u})"],
        "en": ["Net mass of the relevant commodity in kg"],
    },
}

#: 手写难例。kind：ambiguous / ood / distractor / negation / abbreviation / mixed_lang / typo / unit_mismatch /
#: compound / long_context / cross_lang / near_miss
HARD_CASES: list[dict] = [
    # —— 含混：任一可接受即算对（11 §2.4 ambiguous） ——
    {"text": "Energieverbrauch 2025 (MWh)", "lang": "de", "acceptable": ["dp:energy.consumption.total",
     "dp:energy.consumption.electricity_total"], "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "Scope 2 Emissionen 2025 in t CO2e", "lang": "de", "acceptable": ["dp:env.ghg.scope2_location_based",
     "dp:env.ghg.scope2_market_based"], "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "Please provide your Scope 2 figure for 2025.", "lang": "en",
     "acceptable": ["dp:env.ghg.scope2_location_based", "dp:env.ghg.scope2_market_based"], "flags": ["ambiguous"],
     "kind": "ambiguous"},
    {"text": "Anzahl Mitarbeiter", "lang": "de", "acceptable": ["dp:org.employees.headcount", "dp:org.employees.fte"],
     "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "Workforce size", "lang": "en", "acceptable": ["dp:org.employees.headcount", "dp:org.employees.fte"],
     "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "CO2-Fußabdruck Ihres Produkts", "lang": "de", "acceptable": ["dp:prod.pcf.cradle_to_gate",
     "dp:prod.pcf.cradle_to_gate.excluding_biogenic"], "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "Abfallmenge 2025", "lang": "de", "acceptable": ["dp:env.waste.generated",
     "dp:env.waste.generated.non_hazardous"], "flags": ["ambiguous"], "kind": "ambiguous"},
    {"text": "Gültigkeit Ihres Umweltzertifikats", "lang": "de", "acceptable": ["dp:cert.iso14001.valid_until"],
     "flags": [], "kind": "near_miss"},
    # —— OOD：字典无此数据点，正确行为只有转人工 ——
    {"text": "Wie viele Patente hat Ihr Unternehmen angemeldet?", "lang": "de", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Number of meals served in your staff canteen per year", "lang": "en", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Lärmemissionen am Standort in dB(A) nachts", "lang": "de", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Do you use AI tools in your quality management?", "lang": "en", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Anteil Frauen in Führungspositionen (%)", "lang": "de", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Average commuting distance of employees (km)", "lang": "en", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Bitte beschreiben Sie Ihre Strategie zur Kreislaufwirtschaft (Freitext).", "lang": "de",
     "acceptable": [], "ood": True, "kind": "ood"},
    {"text": "Gender pay gap (unadjusted) in %", "lang": "en", "acceptable": [], "ood": True, "kind": "ood"},
    {"text": "Wie viele Ladesäulen für E-Fahrzeuge betreiben Sie?", "lang": "de", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Packaging material per unit shipped (g)", "lang": "en", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Kältemittel-Nachfüllmenge 2025 (kg R-410A)", "lang": "de", "acceptable": [], "ood": True,
     "kind": "ood"},
    {"text": "Do you hold an EcoVadis rating? If yes, which medal?", "lang": "en", "acceptable": [], "ood": True,
     "kind": "ood"},
    # —— 干扰项：关键词相同但数据点不同 ——
    {"text": "Strom aus eigener PV-Anlage 2025 (MWh)", "lang": "de",
     "acceptable": ["dp:energy.generation.onsite_renewable"], "kind": "distractor"},
    {"text": "Strommenge mit Herkunftsnachweisen (HKN) 2025", "lang": "de",
     "acceptable": ["dp:energy.procurement.eac_volume"], "kind": "distractor"},
    {"text": "Electricity consumed per tonne of steel produced (MWh/t)", "lang": "en",
     "acceptable": ["dp:prod.cbam.specific_electricity_consumption"], "kind": "distractor"},
    {"text": "Emissionen aus Strombezug je Tonne Ware (indirekt)", "lang": "de",
     "acceptable": ["dp:prod.cbam.see_indirect"], "kind": "distractor"},
    {"text": "Anteil Recyclingstahl im Produkt (%)", "lang": "de",
     "acceptable": ["dp:mat.recycled_content.steel_share"], "kind": "distractor"},
    {"text": "Weibliche Beschäftigte (Anzahl) zum 31.12.2025", "lang": "de",
     "acceptable": ["dp:social.workforce.headcount.female"], "kind": "distractor"},
    {"text": "Number of temporary employees at year end", "lang": "en",
     "acceptable": ["dp:social.workforce.temporary_count"], "kind": "distractor"},
    {"text": "Fines paid for bribery cases in 2025 (EUR)", "lang": "en",
     "acceptable": ["dp:gov.anticorruption.fines_amount"], "kind": "distractor"},
    {"text": "Gefährliche Abfälle 2025 in Tonnen", "lang": "de",
     "acceptable": ["dp:env.waste.generated.hazardous"], "kind": "distractor"},
    {"text": "Non-hazardous waste generated, 2025 (t)", "lang": "en",
     "acceptable": ["dp:env.waste.generated.non_hazardous"], "kind": "distractor"},
    {"text": "PCF ohne biogene Kohlenstoffanteile (kg CO2e/kg)", "lang": "de",
     "acceptable": ["dp:prod.pcf.cradle_to_gate.excluding_biogenic"], "kind": "distractor"},
    {"text": "Scope 3 – nur Transport zum Werk (vorgelagert), 2025", "lang": "de",
     "acceptable": ["dp:env.ghg.scope3_category.c04"], "kind": "distractor"},
    # —— 否定 / 反向问法 ——
    {"text": "Ist Ihr Produkt frei von SVHC-Stoffen > 0,1 %? (Ja/Nein)", "lang": "de",
     "acceptable": ["dp:mat.reach.svhc_present"], "kind": "negation"},
    {"text": "Confirm that no tin is intentionally added to the product.", "lang": "en",
     "acceptable": ["dp:mat.conflict_minerals.intentionally_added.tin"], "kind": "negation"},
    {"text": "Gibt es Standorte, die NICHT in der Nähe von Schutzgebieten liegen?", "lang": "de",
     "acceptable": ["dp:env.biodiversity.sites_near_sensitive_area"], "kind": "negation"},
    # —— 缩写 / 行话 ——
    {"text": "THG S1 FY25 [t]", "lang": "de", "acceptable": ["dp:env.ghg.scope1_total"], "kind": "abbreviation"},
    {"text": "GHG S2 MB 2025", "lang": "en", "acceptable": ["dp:env.ghg.scope2_market_based"],
     "kind": "abbreviation"},
    {"text": "S2 LB (tCO2e)", "lang": "en", "acceptable": ["dp:env.ghg.scope2_location_based"],
     "kind": "abbreviation"},
    {"text": "VZÄ Ø 2025", "lang": "de", "acceptable": ["dp:org.employees.fte"], "kind": "abbreviation"},
    {"text": "HR-Nr.", "lang": "de", "acceptable": ["dp:org.identity.registration_number"],
     "kind": "abbreviation"},
    {"text": "LTIR / TRIR 2025", "lang": "en", "acceptable": ["dp:social.health_safety.recordable_accident_rate"],
     "kind": "abbreviation"},
    {"text": "KN-Code / HS-Code", "lang": "de", "acceptable": ["dp:prod.identity.cn_code"], "kind": "abbreviation"},
    {"text": "SEE dir. (tCO2e/t)", "lang": "en", "acceptable": ["dp:prod.cbam.see_direct"], "kind": "abbreviation"},
    {"text": "DDS-Ref.-Nr.", "lang": "de", "acceptable": ["dp:supply.eudr.dds_reference"], "kind": "abbreviation"},
    {"text": "PDS in %", "lang": "en", "acceptable": ["dp:prod.pcf.primary_data_share"], "kind": "abbreviation"},
    # —— 德英混排 ——
    {"text": "Total Scope 1 Emissionen (direct) für 2025", "lang": "de", "acceptable": ["dp:env.ghg.scope1_total"],
     "kind": "mixed_lang"},
    {"text": "Renewable energy Anteil am Gesamtverbrauch", "lang": "de",
     "acceptable": ["dp:energy.consumption.renewable_share"], "kind": "mixed_lang"},
    {"text": "Headcount zum Stichtag (end of year)", "lang": "de", "acceptable": ["dp:org.employees.headcount"],
     "kind": "mixed_lang"},
    {"text": "Water withdrawal gesamt in m³", "lang": "de", "acceptable": ["dp:env.water.withdrawal_total"],
     "kind": "mixed_lang"},
    {"text": "Supplier Code of Conduct vorhanden?", "lang": "de",
     "acceptable": ["dp:supply.code_of_conduct.supplier_coc_in_place"], "kind": "mixed_lang"},
    # —— 错别字 ——
    {"text": "Stromverbruach gesammt 2025", "lang": "de", "acceptable": ["dp:energy.consumption.electricity_total"],
     "kind": "typo"},
    {"text": "Scpoe 1 emissoins in 2025", "lang": "en", "acceptable": ["dp:env.ghg.scope1_total"], "kind": "typo"},
    {"text": "Erdgasverbruach (kWh)", "lang": "de", "acceptable": ["dp:energy.consumption.natural_gas"],
     "kind": "typo"},
    {"text": "Wasserentnahem insgesamt", "lang": "de", "acceptable": ["dp:env.water.withdrawal_total"],
     "kind": "typo"},
    {"text": "ISO 14001 certficate expiry", "lang": "en", "acceptable": ["dp:cert.iso14001.valid_until"],
     "kind": "typo"},
    # —— 单位与字典不一致（仍是同一数据点） ——
    {"text": "Strombezug 2025 in kWh", "lang": "de", "acceptable": ["dp:energy.consumption.electricity_total"],
     "kind": "unit_mismatch", "unit_hint": "kWh"},
    {"text": "Scope 1 emissions 2025 in kg CO2e", "lang": "en", "acceptable": ["dp:env.ghg.scope1_total"],
     "kind": "unit_mismatch", "unit_hint": "kgCO2e"},
    {"text": "Erdgasverbrauch 2025 in GJ", "lang": "de", "acceptable": ["dp:energy.consumption.natural_gas"],
     "kind": "unit_mismatch", "unit_hint": "GJ"},
    {"text": "Water withdrawal 2025 in litres", "lang": "en", "acceptable": ["dp:env.water.withdrawal_total"],
     "kind": "unit_mismatch", "unit_hint": "L"},
    {"text": "Abfall gesamt 2025 in kg", "lang": "de", "acceptable": ["dp:env.waste.generated"],
     "kind": "unit_mismatch", "unit_hint": "kg"},
    # —— 复合题（一题多点：任一可接受，完整拆分由 transform 处理） ——
    {"text": "Scope 1 und Scope 2 (marktbasiert) Emissionen 2025, getrennt ausweisen", "lang": "de",
     "acceptable": ["dp:env.ghg.scope1_total", "dp:env.ghg.scope2_market_based"], "flags": ["ambiguous"],
     "kind": "compound"},
    {"text": "Number of employees by gender (female / male)", "lang": "en",
     "acceptable": ["dp:social.workforce.headcount.female", "dp:social.workforce.headcount.male"],
     "flags": ["ambiguous"], "kind": "compound"},
    # —— 长上下文 ——
    {"text": ("Im Rahmen unserer Lieferantenbewertung 2026 bitten wir um Angaben zu Ihren Umweltkennzahlen. "
              "Geben Sie hier bitte die direkten Emissionen aus eigenen Verbrennungsanlagen und Fahrzeugen für das "
              "Kalenderjahr 2025 an (Tonnen CO2-Äquivalente, nach GHG Protocol)."), "lang": "de",
     "acceptable": ["dp:env.ghg.scope1_total"], "kind": "long_context"},
    {"text": ("As part of our 2026 supplier assessment we kindly ask for data on resource use. In this field, "
              "report the total volume of water your sites drew from all sources (municipal supply, groundwater, "
              "surface water) during calendar year 2025."), "lang": "en",
     "acceptable": ["dp:env.water.withdrawal_total"], "kind": "long_context"},
    # —— 其他语种 ——
    {"text": "Émissions de scope 2 basées sur le marché (2025)", "lang": "fr",
     "acceptable": ["dp:env.ghg.scope2_market_based"], "kind": "cross_lang"},
    {"text": "Emisje bezpośrednie (zakres 1) w 2025 r.", "lang": "pl", "acceptable": ["dp:env.ghg.scope1_total"],
     "kind": "cross_lang"},
    {"text": "Spotřeba elektřiny celkem za rok 2025 (MWh)", "lang": "cs",
     "acceptable": ["dp:energy.consumption.electricity_total"], "kind": "cross_lang"},
    {"text": "Consumo totale di gas naturale nel 2025", "lang": "it",
     "acceptable": ["dp:energy.consumption.natural_gas"], "kind": "cross_lang"},
    {"text": "Doğrudan gömülü emisyonlar (tCO2e/t)", "lang": "tr", "acceptable": ["dp:prod.cbam.see_direct"],
     "kind": "cross_lang"},
    {"text": "2025年范围一温室气体排放总量（吨二氧化碳当量）", "lang": "zh",
     "acceptable": ["dp:env.ghg.scope1_total"], "kind": "cross_lang"},
    {"text": "产品的海关编码（CN 编码）", "lang": "zh", "acceptable": ["dp:prod.identity.cn_code"],
     "kind": "cross_lang"},
]

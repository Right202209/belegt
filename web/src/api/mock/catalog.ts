// Spec: 02（DataPoint ID 规则 dp:<domain>.<topic>.<metric>）；_baseline §D 示例
// 演示用的 OEM 问卷题库。公司名、数值均为示例值（01 阅读约定）。
import type { I18nText, ValueType } from '../../types';

export interface CatalogEntry {
  dp: string; // 不带 @semver
  text: I18nText;
  value_type: ValueType;
  unit?: string;
  value: string | boolean;
  boundary?: string;
  section: string;
  fact_key: string;
}

export const CATALOG: CatalogEntry[] = [
  { section: 'A', dp: 'dp:org.employees.headcount', value_type: 'number', unit: '1', value: '380', fact_key: 'org.headcount', text: { de: 'Anzahl Mitarbeitende zum 31.12.2025', en: 'Number of employees as of 2025-12-31', zh: '截至 2025-12-31 的员工人数' } },
  { section: 'A', dp: 'dp:org.legal_entity.name', value_type: 'text', value: 'Schmid Umformtechnik GmbH', fact_key: 'org.legal_name', text: { de: 'Rechtlicher Name des Unternehmens', en: 'Legal name of the company', zh: '公司法定名称' } },
  { section: 'A', dp: 'dp:org.legal_entity.country', value_type: 'enum', value: 'DE', fact_key: 'org.country', text: { de: 'Sitzland', en: 'Country of registration', zh: '注册国家' } },
  { section: 'A', dp: 'dp:org.sites.count', value_type: 'number', unit: '1', value: '2', fact_key: 'org.sites', text: { de: 'Anzahl Produktionsstandorte', en: 'Number of production sites', zh: '生产基地数量' } },
  { section: 'B', dp: 'dp:energy.consumption.electricity_total', value_type: 'number', unit: 'MWh', value: '4820', fact_key: 'activity.electricity.purchased', text: { de: 'Stromverbrauch gesamt 2025', en: 'Total electricity consumption 2025', zh: '2025 年总用电量' } },
  { section: 'B', dp: 'dp:energy.consumption.natural_gas', value_type: 'number', unit: 'MWh', value: '2140', fact_key: 'activity.fuel.natural_gas', text: { de: 'Erdgasverbrauch 2025', en: 'Natural gas consumption 2025', zh: '2025 年天然气消耗' } },
  { section: 'B', dp: 'dp:energy.consumption.diesel', value_type: 'number', unit: 'MWh', value: '312', fact_key: 'activity.fuel.diesel', text: { de: 'Dieselverbrauch Fuhrpark 2025', en: 'Fleet diesel consumption 2025', zh: '2025 年车队柴油消耗' } },
  { section: 'B', dp: 'dp:energy.consumption.onsite_renewable', value_type: 'number', unit: 'MWh', value: '410', fact_key: 'activity.electricity.onsite_pv', text: { de: 'Eigenerzeugung PV 2025', en: 'On-site PV generation 2025', zh: '2025 年自发光伏电量' } },
  { section: 'C', dp: 'dp:env.ghg.scope1_total', value_type: 'number', unit: 'tCO2e', value: '1820', fact_key: 'ghg.scope1', text: { de: 'Scope-1-Emissionen 2025', en: 'Scope 1 emissions 2025', zh: '2025 年范围一排放' } },
  { section: 'C', dp: 'dp:env.ghg.scope3_cat1', value_type: 'number', unit: 'tCO2e', value: '21400', fact_key: 'ghg.scope3.c1', text: { de: 'Scope 3 Kat. 1 (eingekaufte Waren) 2025', en: 'Scope 3 cat. 1 (purchased goods) 2025', zh: '2025 年范围三类别 1（外购商品）' } },
  { section: 'D', dp: 'dp:env.water.withdrawal_total', value_type: 'number', unit: 'm3', value: '9050', fact_key: 'activity.water.withdrawal', text: { de: 'Wasserentnahme gesamt 2025', en: 'Total water withdrawal 2025', zh: '2025 年总取水量' } },
  { section: 'D', dp: 'dp:env.waste.total', value_type: 'number', unit: 't', value: '980', fact_key: 'activity.waste.total', text: { de: 'Abfallaufkommen gesamt 2025', en: 'Total waste 2025', zh: '2025 年废弃物总量' } },
  { section: 'D', dp: 'dp:env.waste.hazardous', value_type: 'number', unit: 't', value: '120', fact_key: 'activity.waste.hazardous', text: { de: 'Gefährliche Abfälle 2025', en: 'Hazardous waste 2025', zh: '2025 年危险废弃物' } },
  { section: 'D', dp: 'dp:env.waste.recycling_share', value_type: 'number', unit: '%', value: '78', fact_key: 'activity.waste.recycled', text: { de: 'Recyclingquote Abfall 2025', en: 'Waste recycling rate 2025', zh: '2025 年废弃物回收率' } },
  { section: 'E', dp: 'dp:mat.reach.svhc_present', value_type: 'bool', value: false, fact_key: 'mat.svhc', text: { de: 'Enthalten Ihre Produkte SVHC > 0,1 % (w/w)?', en: 'Do your products contain SVHCs > 0.1 % w/w?', zh: '产品是否含有 > 0.1 %（质量比）的 SVHC？' } },
  { section: 'E', dp: 'dp:mat.rohs.compliant', value_type: 'bool', value: true, fact_key: 'mat.rohs', text: { de: 'RoHS-Konformität bestätigt?', en: 'RoHS compliance confirmed?', zh: '是否确认符合 RoHS？' } },
  { section: 'E', dp: 'dp:mat.recycled_content.steel_share', value_type: 'number', unit: '%', value: '31', fact_key: 'mat.recycled_steel', text: { de: 'Recyclinganteil Stahl im Einkauf 2025', en: 'Recycled content of purchased steel 2025', zh: '2025 年采购钢材再生料比例' } },
  { section: 'F', dp: 'dp:cert.iso14001.valid_until', value_type: 'date', value: '2026-12-31', fact_key: 'cert.iso14001', text: { de: 'ISO 14001 gültig bis', en: 'ISO 14001 valid until', zh: 'ISO 14001 有效期至' } },
  { section: 'F', dp: 'dp:cert.iso9001.valid_until', value_type: 'date', value: '2027-09-30', fact_key: 'cert.iso9001', text: { de: 'ISO 9001 gültig bis', en: 'ISO 9001 valid until', zh: 'ISO 9001 有效期至' } },
  { section: 'F', dp: 'dp:cert.iso50001.certified', value_type: 'bool', value: true, fact_key: 'cert.iso50001', text: { de: 'Energiemanagement nach ISO 50001 zertifiziert?', en: 'Energy management certified to ISO 50001?', zh: '能源管理是否通过 ISO 50001 认证？' } },
  { section: 'G', dp: 'dp:gov.code_of_conduct.exists', value_type: 'bool', value: true, fact_key: 'gov.coc', text: { de: 'Verhaltenskodex vorhanden?', en: 'Code of conduct in place?', zh: '是否有行为准则？' } },
  { section: 'G', dp: 'dp:gov.anti_corruption.policy', value_type: 'bool', value: true, fact_key: 'gov.anticorruption', text: { de: 'Antikorruptionsrichtlinie vorhanden?', en: 'Anti-corruption policy in place?', zh: '是否有反腐败政策？' } },
  { section: 'G', dp: 'dp:gov.whistleblowing.channel', value_type: 'bool', value: true, fact_key: 'gov.whistleblowing', text: { de: 'Hinweisgebersystem eingerichtet?', en: 'Whistleblowing channel established?', zh: '是否设立举报渠道？' } },
  { section: 'G', dp: 'dp:gov.sustainability.responsible_role', value_type: 'text', value: 'Qualitätsmanagementbeauftragte (QMB)', fact_key: 'gov.responsible', text: { de: 'Verantwortliche Funktion für Nachhaltigkeit', en: 'Function responsible for sustainability', zh: '可持续发展负责职能' } },
  { section: 'H', dp: 'dp:social.safety.ltifr', value_type: 'number', unit: '1', value: '3.1', fact_key: 'social.ltifr', text: { de: 'Unfallhäufigkeit LTIFR 2025', en: 'Lost time injury frequency rate 2025', zh: '2025 年损工事故率 LTIFR' } },
  { section: 'H', dp: 'dp:social.training.hours_per_employee', value_type: 'number', unit: 'h', value: '14.5', fact_key: 'social.training', text: { de: 'Schulungsstunden je Mitarbeitende 2025', en: 'Training hours per employee 2025', zh: '2025 年人均培训小时' } },
  { section: 'H', dp: 'dp:social.employees.female_share', value_type: 'number', unit: '%', value: '22', fact_key: 'social.female', text: { de: 'Frauenanteil Belegschaft 2025', en: 'Share of female employees 2025', zh: '2025 年女性员工占比' } },
  { section: 'I', dp: 'dp:supply.supplier_code.signed_share', value_type: 'number', unit: '%', value: '86', fact_key: 'supply.coc_signed', text: { de: 'Anteil Lieferanten mit unterzeichnetem Lieferantenkodex', en: 'Share of suppliers that signed the supplier code', zh: '已签署供应商准则的供应商比例' } },
  { section: 'I', dp: 'dp:supply.conflict_minerals.policy', value_type: 'bool', value: true, fact_key: 'supply.cm_policy', text: { de: 'Konfliktmineralien-Richtlinie vorhanden?', en: 'Conflict minerals policy in place?', zh: '是否有冲突矿产政策？' } },
  { section: 'I', dp: 'dp:supply.audits.count', value_type: 'number', unit: '1', value: '7', fact_key: 'supply.audits', text: { de: 'Durchgeführte Lieferantenaudits 2025', en: 'Supplier audits conducted 2025', zh: '2025 年开展的供应商审核次数' } },
];

export const SITE_VARIANTS: { boundary: string; de: string; en: string; zh: string }[] = [
  { boundary: 'organization', de: '', en: '', zh: '' },
  { boundary: 'site:site_01JD0000000000000000000ULM', de: ' – Werk Ulm', en: ' – Ulm plant', zh: '——乌尔姆工厂' },
  { boundary: 'site:site_01JD000000000000000000REUT', de: ' – Werk Reutlingen', en: ' – Reutlingen plant', zh: '——罗伊特林根工厂' },
];

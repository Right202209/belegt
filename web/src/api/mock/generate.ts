// Spec: 01 §2.1（核心旅程：100 题 OEM Excel 问卷）、03 §3（对象字段）、03 §5.6（Scope 2 串联实例）、06 §2.2/§4.2
// 生成一份确定性的演示世界：1 份 100 题 OEM 问卷（in_review / awaiting_human）+ 1 份 CDP 问卷 + 若干其它阶段的问卷。
// 风险分布与 01 UI-01 示意一致：L0 61、L1 26、L2 8(+2)、L3 2(+1)。
import type {
  Answer,
  Approval,
  Calculation,
  ClosedQuestion,
  Conflict,
  Counterparty,
  Evidence,
  FactorSet,
  GraphBase,
  I18nText,
  Question,
  Request,
  RequestView,
  RiskLevel,
  Role,
  SupplierRequestView,
} from '../../types';
import { toIsoDate } from '../../domain/dates';
import { requiredRolesFor } from '../../domain/policy';
import { CATALOG, SITE_VARIANTS, type CatalogEntry } from './catalog';
import { fakeSha, mkId, ref } from './ids';

export const ORG_ID = 'org_01JC0A1B2C3D4E5F6G7H8J9K0M';
export const SCHEMA = 'belegt.graph/2026-10';
export const POLICY_VERSION = 'approval-policy@1.0.0';

export const USERS: Record<'compliance_owner' | 'executive' | 'sales' | 'contributor', { id: string; name: string }> = {
  compliance_owner: { id: 'user:petra_huber', name: 'Petra Huber' },
  executive: { id: 'user:thomas_schmid', name: 'Thomas Schmid' },
  sales: { id: 'user:lukas_berger', name: 'Lukas Berger' },
  contributor: { id: 'user:jonas_maier', name: 'Jonas Maier' },
};

export interface MockWorld {
  today: string;
  counterparties: Counterparty[];
  tiers: Record<string, 'tier_a' | 'tier_b' | 'tier_c'>;
  requests: Request[];
  staticProgress: Record<string, RequestView['progress']>;
  questions: Question[];
  answers: Answer[];
  answerHead: Record<string, number>;
  evidence: Evidence[];
  calculations: Calculation[];
  approvals: Approval[];
  conflicts: Conflict[];
  closedQuestions: ClosedQuestion[];
  supplierRequests: SupplierRequestView[];
  factorSets: FactorSet[];
  excerpts: Record<string, string>;
}

export function generateWorld(baseDate: Date = new Date()): MockWorld {
  let seq = 1;
  const next = () => seq++;
  const day = (n: number) => toIsoDate(new Date(baseDate.getTime() + n * 86_400_000));
  const dt = (n: number, hh = 8) => `${day(n)}T${String(hh).padStart(2, '0')}:15:00Z`;
  const base = (prefix: string, version: number, created_by: string, createdDay = -2, trust: 'trusted' | 'quarantine' = 'trusted'): GraphBase & { id: string } => {
    const id = mkId(prefix, next());
    return {
      id,
      org_id: ORG_ID,
      version,
      created_at: dt(createdDay),
      created_by,
      trust_zone: trust,
      provenance: mkId('trc_', next()),
      supersedes: version > 1 ? ref(id, version - 1) : null,
      schema_version: SCHEMA,
      content_hash: fakeSha(id + version),
    };
  };
  const today = day(0);
  const period2025 = { start: '2025-01-01', end: '2025-12-31' };

  // ───── Counterparty ─────
  const cp = (name: string, role: 'customer' | 'supplier', country: string): Counterparty => ({ ...base('cp_', 1, 'user:petra_huber', -300), role, legal_name: name, country });
  const oemA = cp('Muster Automobil AG (OEM-A)', 'customer', 'DE');
  const oemB = cp('Beispiel Fahrzeugtechnik SE (OEM-B)', 'customer', 'DE');
  const tier1 = cp('Nordlicht Systems GmbH (Tier-1)', 'customer', 'AT');
  const supTR = cp('Kocaeli Çelik A.Ş.', 'supplier', 'TR');
  const supCN = cp('Jiangsu Rohr Co., Ltd.', 'supplier', 'CN');
  const supIT = cp('Brescia Acciai S.p.A.', 'supplier', 'IT');
  const supPL = cp('Silesia Stal Sp. z o.o.', 'supplier', 'PL');
  const supCZ = cp('Ostrava Profil s.r.o.', 'supplier', 'CZ');
  const counterparties = [oemA, oemB, tier1, supTR, supCN, supIT, supPL, supCZ];
  const tiers: MockWorld['tiers'] = { [oemA.id]: 'tier_a', [oemB.id]: 'tier_b', [tier1.id]: 'tier_c' };

  // ───── FactorSet（03 §3.13；ID 连字符形式，_baseline §M） ─────
  const factorSets: FactorSet[] = [
    { id: 'fs:aib-residual-mix@2025.1', source: 'aib-residual-mix', license: 'CC-BY-4.0', published_at: '2025-06-01', valid_from: '2025-01-01', valid_until: '2025-12-31', row_count: 412, rows_hash: fakeSha('aib2025'), status: 'active' },
    { id: 'fs:uba-strommix@2026.03', source: 'uba', license: 'DL-DE-BY-2.0', published_at: '2026-03-20', valid_from: '2025-01-01', row_count: 38, rows_hash: fakeSha('uba2026'), status: 'active' },
    { id: 'fs:uba-strommix@2025.04', source: 'uba', license: 'DL-DE-BY-2.0', published_at: '2025-04-10', valid_from: '2024-01-01', row_count: 36, rows_hash: fakeSha('uba2025'), status: 'deprecated' },
    { id: 'fs:eu-cbam-default@2026.07', source: 'eu-cbam-default', license: 'CC-BY-4.0', published_at: '2026-07-01', valid_from: '2026-01-01', row_count: 1290, rows_hash: fakeSha('cbam2026'), status: 'active' },
  ];

  // ───── Requests ─────
  const mkReq = (title: string, cpId: string, status: Request['status'], due: number, template: string | null, extra: Partial<Request> = {}): Request => ({
    ...base('req_', 3, 'service:workflow', -20),
    counterparty_id: cpId,
    channel: 'email',
    source_ref: { message_id: `<${next()}@mail.example>`, blob_sha256: [fakeSha(title)] },
    template_ref: template,
    title,
    language: 'de',
    due_date: day(due),
    reporting_period: period2025,
    priority: due < 30 ? 'high' : 'normal',
    triage_confidence: 0.94,
    status,
    assignee: USERS.compliance_owner.id,
    ...extra,
  });
  const req1 = mkReq('OEM-A Supplier Sustainability Questionnaire 2026', oemA.id, 'awaiting_human', 57, null, {
    status_reason: 'awaiting_human:conflict;return_to=auditing',
    priority: 'high',
  });
  const req2 = mkReq('CDP Supply Chain 2026 – OEM-B', oemB.id, 'in_review', 34, 'tm:cdp_climate@2026');
  const req3 = mkReq('REACH/SCIP Anfrage Halter 4711', tier1.id, 'evidence_gathering', 41, 'tm:reach_scip@2026');
  const req4 = mkReq('Catena-X PCF – Halter 4711', oemA.id, 'awaiting_supplier', 70, 'tm:catenax_pcf@4.0');
  const req5 = mkReq('Kundenfragebogen Nachhaltigkeit (Excel) – Nordlicht', tier1.id, 'halted', 25, null, { status_reason: 'halted:budget;return_to=drafting' });
  const req6 = mkReq('CMRT 6.4 Konfliktmineralien 2026', oemB.id, 'received', 88, 'tm:cmrt@6.4', { triage_confidence: 0.82, channel: 'portal' });
  const req7 = mkReq('CDP Supply Chain 2025 – OEM-B', oemB.id, 'archived', -210, 'tm:cdp_climate@2025');
  const req8 = mkReq('VSME Basismodul – Nordlicht', tier1.id, 'submitted', -12, 'tm:vsme@2026');
  const req9 = mkReq('OEM-A Lieferantenselbstauskunft 2026', oemA.id, 'drafting', 48, null);
  const requests = [req1, req2, req3, req4, req5, req6, req7, req8, req9];
  const staticProgress: MockWorld['staticProgress'] = {
    [req3.id]: { questions: 18, mapped: 18, evidenced: 11, approved: 0, submitted: 0, est_minutes_remaining: 25, open_conflicts: 0, open_cards: 1 },
    [req4.id]: { questions: 12, mapped: 12, evidenced: 7, approved: 0, submitted: 0, est_minutes_remaining: 15, open_conflicts: 0, open_cards: 0 },
    [req5.id]: { questions: 64, mapped: 61, evidenced: 40, approved: 0, submitted: 0, est_minutes_remaining: 70, open_conflicts: 0, open_cards: 0 },
    [req6.id]: { questions: 0, mapped: 0, evidenced: 0, approved: 0, submitted: 0, est_minutes_remaining: 0, open_conflicts: 0, open_cards: 1 },
    [req7.id]: { questions: 87, mapped: 87, evidenced: 87, approved: 87, submitted: 87, est_minutes_remaining: 0, open_conflicts: 0, open_cards: 0 },
    [req8.id]: { questions: 41, mapped: 41, evidenced: 41, approved: 41, submitted: 41, est_minutes_remaining: 0, open_conflicts: 0, open_cards: 0 },
    [req9.id]: { questions: 52, mapped: 50, evidenced: 44, approved: 0, submitted: 0, est_minutes_remaining: 40, open_conflicts: 0, open_cards: 0 },
  };

  const questions: Question[] = [];
  const answers: Answer[] = [];
  const evidence: Evidence[] = [];
  const calculations: Calculation[] = [];
  const approvals: Approval[] = [];
  const conflicts: Conflict[] = [];
  const excerpts: Record<string, string> = {};
  const answerHead: Record<string, number> = {};

  const mkQuestion = (req: Request, ordinal: number, native_id: string, text: I18nText, answer_format: Question['answer_format'], status: Question['status'] = 'mapped'): Question => {
    const q: Question = {
      ...base('q_', 2, 'agent:guard', -18),
      request_id: req.id,
      ordinal,
      native_id,
      text,
      answer_format,
      locator: { blob_sha256: req.source_ref.blob_sha256[0], mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', sheet: 'Fragebogen', cell: `E${ordinal + 6}` },
      status,
    };
    questions.push(q);
    return q;
  };

  const mkEvidence = (o: Partial<Evidence> & { fact_key: string; extracted_value: Evidence['extracted_value']; value_type: Evidence['value_type'] }, label: string): Evidence => {
    const src = o.source_type ?? 'document';
    const blob = fakeSha(label + o.fact_key);
    const locator: Evidence['locator'] =
      src === 'erp'
        ? { blob_sha256: blob, mime: 'application/json', connector: { connector_id: 'conn:sap-b1', query_hash: fakeSha('q' + label), record_key: `MM-${next()}`, fetched_at: dt(-5) } }
        : src === 'historical_answer'
          ? { blob_sha256: blob, mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', sheet: 'Antworten', cell: `F${10 + (next() % 80)}` }
          : src === 'human_confirmation'
            ? { blob_sha256: blob, mime: 'application/json', cq_ref: mkId('cq_', next()) }
            : { blob_sha256: blob, mime: 'application/pdf', page: 1 + (next() % 4), bbox: [72, 410.5, 520, 428] };
    const ev: Evidence = {
      ...base('ev_', 2, src === 'human_confirmation' ? USERS.compliance_owner.id : 'agent:evidence-miner', -10),
      source_type: src,
      boundary: 'organization',
      valid_from: '2025-01-01',
      confidence: 0.96,
      verification: { method: src === 'erp' ? 'connector_attested' : 'auto_rule', rule_id: 'EV-AUTO-0.90-LOC', by: 'agent:evidence-miner' },
      status: 'verified',
      ...o,
      locator,
    };
    evidence.push(ev);
    const shown = typeof ev.extracted_value === 'string' || typeof ev.extracted_value === 'boolean' ? String(ev.extracted_value) : '…';
    excerpts[ev.id] = `${label} · ${shown}${ev.unit ? ' ' + ev.unit : ''}`;
    return ev;
  };

  const historicalAnswer = (dp: string, value: string, unit: string | undefined, scope: string[] | 'all', reqId: string, extra: Partial<Answer> = {}): Answer => {
    const a: Answer = {
      ...base('ans_', 1, 'user:petra_huber', -220),
      datapoint_id: `${dp}@1.0.0`,
      value,
      value_type: 'number',
      unit,
      period: period2025,
      boundary: 'organization',
      customer_scope: scope,
      origin: 'imported',
      answer_key: fakeSha(dp + JSON.stringify(scope)),
      status: 'submitted',
      risk_level: 'L1',
      is_estimate: false,
      legal_commitment: false,
      submission: { request_id: reqId, channel: 'portal', submitted_at: dt(-200), export_input_hash: fakeSha('exp' + dp) },
      ...extra,
    };
    a.created_at = dt(-220);
    answers.push(a);
    return a;
  };

  const mkAnswer = (q: Question, e: CatalogEntry | { dp: string; value_type: Answer['value_type']; unit?: string; value: string | boolean }, o: Partial<Answer>): Answer => {
    const a: Answer = {
      ...base('ans_', 2, 'agent:approval-router', -1),
      datapoint_id: `${e.dp}@1.0.0`,
      value: e.value,
      value_type: e.value_type,
      unit: e.unit,
      period: period2025,
      boundary: 'organization',
      customer_scope: [oemA.id],
      question_refs: [q.id],
      evidence_refs: [],
      calculation_ref: null,
      origin: 'generated',
      answer_key: fakeSha(q.id),
      status: 'in_review',
      is_estimate: false,
      legal_commitment: false,
      ...o,
    };
    if (a.value_type === 'text') a.text_lang = 'de';
    answers.push(a);
    return a;
  };

  const mkApproval = (a: Answer, req: Request, level: RiskLevel, tier?: string): Approval => {
    const roles: Role[] = requiredRolesFor(level, { legal_commitment: a.legal_commitment, customer_tier: tier });
    const ap: Approval = {
      ...base('apr_', 1, 'agent:approval-router', -1),
      subject_ref: ref(a.id, a.version),
      request_id: req.id,
      risk_level: level,
      required_role: roles,
      assignee: roles.length === 1 && roles[0] === 'executive' ? USERS.executive.id : USERS.compliance_owner.id,
      policy_version: POLICY_VERSION,
      status: 'pending',
    };
    approvals.push(ap);
    return ap;
  };

  // ───── Request 1：100 题 ─────
  const sectionCounter: Record<string, number> = {};
  const nid = (s: string) => {
    sectionCounter[s] = (sectionCounter[s] ?? 0) + 1;
    return `${s}${sectionCounter[s]}`;
  };
  const isCert = (dp: string) => dp.startsWith('dp:cert.');
  const certValidity: Record<string, number> = { 'dp:cert.iso14001.valid_until': 73, 'dp:cert.iso9001.valid_until': 345 };

  // L0：1–61（原样复用历史已提交答案）
  for (let i = 1; i <= 61; i++) {
    const e = CATALOG[(i - 1) % CATALOG.length];
    const v = SITE_VARIANTS[Math.floor((i - 1) / CATALOG.length) % SITE_VARIANTS.length];
    const text: I18nText = { de: e.text.de + v.de, en: e.text.en + v.en, zh: e.text.zh + v.zh };
    const q = mkQuestion(req1, i, `${e.section}.${nid(e.section)}`, text, e.value_type === 'number' ? 'number' : e.value_type === 'bool' ? 'bool' : e.value_type === 'date' ? 'date' : 'text');
    const hist = mkId('ans_', next());
    const validDays = certValidity[e.dp];
    const ev = mkEvidence(
      {
        source_type: isCert(e.dp) ? 'document' : 'historical_answer',
        fact_key: e.fact_key,
        datapoint_id: `${e.dp}@1.0.0`,
        extracted_value: typeof e.value === 'boolean' ? e.value : e.value,
        value_type: e.value_type,
        unit: e.unit,
        period: period2025,
        valid_until: validDays !== undefined ? day(validDays) : undefined,
        boundary: v.boundary,
      },
      isCert(e.dp) ? 'Zertifikat (PDF)' : 'Historische Antwort CDP 2025',
    );
    const a = mkAnswer(q, e, { boundary: v.boundary, reused_from: ref(hist, 1), evidence_refs: [ref(ev.id, ev.version)], risk_level: 'L0', value: validDays !== undefined ? day(validDays) : e.value });
    mkApproval(a, req1, 'L0', tiers[oemA.id]);
    if (i === 12) answerHead[a.id] = 3; // stale：审批期间出现新版本（03 §3.8）
  }

  // L1：62–87（verified 证据直接取值）
  const L1_LIST = CATALOG.filter((e) => !e.dp.startsWith('dp:env.ghg.') && e.value_type !== 'text');
  let waterAnswer: Answer | undefined;
  let expiringAnswer: Answer | undefined;
  L1_LIST.forEach((e, k) => {
    const i = 62 + k;
    const v = SITE_VARIANTS[2];
    const q = mkQuestion(req1, i, `${e.section}.${nid(e.section)}`, { de: e.text.de + v.de, en: e.text.en + v.en, zh: e.text.zh + v.zh }, e.value_type === 'number' ? 'number' : e.value_type === 'bool' ? 'bool' : e.value_type === 'date' ? 'date' : 'enum');
    const expiring = e.dp === 'dp:cert.iso9001.valid_until';
    const validDays = expiring ? 22 : certValidity[e.dp];
    const value = validDays !== undefined ? day(validDays) : e.value;
    const ev = mkEvidence(
      {
        source_type: k % 4 === 0 ? 'erp' : 'document',
        fact_key: e.fact_key,
        datapoint_id: `${e.dp}@1.0.0`,
        extracted_value: value,
        value_type: e.value_type,
        unit: e.unit,
        period: period2025,
        valid_until: validDays !== undefined ? day(validDays) : undefined,
        boundary: v.boundary,
        confidence: 0.93,
      },
      k % 4 === 0 ? 'SAP B1 Auswertung' : expiring ? 'ISO-9001-Zertifikat Werk Reutlingen' : 'Nachweis (PDF)',
    );
    const a = mkAnswer(q, e, { boundary: v.boundary, evidence_refs: [ref(ev.id, ev.version)], risk_level: 'L1', value });
    mkApproval(a, req1, 'L1', tiers[oemA.id]);
    if (e.dp === 'dp:env.water.withdrawal_total') waterAnswer = a;
    if (expiring) expiringAnswer = a;
  });

  // Calculations（03 §5.6.8 Scope 2 market-based 原样）
  const evElec = mkEvidence(
    { source_type: 'document', fact_key: 'activity.electricity.purchased', extracted_value: '4820', value_type: 'number', unit: 'MWh', period: period2025, confidence: 0.96 },
    'Jahresabrechnung Strom 2025, EnBW, S. 2: Verbrauch Wirkarbeit',
  );
  const evGoo = mkEvidence(
    { source_type: 'document', fact_key: 'activity.electricity.contractual_renewable', extracted_value: '1200', value_type: 'number', unit: 'MWh', period: period2025, confidence: 0.88, verification: { method: 'closed_question', cq_ref: mkId('cq_', next()), by: USERS.compliance_owner.id } },
    'Herkunftsnachweis-Entwertung (UBA HKNR), bestätigt durch P. Huber',
  );
  const evPv = mkEvidence({ source_type: 'erp', fact_key: 'activity.electricity.onsite_pv', extracted_value: '410', value_type: 'number', unit: 'MWh', period: period2025 }, 'Zählerdaten PV-Anlage Ulm');
  const calc = (o: Partial<Calculation> & Pick<Calculation, 'method_id' | 'methodology_version' | 'factor_set_version' | 'inputs' | 'result'>): Calculation => {
    const c: Calculation = {
      ...base('calc_', 1, 'agent:calc-service', -3),
      engine_version: 'belegt-calc@0.3.0',
      factors_used: [],
      input_hash: fakeSha('in' + o.method_id + next()),
      output_hash: fakeSha('out' + o.method_id + next()),
      boundary: 'organization',
      period: period2025,
      customer_scope: 'all',
      status: 'computed',
      ...o,
    };
    calculations.push(c);
    return c;
  };
  const calcS2m = calc({
    method_id: 'ghg.scope2.market_based',
    methodology_version: 'ghg-scope2-market@1.0.0',
    factor_set_version: 'fs:aib-residual-mix@2025.1',
    inputs: [
      { name: 'electricity_contractual_renewable', ref: ref(evGoo.id, 2), value: '1200', unit: 'MWh' },
      { name: 'electricity_purchased', ref: ref(evElec.id, 2), value: '4820', unit: 'MWh' },
    ],
    factors_used: [
      { factor_key: 'residual_mix.electricity.DE', value: '0.56', unit: 'tCO2e/MWh', source_row_id: 'DE-2024-RM' },
      { factor_key: 'contractual.electricity.goo', value: '0', unit: 'tCO2e/MWh', source_row_id: 'GOO-ZERO' },
    ],
    params: { consolidation: 'operational_control', grid_region: 'DE' },
    result: { value: '2027.2', unit: 'tCO2e', rounding: { mode: 'half_even', digits: 1 } },
    breakdown: [
      { name: 'residual_mix_consumption', value: '3620', unit: 'MWh' },
      { name: 'emissions_unrounded', value: '2027.2', unit: 'tCO2e' },
    ],
    input_hash: 'sha256:fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446',
    output_hash: 'sha256:12a0337f066e03cf2dfa6be6bc7263bf2d5ac3b4a476a1997464a7b24ae7611c',
  });
  const calcS2l = calc({
    method_id: 'ghg.scope2.location_based',
    methodology_version: 'ghg-scope2-location@1.0.0',
    factor_set_version: 'fs:uba-strommix@2026.03',
    inputs: [{ name: 'electricity_purchased', ref: ref(evElec.id, 2), value: '4820', unit: 'MWh' }],
    factors_used: [{ factor_key: 'grid.electricity.DE', value: '0.363', unit: 'tCO2e/MWh', source_row_id: 'DE-2025-LB' }],
    result: { value: '1749.7', unit: 'tCO2e', rounding: { mode: 'half_even', digits: 1 } },
    breakdown: [{ name: 'emissions_unrounded', value: '1749.66', unit: 'tCO2e' }],
    customer_scope: [oemB.id],
  });
  const calcRen = calc({
    method_id: 'energy.renewable_share',
    methodology_version: 'energy-share@1.0.0',
    factor_set_version: 'fs:uba-strommix@2026.03',
    inputs: [
      { name: 'electricity_contractual_renewable', ref: ref(evGoo.id, 2), value: '1200', unit: 'MWh' },
      { name: 'electricity_onsite_renewable', ref: ref(evPv.id, 2), value: '410', unit: 'MWh' },
      { name: 'electricity_purchased', ref: ref(evElec.id, 2), value: '4820', unit: 'MWh' },
    ],
    result: { value: '33.4', unit: '%', rounding: { mode: 'half_even', digits: 1 } },
  });
  const calcTotal = calc({
    method_id: 'energy.consumption_total',
    methodology_version: 'energy-sum@1.0.0',
    factor_set_version: 'fs:uba-strommix@2026.03',
    inputs: [
      { name: 'electricity_purchased', ref: ref(evElec.id, 2), value: '4820', unit: 'MWh' },
      { name: 'onsite_pv', ref: ref(evPv.id, 2), value: '410', unit: 'MWh' },
    ],
    result: { value: '7682', unit: 'MWh', rounding: { mode: 'half_even', digits: 0 } },
  });
  const calcInt = calc({
    method_id: 'energy.intensity_per_output',
    methodology_version: 'energy-intensity@1.0.0',
    factor_set_version: 'fs:uba-strommix@2026.03',
    inputs: [{ name: 'energy_total', ref: ref(evElec.id, 2), value: '7682', unit: 'MWh' }],
    result: { value: '0.81', unit: 'MWh/t', rounding: { mode: 'half_even', digits: 2 } },
  });

  // L2：88–95
  const evEst = mkEvidence({ source_type: 'document', fact_key: 'activity.water.discharge', extracted_value: '6170', value_type: 'number', unit: 'm3', period: { start: '2025-01-01', end: '2025-10-31' } }, 'Abwasserbescheid, 10 Monatsablesungen');
  const evPack = mkEvidence({ source_type: 'erp', fact_key: 'activity.waste.packaging', extracted_value: '53', value_type: 'number', unit: 't', period: { start: '2025-01-01', end: '2025-10-31' } }, 'Entsorgungsnachweise Verpackung Jan–Okt');
  const L2_SPECS: { native: string; dp: string; text: I18nText; value_type: Answer['value_type']; unit?: string; value: string; calc?: Calculation; est?: string; ev?: Evidence; drafted?: boolean }[] = [
    { native: 'B.9', dp: 'dp:energy.consumption.renewable_share', text: { de: 'Anteil erneuerbarer Energie am Stromverbrauch 2025', en: 'Renewable share of electricity 2025', zh: '2025 年可再生电力占比' }, value_type: 'number', unit: '%', value: '33.4', calc: calcRen },
    { native: 'B.10', dp: 'dp:energy.intensity.per_output', text: { de: 'Energieintensität je Tonne Ausbringung 2025', en: 'Energy intensity per tonne of output 2025', zh: '2025 年单位产出能耗强度' }, value_type: 'number', unit: 'MWh/t', value: '0.81', calc: calcInt },
    { native: 'D.9', dp: 'dp:env.water.discharge_total', text: { de: 'Abwassermenge 2025', en: 'Water discharge 2025', zh: '2025 年排水量' }, value_type: 'number', unit: 'm3', value: '7400', ev: evEst, est: 'Zeitanteilige Hochrechnung aus 10 Monatsablesungen' },
    { native: 'F.6', dp: 'dp:env.ems.description', text: { de: 'Beschreiben Sie Ihr Umweltmanagementsystem.', en: 'Describe your environmental management system.', zh: '请描述贵公司的环境管理体系。' }, value_type: 'text', value: 'Zertifiziertes Umweltmanagementsystem nach ISO 14001 mit jährlicher Managementbewertung, interner Auditplanung und Zielverfolgung je Werk.', drafted: true },
    { native: 'C.7', dp: 'dp:env.ghg_reduction.measures', text: { de: 'Welche Maßnahmen zur Emissionsminderung setzen Sie um?', en: 'Which emission reduction measures do you implement?', zh: '贵公司实施了哪些减排措施？' }, value_type: 'text', value: 'Umstellung der Druckluftversorgung auf drehzahlgeregelte Kompressoren, Bezug von Strom mit Herkunftsnachweisen, PV-Eigenerzeugung am Standort Ulm.', drafted: true },
    { native: 'D.10', dp: 'dp:env.waste.packaging', text: { de: 'Verpackungsabfälle 2025', en: 'Packaging waste 2025', zh: '2025 年包装废弃物' }, value_type: 'number', unit: 't', value: '64', ev: evPack, est: 'Hochrechnung Jan–Okt auf 12 Monate' },
    { native: 'B.11', dp: 'dp:energy.consumption.total', text: { de: 'Gesamtenergieverbrauch 2025', en: 'Total energy consumption 2025', zh: '2025 年总能耗' }, value_type: 'number', unit: 'MWh', value: '7682', calc: calcTotal },
    { native: 'I.5', dp: 'dp:supply.due_diligence.process', text: { de: 'Beschreiben Sie Ihren Sorgfaltsprozess in der Lieferkette.', en: 'Describe your supply-chain due diligence process.', zh: '请描述贵公司的供应链尽职调查流程。' }, value_type: 'text', value: 'Risikoanalyse aller direkten Lieferanten, Lieferantenkodex als Vertragsbestandteil, anlassbezogene Audits und Abhilfemaßnahmen.', drafted: true },
  ];
  L2_SPECS.forEach((s, k) => {
    const q = mkQuestion(req1, 88 + k, s.native, s.text, s.value_type === 'number' ? 'number' : 'text');
    const a = mkAnswer(q, { dp: s.dp, value_type: s.value_type, unit: s.unit, value: s.value }, {
      risk_level: 'L2',
      calculation_ref: s.calc ? ref(s.calc.id, 1) : null,
      evidence_refs: s.ev ? [ref(s.ev.id, s.ev.version)] : [],
      is_estimate: !!s.est,
      estimate_method: s.est,
      created_by: s.drafted ? 'agent:answer-drafter' : 'agent:calc-service',
      version: 1,
      supersedes: null,
      drafting_note: s.drafted ? { de: 'KI-Entwurf auf Basis früherer Antworten und Managementhandbuch.', en: 'AI draft based on prior answers and management manual.', zh: '基于历史答案与管理手册的 AI 草稿。' } : undefined,
    });
    mkApproval(a, req1, 'L2', tiers[oemA.id]);
  });

  // 96：映射待确认（0.60–0.85 → single_choice 卡片）
  const q96 = mkQuestion(req1, 96, 'C8.2a', { de: 'Anteil erneuerbarer Energie (gesamt, inkl. Wärme)', en: 'Share of renewable energy (total, incl. heat)', zh: '可再生能源占比（总计，含热力）' }, 'number', 'parsed');
  const mapIds = [mkId('map_', next()), mkId('map_', next()), mkId('map_', next())];

  // L3：97–98
  const q97 = mkQuestion(req1, 97, 'C7.3', { de: 'Scope-2-Emissionen 2025 (market-based)', en: 'Scope 2 emissions 2025 (market-based)', zh: '2025 年范围二排放（基于市场）' }, 'number');
  const a97 = mkAnswer(q97, { dp: 'dp:env.ghg.scope2_market_based', value_type: 'number', unit: 'tCO2e', value: '2027.2' }, {
    risk_level: 'L3',
    customer_scope: 'all',
    consolidation: 'operational_control',
    evidence_refs: [ref(evElec.id, 2), ref(evGoo.id, 2)],
    calculation_ref: ref(calcS2m.id, 1),
    datapoint_id: 'dp:env.ghg.scope2_market_based@1.1.0',
    version: 3,
    supersedes: null,
  });
  mkApproval(a97, req1, 'L3', tiers[oemA.id]);
  const q98 = mkQuestion(req1, 98, 'K.4', { de: 'Lieferantenkodex §4: Der Lieferant sichert zu, die Anforderungen des Kodex in seiner Lieferkette durchzusetzen.', en: 'Supplier code §4: The supplier warrants that it enforces the code requirements in its supply chain.', zh: '供应商准则 §4：供应商保证在其供应链中落实本准则要求。' }, 'bool');
  const a98 = mkAnswer(q98, { dp: 'dp:gov.supplier_code.commitment', value_type: 'bool', value: true }, { risk_level: 'L3', legal_commitment: true, created_by: 'agent:answer-drafter' });
  mkApproval(a98, req1, 'L3', tiers[oemA.id]);

  // 99、100：被 blocking 冲突挡住的草稿（RISK-00：不可进入 in_review）
  const q99 = mkQuestion(req1, 99, 'C6.1', { de: 'Scope-1-Emissionen 2025', en: 'Scope 1 emissions 2025', zh: '2025 年范围一排放' }, 'number');
  const evS1 = mkEvidence({ source_type: 'document', fact_key: 'ghg.scope1', extracted_value: '1820', value_type: 'number', unit: 'tCO2e', period: period2025 }, 'THG-Bilanz 2025 (operative Kontrolle), S. 4');
  const a99 = mkAnswer(q99, { dp: 'dp:env.ghg.scope1_total', value_type: 'number', unit: 'tCO2e', value: '1820' }, { status: 'draft', version: 1, supersedes: null, consolidation: 'operational_control', evidence_refs: [ref(evS1.id, 2)], created_by: 'agent:calc-service' });
  const histB = historicalAnswer('dp:env.ghg.scope1_total', '1790', 'tCO2e', [oemB.id], req7.id, { consolidation: 'equity_share' });
  const q100 = mkQuestion(req1, 100, 'B.12', { de: 'Erdgasverbrauch 2025 (Heizwert)', en: 'Natural gas consumption 2025 (net calorific value)', zh: '2025 年天然气消耗（低位热值）' }, 'number');
  const evGas = mkEvidence({ source_type: 'document', fact_key: 'activity.fuel.natural_gas', extracted_value: '2140', value_type: 'number', unit: 'MWh', period: period2025 }, 'Gasrechnung 2025, Stadtwerke Ulm, S. 1');
  const a100 = mkAnswer(q100, { dp: 'dp:energy.consumption.natural_gas', value_type: 'number', unit: 'MWh', value: '2140' }, { status: 'draft', version: 1, supersedes: null, evidence_refs: [ref(evGas.id, 2)], created_by: 'agent:evidence-miner' });
  const histA = historicalAnswer('dp:energy.consumption.natural_gas', '2085', 'MWh', [oemA.id], req8.id);

  // ───── Request 2：CDP（OEM-B） ─────
  const r2specs: { level: RiskLevel; native: string; dp: string; text: I18nText; value_type: Answer['value_type']; unit?: string; value: string | boolean; calc?: Calculation; drafted?: boolean }[] = [
    { level: 'L1', native: 'C0.1', dp: 'dp:org.employees.headcount', text: { de: 'Mitarbeitende (FTE)', en: 'Employees (FTE)', zh: '员工（全职当量）' }, value_type: 'number', unit: '1', value: '380' },
    { level: 'L1', native: 'C8.2', dp: 'dp:energy.consumption.electricity_total', text: { de: 'Stromverbrauch gesamt (MWh)', en: 'Total electricity consumption (MWh)', zh: '总用电量（MWh）' }, value_type: 'number', unit: 'MWh', value: '4820' },
    { level: 'L1', native: 'C1.1', dp: 'dp:gov.board_oversight.climate', text: { de: 'Aufsicht über Klimathemen auf Geschäftsführungsebene?', en: 'Board-level oversight of climate issues?', zh: '是否在管理层层面监督气候议题？' }, value_type: 'bool', value: true },
    { level: 'L2', native: 'C8.2e', dp: 'dp:energy.consumption.renewable_share', text: { de: 'Anteil erneuerbarer Strom', en: 'Renewable electricity share', zh: '可再生电力占比' }, value_type: 'number', unit: '%', value: '33.4', calc: calcRen },
    { level: 'L2', native: 'C4.3b', dp: 'dp:env.ghg_reduction.initiatives', text: { de: 'Initiativen zur Emissionsminderung', en: 'Emission reduction initiatives', zh: '减排举措' }, value_type: 'text', value: 'Effizienzprogramm Druckluft, Herkunftsnachweise für Strombezug, PV-Ausbau Ulm.', drafted: true },
    { level: 'L3', native: 'C6.3', dp: 'dp:env.ghg.scope2_location_based', text: { de: 'Scope-2-Emissionen (location-based)', en: 'Scope 2 emissions (location-based)', zh: '范围二排放（基于位置）' }, value_type: 'number', unit: 'tCO2e', value: '1749.7', calc: calcS2l },
  ];
  r2specs.forEach((s, k) => {
    const q = mkQuestion(req2, k + 1, s.native, s.text, s.value_type === 'number' ? 'number' : s.value_type === 'bool' ? 'bool' : 'text');
    const ev =
      s.level === 'L1'
        ? mkEvidence({ source_type: 'document', fact_key: `cdp.${s.native}`, extracted_value: typeof s.value === 'boolean' ? s.value : s.value, value_type: s.value_type, unit: s.unit, period: period2025 }, 'Nachweis (PDF)')
        : undefined;
    const a = mkAnswer(q, { dp: s.dp, value_type: s.value_type, unit: s.unit, value: s.value }, {
      customer_scope: [oemB.id],
      risk_level: s.level,
      evidence_refs: ev ? [ref(ev.id, 2)] : s.calc ? s.calc.inputs.map((i) => i.ref) : [],
      calculation_ref: s.calc ? ref(s.calc.id, 1) : null,
      created_by: s.drafted ? 'agent:answer-drafter' : 'agent:approval-router',
    });
    mkApproval(a, req2, s.level, tiers[oemB.id]);
  });
  for (let k = r2specs.length + 1; k <= 24; k++) {
    const e = CATALOG[(k * 7) % CATALOG.length];
    mkQuestion(req2, k, `C${k}.0`, e.text, e.value_type === 'number' ? 'number' : 'text', 'answered');
  }

  // ───── 冲突（06 §2.2） ─────
  const mkConflict = (o: Omit<Conflict, keyof GraphBase>): Conflict => {
    const c: Conflict = { ...base('cfl_', 1, 'agent:consistency-auditor', -1), ...o };
    conflicts.push(c);
    return c;
  };
  mkConflict({
    rule_id: 'CR-001',
    severity: 'blocking',
    datapoint_id: 'dp:env.ghg.scope1_total',
    subject_refs: [ref(a99.id, 1), ref(histB.id, 1)],
    detail: { values: [{ ref: ref(a99.id, 1), value: '1820', unit: 'tCO2e' }, { ref: ref(histB.id, 1), value: '1790', unit: 'tCO2e' }], delta: '30' },
    explanation: {
      de: 'Der Unterschied entsteht durch die Konsolidierungsmethode: OEM-A operative Kontrolle, OEM-B Equity-Share (Vorjahresabgabe).',
      en: 'The difference stems from the consolidation method: OEM-A operational control, OEM-B equity share (prior submission).',
      zh: '差异来自合并方法：OEM-A 采用运营控制，OEM-B 采用股权比例（上次提交）。',
    },
    request_id: req1.id,
    status: 'open',
  });
  mkConflict({
    rule_id: 'CR-005',
    severity: 'blocking',
    datapoint_id: 'dp:energy.consumption.natural_gas',
    subject_refs: [ref(a100.id, 1), ref(histA.id, 1)],
    detail: { values: [{ ref: ref(a100.id, 1), value: '2140', unit: 'MWh' }, { ref: ref(histA.id, 1), value: '2085', unit: 'MWh' }], delta: '55' },
    explanation: {
      de: 'Für denselben Kunden und Zeitraum wurde bereits ein abweichender Wert eingereicht. Möglicherweise Brennwert statt Heizwert.',
      en: 'A different value was already submitted for the same customer and period. Possibly gross instead of net calorific value.',
      zh: '同一客户、同一期间已提交过不同的值，可能混用了高位与低位热值。',
    },
    request_id: req1.id,
    status: 'open',
  });
  if (waterAnswer) {
    const prev = historicalAnswer('dp:env.water.withdrawal_total', '6700', 'm3', [oemA.id], req8.id, { period: { start: '2024-01-01', end: '2024-12-31' } });
    mkConflict({
      rule_id: 'CR-002',
      severity: 'warning',
      datapoint_id: 'dp:env.water.withdrawal_total',
      subject_refs: [ref(waterAnswer.id, 2), ref(prev.id, 1)],
      detail: { values: [{ ref: ref(waterAnswer.id, 2), value: '9050', unit: 'm3' }, { ref: ref(prev.id, 1), value: '6700', unit: 'm3' }], delta: '0.35', threshold: '0.30' },
      explanation: { de: 'Veränderung gegenüber Vorjahr über Schwellenwert; Erläuterung erforderlich.', en: 'Year-on-year change above threshold; explanation required.', zh: '同比变化超过阈值，需填写说明。' },
      request_id: req1.id,
      status: 'open',
    });
  }
  if (expiringAnswer) {
    mkConflict({
      rule_id: 'CR-004',
      severity: 'warning',
      datapoint_id: 'dp:cert.iso9001.valid_until',
      subject_refs: [ref(expiringAnswer.id, 2)],
      detail: { values: [{ ref: ref(expiringAnswer.id, 2), value: day(22) }], threshold: '30' },
      explanation: { de: 'Zertifikat läuft vor der Frist des Fragebogens ab.', en: 'Certificate expires before the questionnaire deadline.', zh: '证书在问卷截止日前到期。' },
      request_id: req1.id,
      status: 'open',
    });
  }
  const resolvedHist = historicalAnswer('dp:env.waste.total', '980', 't', [oemB.id], req7.id);
  mkConflict({
    rule_id: 'CR-003',
    severity: 'blocking',
    datapoint_id: 'dp:env.waste.total',
    subject_refs: [ref(resolvedHist.id, 1)],
    detail: { values: [{ ref: ref(resolvedHist.id, 1), value: '980', unit: 't' }] },
    request_id: req7.id,
    status: 'resolved',
    resolution: { kind: 'corrected', note: 'Teilmengen korrigiert (gefährlich 120 t + nicht gefährlich 860 t).', approval_ref: ref(mkId('apr_', next()), 2), resolved_by: USERS.compliance_owner.id },
  });

  // ───── ClosedQuestion（01 §3.2 示例 5 张 + 补充） ─────
  const evUlm = mkEvidence({ source_type: 'document', fact_key: 'activity.electricity.purchased', extracted_value: '1284300', value_type: 'number', unit: 'kWh', period: period2025, boundary: 'site:site_01JD0000000000000000000ULM', status: 'candidate', confidence: 0.78, verification: undefined }, 'Rechnung EnBW S. 2, markiert: Wirkarbeit gesamt');
  const evDelivery = mkEvidence({ source_type: 'erp', fact_key: 'sales.delivered_mass.oem_a', extracted_value: '2300', value_type: 'number', unit: 't', period: period2025, status: 'candidate', confidence: 0.84, verification: undefined }, 'SAP B1 Lieferungen an OEM-A 2025');
  const cq = (o: Omit<ClosedQuestion, keyof GraphBase>, by = 'agent:evidence-miner'): ClosedQuestion => ({ ...base('cq_', 1, by, -2), ...o });
  const closedQuestions: ClosedQuestion[] = [
    cq({ target_ref: ref(evUlm.id, 2), request_id: req1.id, kind: 'numeric_confirm', prompt: { de: 'Stimmt der Stromverbrauch 2025 für Werk Ulm: 1.284.300 kWh (EnBW-Rechnung S. 2)?', en: 'Is 1,284,300 kWh the correct 2025 electricity use for the Ulm plant (EnBW invoice p. 2)?', zh: '乌尔姆工厂 2025 年用电量 1,284,300 kWh（EnBW 账单第 2 页）是否正确？' }, proposed_value: { value: '1284300', unit: 'kWh', evidence_ref: ref(evUlm.id, 2) }, context_refs: [ref(evUlm.id, 2)], assignee_role: 'contributor', assignee: USERS.contributor.id, expires_at: dt(3, 16), status: 'open' }),
    cq({ target_ref: ref(mapIds[0], 1), request_id: req1.id, kind: 'single_choice', prompt: { de: 'Frage C8.2a („Anteil erneuerbarer Energie") entspricht welchem Datenpunkt?', en: 'Question C8.2a ("Share of renewable energy") maps to which data point?', zh: '题目 C8.2a（"可再生能源占比"）对应哪个数据点？' }, options: [
      { key: 'A', label: { de: 'dp:energy.consumption.renewable_share', en: 'dp:energy.consumption.renewable_share', zh: 'dp:energy.consumption.renewable_share' }, ref: ref(mapIds[0], 1) },
      { key: 'B', label: { de: 'Strom aus Eigenerzeugung (dp:energy.consumption.onsite_renewable)', en: 'Self-generated electricity (dp:energy.consumption.onsite_renewable)', zh: '自发电（dp:energy.consumption.onsite_renewable）' }, ref: ref(mapIds[1], 1) },
      { key: 'C', label: { de: 'Keiner davon', en: 'None of these', zh: '都不是' } },
    ], context_refs: [ref(q96.id, 2)], assignee_role: 'compliance_owner', expires_at: dt(4, 16), status: 'open' }, 'agent:guard'),
    cq({ target_ref: ref(mkId('ev_', next()), 1), request_id: req3.id, kind: 'yes_no', prompt: { de: 'Enthalten die Halter 4711 SVHC-Stoffe über 0,1 % (Gewicht)? Grundlage: SDB Lieferant X, Stand 03/2026', en: 'Do brackets 4711 contain SVHCs above 0.1 % w/w? Basis: supplier X SDS, 03/2026', zh: '支架 4711 是否含有超过 0.1 %（质量比）的 SVHC？依据：供应商 X 安全数据表，2026 年 3 月版' }, assignee_role: 'contributor', assignee: USERS.contributor.id, expires_at: dt(5, 16), status: 'open' }),
    cq({ target_ref: ref(mkId('ev_', next()), 2), request_id: req1.id, kind: 'file_request', prompt: { de: `Das ISO-14001-Zertifikat läuft am ${day(73).split('-').reverse().join('.')} ab. Liegt ein neues vor? Bitte hochladen.`, en: `The ISO 14001 certificate expires on ${day(73)}. Do you have a renewed one? Please upload.`, zh: `ISO 14001 证书将于 ${day(73)} 到期。是否已有新证书？请上传。` }, assignee_role: 'compliance_owner', expires_at: dt(10, 16), status: 'open' }, 'agent:consistency-auditor'),
    cq({ target_ref: ref(calcS2m.id, 1), request_id: req1.id, kind: 'single_choice', prompt: { de: 'Welche Methode für Scope 2 gegenüber OEM-A?', en: 'Which Scope 2 method for OEM-A?', zh: '对 OEM-A 采用哪种范围二方法？' }, options: [
      { key: 'A', label: { de: 'location-based', en: 'location-based', zh: '基于位置（location-based）' } },
      { key: 'B', label: { de: 'market-based (mit Herkunftsnachweisen)', en: 'market-based (with guarantees of origin)', zh: '基于市场（含来源证明）' } },
    ], assignee_role: 'compliance_owner', expires_at: dt(6, 16), status: 'open' }, 'agent:calc-service'),
    cq({ target_ref: ref(evDelivery.id, 2), request_id: req4.id, kind: 'numeric_confirm', prompt: { de: 'Stimmt die Liefermenge an OEM-A 2025: 2.300 t (SAP-Auswertung)?', en: 'Is 2,300 t the correct 2025 delivered mass to OEM-A (SAP report)?', zh: '2025 年向 OEM-A 交付量 2,300 t（SAP 报表）是否正确？' }, proposed_value: { value: '2300', unit: 't', evidence_ref: ref(evDelivery.id, 2) }, assignee_role: 'sales', assignee: USERS.sales.id, expires_at: dt(2, 16), status: 'open' }),
    cq({ target_ref: ref(mkId('ev_', next()), 1), request_id: req3.id, kind: 'yes_no', prompt: { de: 'Hat Kocaeli Çelik den Lieferantenkodex 2026 unterzeichnet?', en: 'Has Kocaeli Çelik signed the 2026 supplier code?', zh: 'Kocaeli Çelik 是否已签署 2026 版供应商准则？' }, assignee_role: 'contributor', assignee: USERS.contributor.id, expires_at: dt(7, 16), status: 'open' }),
    cq({ target_ref: ref(mkId('ev_', next()), 1), request_id: req5.id, kind: 'yes_no', prompt: { de: 'Wird am Standort Reutlingen Kältemittel R-410A eingesetzt?', en: 'Is refrigerant R-410A used at the Reutlingen site?', zh: '罗伊特林根工厂是否使用 R-410A 制冷剂？' }, assignee_role: 'contributor', expires_at: dt(-1, 16), status: 'expired' }),
  ];

  // ───── SupplierRequest（03 §3.10，08 §4.1 chase-policy@1.0.0） ─────
  const sreq = (cpObj: Counterparty, language: string, status: SupplierRequestView['supplier_request']['status'], sentDay: number | null, reminders: number, gap: string, historyStates: { s: SupplierRequestView['supplier_request']['status']; d: number }[]): SupplierRequestView => {
    const b = base('sreq_', historyStates.length, 'agent:upstream-chaser', historyStates[historyStates.length - 1].d);
    const family = language === 'tr' ? 'tr' : language === 'zh-Hans' ? 'zh-Hans' : language;
    const sr: SupplierRequestView['supplier_request'] = {
      ...b,
      counterparty_id: cpObj.id,
      datapoint_ids: ['dp:prod.cbam.see_direct@1.0.0', 'dp:prod.cbam.see_indirect@1.0.0'],
      product_ids: [mkId('prod_', next())],
      period: period2025,
      language,
      template_id: `stpl:cbam_see_request.${family}@1.0.0`,
      channel: 'supplier_portal',
      portal_token_hash: fakeSha('tok' + cpObj.id),
      due_date: sentDay !== null ? day(sentDay + 21) : day(30),
      reminder_count: reminders,
      last_sent_at: sentDay !== null ? dt(historyStates.filter((h) => h.s === 'sent' || h.s === 'reminded' || h.s === 'escalated').slice(-1)[0]?.d ?? sentDay, 9) : undefined,
      status,
    };
    return {
      supplier_request: sr,
      supplier_name: cpObj.legal_name,
      ui_cost_gap_eur: gap,
      history: historyStates.map((h, i) => ({
        version: i + 1,
        status: h.s,
        created_at: dt(h.d, 9),
        reminder_count: historyStates.slice(0, i + 1).filter((x) => x.s === 'reminded' || x.s === 'escalated').length,
        created_by: h.s === 'sent' ? USERS.compliance_owner.id : h.s === 'responded' || h.s === 'reviewed' ? 'agent:supplier-reply-reviewer' : h.s === 'closed' ? USERS.compliance_owner.id : 'agent:upstream-chaser',
      })),
    };
  };
  const supplierRequests: SupplierRequestView[] = [
    sreq(supTR, 'tr', 'reminded', -9, 1, '63900', [ { s: 'draft', d: -11 }, { s: 'sent', d: -9 }, { s: 'reminded', d: -2 } ]),
    sreq(supCN, 'zh-Hans', 'closed', -30, 1, '21400', [ { s: 'draft', d: -32 }, { s: 'sent', d: -30 }, { s: 'reminded', d: -23 }, { s: 'responded', d: -19 }, { s: 'reviewed', d: -18 }, { s: 'closed', d: -17 } ]),
    sreq(supIT, 'it', 'escalated', -20, 3, '14800', [ { s: 'draft', d: -22 }, { s: 'sent', d: -20 }, { s: 'reminded', d: -13 }, { s: 'reminded', d: -6 }, { s: 'escalated', d: -1 } ]),
    sreq(supPL, 'pl', 'draft', null, 0, '9200', [ { s: 'draft', d: -1 } ]),
    sreq(supCZ, 'cs', 'expired', -40, 2, '3100', [ { s: 'draft', d: -42 }, { s: 'sent', d: -40 }, { s: 'reminded', d: -33 }, { s: 'reminded', d: -26 }, { s: 'expired', d: -19 } ]),
  ];

  // ───── 额外 Evidence（有效期预警，06 §7） ─────
  mkEvidence({ source_type: 'document', fact_key: 'mat.sds.supplier_x', extracted_value: 'SDB Stand 03/2026', value_type: 'text', valid_until: day(5) }, 'Sicherheitsdatenblatt Lieferant X');
  mkEvidence({ source_type: 'document', fact_key: 'cert.iso50001', extracted_value: true, value_type: 'bool', valid_until: day(50) }, 'ISO-50001-Zertifikat');
  mkEvidence({ source_type: 'supplier_response', fact_key: 'supply.declaration.brescia', extracted_value: 'Lieferantenerklärung 2025', value_type: 'text', valid_until: day(-3), status: 'expired', supplier_request_ref: supplierRequests[2].supplier_request.id }, 'Langzeit-Lieferantenerklärung Brescia Acciai');
  mkEvidence({ source_type: 'document', fact_key: 'cert.iatf16949', extracted_value: true, value_type: 'bool', valid_until: day(12) }, 'IATF-16949-Zertifikat Werk Ulm');

  return {
    today,
    counterparties,
    tiers,
    requests,
    staticProgress,
    questions,
    answers,
    answerHead,
    evidence,
    calculations,
    approvals,
    conflicts,
    closedQuestions,
    supplierRequests,
    factorSets,
    excerpts,
  };
}

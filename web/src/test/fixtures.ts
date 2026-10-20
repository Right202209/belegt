// 测试夹具：最小 InboxItem 构造器（字段名按 03）
import type { Answer, Approval, Conflict, Evidence, InboxItem, RiskLevel } from '../types';

let n = 0;
const base = (prefix: string, version = 1) => {
  n++;
  const id = `${prefix}${String(n).padStart(26, '0')}`;
  return { id, org_id: 'org_x', version, created_at: '2026-10-01T00:00:00Z', created_by: 'agent:approval-router', trust_zone: 'trusted' as const, provenance: 'trc_x', supersedes: null, schema_version: 'belegt.graph/2026-10', content_hash: 'sha256:0' };
};

export function evidence(o: Partial<Evidence> = {}): Evidence {
  return {
    ...base('ev_', 2),
    source_type: 'document',
    fact_key: 'activity.x',
    extracted_value: '1',
    value_type: 'number',
    unit: 'MWh',
    boundary: 'organization',
    locator: { blob_sha256: 'sha256:1', mime: 'application/pdf', page: 1 },
    valid_from: '2025-01-01',
    confidence: 0.95,
    status: 'verified',
    ...o,
  };
}

export function conflict(o: Partial<Conflict> = {}): Conflict {
  return { ...base('cfl_'), rule_id: 'CR-002', severity: 'warning', datapoint_id: 'dp:x', subject_refs: [], detail: { values: [] }, status: 'open', ...o };
}

export interface ItemOpts {
  level?: RiskLevel;
  request_id?: string;
  due?: string;
  reused?: boolean;
  legal?: boolean;
  estimate?: boolean;
  evidence?: Evidence[];
  conflicts?: Conflict[];
  stale?: boolean;
  tier?: 'tier_a' | 'tier_b' | 'tier_c';
}

export function item(o: ItemOpts = {}): InboxItem {
  const level = o.level ?? 'L0';
  const a = base('ans_', 2);
  const answer: Answer = {
    ...a,
    datapoint_id: `dp:test.metric.m${n}@1.0.0`,
    value: '10',
    value_type: 'number',
    unit: 'MWh',
    period: { start: '2025-01-01', end: '2025-12-31' },
    boundary: 'organization',
    customer_scope: ['cp_a'],
    origin: 'generated',
    answer_key: 'sha256:k',
    status: 'in_review',
    risk_level: level,
    is_estimate: !!o.estimate,
    legal_commitment: !!o.legal,
    reused_from: o.reused === false ? undefined : level === 'L0' ? 'ans_hist#v1' : undefined,
    evidence_refs: [],
  };
  const approval: Approval = {
    ...base('apr_'),
    subject_ref: `${answer.id}#v${answer.version}`,
    request_id: o.request_id ?? 'req_1',
    risk_level: level,
    required_role: level === 'L3' ? ['compliance_owner', 'executive'] : ['compliance_owner'],
    policy_version: 'approval-policy@1.0.0',
    status: 'pending',
  };
  const ev = o.evidence ?? [evidence()];
  answer.evidence_refs = ev.map((e) => `${e.id}#v${e.version}`);
  return {
    approval,
    answer,
    head_version: o.stale ? answer.version + 1 : answer.version,
    questions: [],
    evidence: ev,
    conflicts: o.conflicts ?? [],
    customer_tier: o.tier,
    request_title: 'Test',
    request_due_date: o.due ?? '2026-12-15',
  };
}

export const NOW = new Date('2026-10-19T08:00:00Z');

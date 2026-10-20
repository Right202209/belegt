// 覆盖 06 §5.1 批量一键批准条件、§4.3/§4.4 上限与角色、AC-403-2/3
import { describe, expect, it } from 'vitest';
import { evaluateBatch, exclusionReasons, recheckBeforeCommit, type BatchContext } from './batch';
import { canApprove, requiredRolesFor } from './policy';
import { conflict, evidence, item, NOW } from '../test/fixtures';
import type { RiskLevel, Role } from '../types';
import { MockDataSource } from '../api/mock/MockDataSource';
import { generateWorld } from '../api/mock/generate';

const ctx = (o: Partial<BatchContext> = {}): BatchContext => ({
  role: 'compliance_owner' as Role,
  now: NOW,
  viewed: new Set(),
  estimateAcked: new Set(),
  expandedLevels: new Set<RiskLevel>(['L0', 'L1', 'L2', 'L3']),
  ...o,
});

describe('evaluateBatch – 06 §5.1', () => {
  it('shows one-click approval for a clean L0 group', () => {
    const items = [item(), item(), item()];
    const ev = evaluateBatch('L0', items, ctx());
    expect(ev.showButton).toBe(true);
    expect(ev.eligible).toHaveLength(3);
    expect(ev.summary.count).toBe(3);
    expect(ev.summary.numbers_changed).toBe(0);
    expect(ev.summary.by_level).toEqual({ L0: 3 });
  });

  it('condition 1: excludes L3 from a batch while the rest proceeds (AC-403-2)', () => {
    const l3 = item({ level: 'L3' });
    const ev = evaluateBatch('L0', [item(), l3, item()], ctx());
    expect(ev.eligible).toHaveLength(2);
    expect(ev.excluded.find((x) => x.item === l3)?.reasons).toEqual(['l3_single']);
    expect(ev.showButton).toBe(true);
  });

  it('condition 1: legal_commitment never enters a batch (CR-042)', () => {
    const legal = item({ level: 'L1', legal: true });
    expect(exclusionReasons(legal, 'L1', ctx())).toContain('legal_commitment');
  });

  it('L3 group never offers batch approval', () => {
    const ev = evaluateBatch('L3', [item({ level: 'L3' })], ctx());
    expect(ev.showButton).toBe(false);
    expect(ev.blockers).toContain('batch_not_allowed');
  });

  it('condition 2: blocking conflict excludes at every level; warnings exclude L0/L1 and need a note at L2', () => {
    const blk = conflict({ severity: 'blocking', rule_id: 'CR-001' });
    expect(exclusionReasons(item({ conflicts: [blk] }), 'L0', ctx())).toContain('open_blocking_conflict');
    const warn = conflict({ severity: 'warning' });
    expect(exclusionReasons(item({ conflicts: [warn] }), 'L0', ctx())).toContain('open_conflict');
    expect(exclusionReasons(item({ level: 'L1', conflicts: [warn] }), 'L1', ctx())).toContain('open_conflict');
    expect(exclusionReasons(item({ level: 'L2', conflicts: [warn] }), 'L2', ctx())).toContain('open_warning_needs_note');
    const closed = conflict({ severity: 'blocking', status: 'resolved' });
    expect(exclusionReasons(item({ conflicts: [closed] }), 'L0', ctx())).toEqual([]);
  });

  it('condition 3: evidence must still be valid at the request due_date (CR-004/CR-013)', () => {
    const it1 = item({ due: '2026-12-15', evidence: [evidence({ valid_until: '2026-12-01' })] });
    expect(exclusionReasons(it1, 'L0', ctx())).toContain('evidence_invalid_at_due');
    const it2 = item({ due: '2026-12-15', evidence: [evidence({ valid_until: '2026-12-15' })] });
    expect(exclusionReasons(it2, 'L0', ctx())).not.toContain('evidence_invalid_at_due');
    const it3 = item({ evidence: [evidence({ status: 'expired' })] });
    expect(exclusionReasons(it3, 'L0', ctx())).toContain('evidence_invalid_at_due');
  });

  it('condition 4: stale head version is excluded', () => {
    expect(exclusionReasons(item({ stale: true }), 'L0', ctx())).toContain('stale_version');
  });

  it('L0 requires reused_from', () => {
    expect(exclusionReasons(item({ reused: false }), 'L0', ctx())).toContain('not_reused');
  });

  it('L1: evidence verified and valid > 30 days; group must be expanded', () => {
    const cand = item({ level: 'L1', evidence: [evidence({ status: 'candidate' })] });
    expect(exclusionReasons(cand, 'L1', ctx())).toContain('evidence_not_verified');
    const soon = item({ level: 'L1', due: '2026-10-30', evidence: [evidence({ valid_until: '2026-11-18' })] }); // 30 天
    expect(exclusionReasons(soon, 'L1', ctx())).toEqual(['evidence_expiring_soon']);
    const ok = item({ level: 'L1', due: '2026-10-30', evidence: [evidence({ valid_until: '2026-11-19' })] }); // 31 天
    expect(exclusionReasons(ok, 'L1', ctx())).toEqual([]);
    const ev = evaluateBatch('L1', [ok], ctx({ expandedLevels: new Set(['L0']) }));
    expect(ev.blockers).toContain('group_not_expanded');
    expect(ev.showButton).toBe(false);
  });

  it('L2: requires detail view, estimate acknowledgement and a single request', () => {
    const a = item({ level: 'L2', estimate: true, request_id: 'req_1' });
    const b = item({ level: 'L2', request_id: 'req_1' });
    let ev = evaluateBatch('L2', [a, b], ctx());
    expect(ev.eligible).toHaveLength(0);
    expect(ev.excluded.find((x) => x.item === a)?.reasons).toEqual(['detail_view_required', 'estimate_ack_required']);
    ev = evaluateBatch('L2', [a, b], ctx({ viewed: new Set([a.approval.id, b.approval.id]), estimateAcked: new Set([a.approval.id]) }));
    expect(ev.eligible).toHaveLength(2);
    expect(ev.showButton).toBe(true);
    expect(ev.summary.estimates).toBe(1);
    const c = item({ level: 'L2', request_id: 'req_2' });
    ev = evaluateBatch('L2', [a, b, c], ctx({ viewed: new Set([a.approval.id, b.approval.id, c.approval.id]), estimateAcked: new Set([a.approval.id]) }));
    expect(ev.blockers).toContain('mixed_requests');
    expect(ev.showButton).toBe(false);
  });

  it('condition 5: batch size limits L0 200 / L1 100 / L2 20', () => {
    const l1 = Array.from({ length: 120 }, () => item({ level: 'L1' }));
    const ev1 = evaluateBatch('L1', l1, ctx());
    expect(ev1.eligible).toHaveLength(100);
    expect(ev1.overflow).toBe(20);
    expect(ev1.max_items).toBe(100);
    const l2 = Array.from({ length: 25 }, () => item({ level: 'L2' }));
    const ev2 = evaluateBatch('L2', l2, ctx({ viewed: new Set(l2.map((i) => i.approval.id)) }));
    expect(ev2.eligible).toHaveLength(20);
    expect(ev2.overflow).toBe(5);
    const l0 = Array.from({ length: 205 }, () => item());
    expect(evaluateBatch('L0', l0, ctx()).eligible).toHaveLength(200);
  });

  it('condition 5: approver role must be permitted (AC-403-3)', () => {
    for (const role of ['sales', 'contributor', 'executive'] as Role[]) {
      const ev = evaluateBatch('L0', [item()], ctx({ role }));
      expect(ev.blockers).toContain('role');
      expect(ev.showButton).toBe(false);
    }
  });

  it('recheckBeforeCommit voids the batch when data changed in the window (06 §5.2)', () => {
    const a = item();
    expect(recheckBeforeCommit([a], NOW).ok).toBe(true);
    const changed = { ...a, head_version: a.answer.version + 1 };
    const r = recheckBeforeCommit([changed], NOW);
    expect(r.ok).toBe(false);
    expect(r.failing[0].reasons).toContain('stale_version');
    const withConflict = { ...a, conflicts: [conflict({ severity: 'blocking' })] };
    expect(recheckBeforeCommit([withConflict], NOW).ok).toBe(false);
  });
});

describe('policy – 06 §4.3', () => {
  it('L3 legal commitment to tier_a customer is executive-only', () => {
    expect(requiredRolesFor('L3', { legal_commitment: true, customer_tier: 'tier_a' })).toEqual(['executive']);
    expect(canApprove('compliance_owner', 'L3', { legal_commitment: true, customer_tier: 'tier_a' })).toBe(false);
    expect(canApprove('executive', 'L3', { legal_commitment: true, customer_tier: 'tier_a' })).toBe(true);
    expect(canApprove('compliance_owner', 'L3')).toBe(true);
    expect(canApprove('sales', 'L3')).toBe(false);
  });
});

describe('mock world – 100-question OEM questionnaire', () => {
  it('generates 100 questions with the 01 UI-01 risk mix and the expected batch outcome', async () => {
    const ds = new MockDataSource(generateWorld(NOW), 0);
    const reqs = await ds.listRequests();
    const oem = reqs.find((r) => r.request.title.startsWith('OEM-A Supplier Sustainability'))!;
    expect(oem.progress.questions).toBe(100);
    const inbox = (await ds.listInbox()).filter((i) => i.approval.request_id === oem.request.id);
    const by = (l: RiskLevel) => inbox.filter((i) => i.approval.risk_level === l);
    expect(by('L0')).toHaveLength(61);
    expect(by('L1')).toHaveLength(26);
    expect(by('L2')).toHaveLength(8);
    expect(by('L3')).toHaveLength(2);
    const c = ctx({ now: NOW });
    const l0 = evaluateBatch('L0', by('L0'), c);
    expect(l0.eligible).toHaveLength(60); // 1 条 stale
    const l1 = evaluateBatch('L1', by('L1'), c);
    expect(l1.eligible).toHaveLength(24); // 1 条证书将到期 + 1 条 CR-002 warning
  });

  it('stages a batch on the (mock) server, commits atomically after the window and rejects L3 (R-APR)', async () => {
    const ds = new MockDataSource(generateWorld(NOW), 0);
    let t = Date.parse('2026-11-03T09:00:00Z');
    ds.clock = () => t;
    const inbox = await ds.listInbox();
    const l3 = inbox.find((i) => i.approval.risk_level === 'L3')!;
    await expect(ds.stageBatch({ approval_ids: [l3.approval.id], viewed_approval_ids: [] }, 'user:petra_huber')).rejects.toMatchObject({
      error: { code: 'GRAPH_APPROVAL_REQUIRED' },
    });
    const l0 = inbox.filter((i) => i.approval.risk_level === 'L0').slice(0, 3).map((i) => i.approval.id);
    const b = await ds.stageBatch({ approval_ids: l0, viewed_approval_ids: [] }, 'user:petra_huber');
    expect(b.status).toBe('staged');
    expect(Date.parse(b.undo_until) - Date.parse(b.staged_at)).toBe(10 * 60_000);
    // 窗口内：仍 pending（服务端暂存，不落图）
    expect((await ds.listInbox()).filter((i) => l0.includes(i.approval.id))).toHaveLength(3);
    t += 10 * 60_000;
    const after = await ds.getBatch(b.batch_id, 'user:petra_huber');
    expect(after.status).toBe('committed');
    expect((await ds.listInbox()).some((i) => l0.includes(i.approval.id))).toBe(false);
  });

  it('undo within the window discards the batch; after the window it is refused', async () => {
    const ds = new MockDataSource(generateWorld(NOW), 0);
    let t = Date.parse('2026-11-03T09:00:00Z');
    ds.clock = () => t;
    const inbox = await ds.listInbox();
    const l1 = inbox.filter((i) => i.approval.risk_level === 'L1' && !i.conflicts.length).slice(0, 2).map((i) => i.approval.id);
    const b = await ds.stageBatch({ approval_ids: l1, viewed_approval_ids: [] }, 'user:petra_huber');
    const part = await ds.undoBatch(b.batch_id, [l1[0]], 'user:petra_huber');
    expect(part.approval_ids).toEqual([l1[1]]);
    const undone = await ds.undoBatch(b.batch_id, undefined, 'user:petra_huber');
    expect(undone.status).toBe('undone');
    t += 11 * 60_000;
    expect((await ds.listInbox()).filter((i) => l1.includes(i.approval.id))).toHaveLength(2);
    const b2 = await ds.stageBatch({ approval_ids: [l1[1]], viewed_approval_ids: [] }, 'user:petra_huber');
    t += 10 * 60_000;
    await expect(ds.undoBatch(b2.batch_id, undefined, 'user:petra_huber')).rejects.toMatchObject({ error: { code: 'GRAPH_ILLEGAL_TRANSITION' } });
  });

  it('L2 batch requires viewed_approval_ids (06 §5.1)', async () => {
    const ds = new MockDataSource(generateWorld(NOW), 0);
    const inbox = await ds.listInbox();
    const l2 = inbox.filter((i) => i.approval.risk_level === 'L2' && !i.answer.is_estimate && !i.conflicts.length).slice(0, 1);
    if (!l2.length) return;
    const ids = l2.map((i) => i.approval.id);
    await expect(ds.stageBatch({ approval_ids: ids, viewed_approval_ids: [] }, 'user:petra_huber')).rejects.toMatchObject({
      error: { code: 'GRAPH_APPROVAL_REQUIRED' },
    });
    const b = await ds.stageBatch({ approval_ids: ids, viewed_approval_ids: ids }, 'user:petra_huber');
    expect(b.status).toBe('staged');
  });
});

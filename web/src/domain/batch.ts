// Spec: 06 §5.1（批量一键批准安全条件）、§4.3（各级审批要求）、01 §3.1（UI-01 批量规则）、R-APR / CR-042
import type { InboxItem, RiskLevel, Role } from '../types';
import { APPROVAL_POLICY_V1, canApprove, type ApprovalPolicy } from './policy';
import { daysBetween, toIsoDate } from './dates';

/** 单条被排除出批次的原因（i18n 键 `batch.reason.<key>`）。 */
export type ExclusionReason =
  | 'l3_single' // 条件 1：L3 永不入批（R-APR）
  | 'legal_commitment' // 条件 1：legal_commitment=true 永不入批（CR-042）
  | 'level_mismatch'
  | 'not_pending'
  | 'not_reused' // L0：reused_from 为空
  | 'open_blocking_conflict' // 条件 2
  | 'open_conflict' // 条件 2：L0/L1 连 warn 也不得有
  | 'open_warning_needs_note' // L2 有 warn 须单条填写处置说明（06 §4.3）
  | 'evidence_invalid_at_due' // 条件 3：CR-004 / CR-013
  | 'evidence_not_verified' // L1：全部 Evidence.status=verified
  | 'evidence_expiring_soon' // L1：有效期距今 > 30 天（01 §3.1）
  | 'stale_version' // 条件 4：head 版本变化
  | 'detail_view_required' // L2：require_detail_view
  | 'estimate_ack_required'; // L2：估算答案须勾选"知悉为估算"

/** 整组不显示一键批准的原因（i18n 键 `batch.blocker.<key>`）。 */
export type GroupBlocker =
  | 'batch_not_allowed' // L3
  | 'role' // 条件 5：批准人角色
  | 'mixed_requests' // L2：same_request
  | 'group_not_expanded' // L1：需逐页展开证据缩略
  | 'empty';

export interface BatchContext {
  role: Role;
  now: Date;
  /** 已打开过详情侧栏的 Approval.id（L2 require_detail_view） */
  viewed: ReadonlySet<string>;
  /** 已勾选"知悉为估算"的 Approval.id */
  estimateAcked: ReadonlySet<string>;
  /** 已展开（看过证据缩略）的风险组 */
  expandedLevels: ReadonlySet<RiskLevel>;
  policy?: ApprovalPolicy;
}

export interface BatchSummary {
  count: number;
  by_level: Partial<Record<RiskLevel, number>>;
  datapoints: string[];
  customers: string[];
  estimates: number;
  numbers_changed: number;
}

export interface BatchEvaluation {
  level: RiskLevel;
  eligible: InboxItem[];
  excluded: { item: InboxItem; reasons: ExclusionReason[] }[];
  /** 超过 max_items 而未纳入本批的合格条目数（下批处理） */
  overflow: number;
  max_items: number;
  blockers: GroupBlocker[];
  /** 06 §5.1：全部条件满足才显示"一键批准" */
  showButton: boolean;
  summary: BatchSummary;
}

export function isStale(item: InboxItem): boolean {
  const pinned = `${item.answer.id}#v${item.answer.version}`;
  return item.head_version !== item.answer.version || item.approval.subject_ref !== pinned;
}

/** 条件 3：所有引用 Evidence 在 Request due_date（无则今天）时仍有效。 */
export function evidenceValidAtDue(item: InboxItem, now: Date): boolean {
  const gate = item.request_due_date ?? toIsoDate(now);
  return item.evidence.every((ev) => {
    if (ev.status === 'expired' || ev.status === 'rejected' || ev.status === 'superseded') return false;
    if (!ev.valid_until) return true;
    return ev.valid_until >= gate;
  });
}

/** 单条在指定级别批次中的排除原因；空数组 = 可入批。 */
export function exclusionReasons(item: InboxItem, level: RiskLevel, ctx: BatchContext): ExclusionReason[] {
  const policy = ctx.policy ?? APPROVAL_POLICY_V1;
  const r: ExclusionReason[] = [];
  const a = item.answer;
  if (item.approval.risk_level === 'L3' || a.risk_level === 'L3') r.push('l3_single');
  if (a.legal_commitment) r.push('legal_commitment');
  if (r.length) return r; // L3 / 法律承诺：无须再看其它条件
  if (item.approval.risk_level !== level) r.push('level_mismatch');
  if (item.approval.status !== 'pending') r.push('not_pending');

  const open = item.conflicts.filter((c) => c.status === 'open');
  if (open.some((c) => c.severity === 'blocking')) r.push('open_blocking_conflict');
  else if (open.some((c) => c.severity === 'warning')) r.push(level === 'L2' ? 'open_warning_needs_note' : 'open_conflict');

  if (!evidenceValidAtDue(item, ctx.now)) r.push('evidence_invalid_at_due');
  if (isStale(item)) r.push('stale_version');

  if (level === 'L0' && !a.reused_from) r.push('not_reused');
  if (level === 'L1') {
    if (item.evidence.length === 0 || item.evidence.some((ev) => ev.status !== 'verified')) r.push('evidence_not_verified');
    const today = toIsoDate(ctx.now);
    if (item.evidence.some((ev) => ev.valid_until && daysBetween(today, ev.valid_until) <= policy.l1_min_evidence_days)) {
      r.push('evidence_expiring_soon');
    }
  }
  if (level === 'L2') {
    const lp = policy.levels.L2.batch;
    if (lp.require_detail_view && !ctx.viewed.has(item.approval.id)) r.push('detail_view_required');
    if (a.is_estimate && !ctx.estimateAcked.has(item.approval.id)) r.push('estimate_ack_required');
  }
  return r;
}

export function summarize(items: InboxItem[]): BatchSummary {
  const by_level: Partial<Record<RiskLevel, number>> = {};
  const dps = new Set<string>();
  const cust = new Set<string>();
  let estimates = 0;
  for (const it of items) {
    by_level[it.approval.risk_level] = (by_level[it.approval.risk_level] ?? 0) + 1;
    dps.add(it.answer.datapoint_id.split('@')[0]);
    const cs = it.answer.customer_scope;
    if (cs === 'all') cust.add('all');
    else cs.forEach((c) => cust.add(c));
    if (it.answer.is_estimate) estimates++;
  }
  return { count: items.length, by_level, datapoints: [...dps].sort(), customers: [...cust].sort(), estimates, numbers_changed: 0 };
}

/**
 * 评估某风险组能否一键批准（06 §5.1 条件 1–6）。
 * 不合格条目被剔除并给出原因（AC-403-2："L3 被排除并提示，其余正常"）；
 * 组级阻断（角色、L2 跨问卷、L1 未展开）出现时不显示按钮。
 */
export function evaluateBatch(level: RiskLevel, items: InboxItem[], ctx: BatchContext): BatchEvaluation {
  const policy = ctx.policy ?? APPROVAL_POLICY_V1;
  const lp = policy.levels[level];
  const blockers: GroupBlocker[] = [];
  const eligibleAll: InboxItem[] = [];
  const excluded: { item: InboxItem; reasons: ExclusionReason[] }[] = [];

  for (const it of items) {
    const reasons = exclusionReasons(it, level, ctx);
    if (reasons.length) excluded.push({ item: it, reasons });
    else eligibleAll.push(it);
  }

  if (!lp.batch.allowed) blockers.push('batch_not_allowed');
  const max = lp.batch.allowed ? lp.batch.max_items ?? Infinity : 1;
  const eligible = eligibleAll.slice(0, Number.isFinite(max) ? max : undefined);
  const overflow = eligibleAll.length - eligible.length;

  if (lp.batch.allowed) {
    if (eligible.some((it) => !canApprove(ctx.role, level, { legal_commitment: it.answer.legal_commitment, customer_tier: it.customer_tier, required_role: it.approval.required_role }, policy))) {
      blockers.push('role');
    }
    if (lp.batch.same_request && new Set(eligible.map((it) => it.approval.request_id)).size > 1) blockers.push('mixed_requests');
    if (level === 'L1' && !ctx.expandedLevels.has('L1')) blockers.push('group_not_expanded');
    if (eligible.length === 0) blockers.push('empty');
  }

  return {
    level,
    eligible,
    excluded,
    overflow,
    max_items: Number.isFinite(max) ? (max as number) : 0,
    blockers,
    showButton: blockers.length === 0 && eligible.length > 0,
    summary: summarize(eligible),
  };
}

/**
 * 06 §5.2：窗口结束提交前"再跑一次门禁子集（条件 2–4）"。任一条不过 ⇒ 整批作废重建。
 * `current` 为提交时刻重新读取的条目。
 */
export function recheckBeforeCommit(current: InboxItem[], now: Date): { ok: boolean; failing: { approval_id: string; reasons: ExclusionReason[] }[] } {
  const failing: { approval_id: string; reasons: ExclusionReason[] }[] = [];
  for (const it of current) {
    const reasons: ExclusionReason[] = [];
    const open = it.conflicts.filter((c) => c.status === 'open');
    if (open.some((c) => c.severity === 'blocking')) reasons.push('open_blocking_conflict');
    else if (open.length && (it.approval.risk_level === 'L0' || it.approval.risk_level === 'L1')) reasons.push('open_conflict');
    if (!evidenceValidAtDue(it, now)) reasons.push('evidence_invalid_at_due');
    if (isStale(it)) reasons.push('stale_version');
    if (reasons.length) failing.push({ approval_id: it.approval.id, reasons });
  }
  return { ok: failing.length === 0, failing };
}

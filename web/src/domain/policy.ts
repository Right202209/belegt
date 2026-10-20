// Spec: 06 §4.3（各级审批要求）、§4.4（approval-policy@1.0.0）
import type { RiskLevel, Role } from '../types';

export interface LevelPolicy {
  roles: Role[];
  batch: { allowed: boolean; max_items?: number; same_request?: boolean; require_detail_view?: boolean };
  sla_workdays: number;
  confirm?: ('estimate_ack' | 'warn_note' | 'step_up_auth' | 'clause_read_ack')[];
}

export interface ApprovalPolicy {
  policy_id: 'approval-policy';
  version: string;
  levels: Record<RiskLevel, LevelPolicy>;
  batch_undo_window_minutes: number; // 租户可调 5–60
  /** 01 §3.1 L1：证据有效期距今须 > 30 天 */
  l1_min_evidence_days: number;
}

export const APPROVAL_POLICY_V1: ApprovalPolicy = {
  policy_id: 'approval-policy',
  version: '1.0.0',
  levels: {
    L0: { roles: ['compliance_owner'], batch: { allowed: true, max_items: 200 }, sla_workdays: 1 },
    L1: { roles: ['compliance_owner'], batch: { allowed: true, max_items: 100 }, sla_workdays: 2 },
    L2: {
      roles: ['compliance_owner'],
      batch: { allowed: true, max_items: 20, same_request: true, require_detail_view: true },
      sla_workdays: 3,
      confirm: ['estimate_ack', 'warn_note'],
    },
    L3: { roles: ['compliance_owner', 'executive'], batch: { allowed: false }, sla_workdays: 3, confirm: ['step_up_auth', 'clause_read_ack'] },
  },
  batch_undo_window_minutes: 10,
  l1_min_evidence_days: 30,
};

/** 06 §4.4：租户覆盖 batch_undo_window_minutes 只能取 5–60。 */
export function clampUndoWindowMinutes(minutes: number): number {
  if (!Number.isFinite(minutes)) return 10;
  return Math.min(60, Math.max(5, Math.round(minutes)));
}

/**
 * 单条审批所需角色（06 §4.3）：L3 + legal_commitment + tier_a ⇒ 仅 executive（policy.special）。
 */
export function requiredRolesFor(
  level: RiskLevel,
  opts: { legal_commitment?: boolean; customer_tier?: string } = {},
  policy: ApprovalPolicy = APPROVAL_POLICY_V1,
): Role[] {
  if (level === 'L3' && opts.legal_commitment && opts.customer_tier === 'tier_a') return ['executive'];
  return policy.levels[level].roles;
}

export function canApprove(
  role: Role,
  level: RiskLevel,
  opts: { legal_commitment?: boolean; customer_tier?: string; required_role?: Role[] } = {},
  policy: ApprovalPolicy = APPROVAL_POLICY_V1,
): boolean {
  const byPolicy = requiredRolesFor(level, opts, policy);
  // Approval.required_role 由 approval-router 计算；两者取交集（更严）
  const allowed = opts.required_role && opts.required_role.length ? byPolicy.filter((r) => opts.required_role!.includes(r)) : byPolicy;
  return allowed.includes(role);
}

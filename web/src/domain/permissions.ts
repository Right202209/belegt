// Spec: 01 §5.2（权限与可见性在界面上的表现）、06 §3.2（冲突处理角色）、03 §7（审计包触发角色）
import type { Role } from '../types';

export type Page = 'requests' | 'inbox' | 'cards' | 'conflicts' | 'suppliers' | 'expiry';

/** 界面演示用的四个角色（任务要求） */
export const DEMO_ROLES: Role[] = ['compliance_owner', 'executive', 'sales', 'contributor'];

const NAV: Record<string, Page[]> = {
  compliance_owner: ['inbox', 'cards', 'requests', 'conflicts', 'suppliers', 'expiry'],
  executive: ['inbox', 'cards', 'requests', 'conflicts', 'suppliers', 'expiry'],
  sales: ['requests', 'cards', 'expiry'],
  contributor: ['cards', 'requests'],
};

/** "不可见优于禁用"：无权访问的导航项不出现。 */
export function visiblePages(role: Role): Page[] {
  return NAV[role] ?? [];
}

export function defaultPage(role: Role): Page {
  return visiblePages(role)[0] ?? 'requests';
}

/** 草稿值（draft/in_review）可见性：sales ✗（显示"in Vorbereitung"），contributor 仅相关题。 */
export function canSeeDraftValues(role: Role): boolean {
  return role === 'compliance_owner' || role === 'executive' || role === 'auditor_readonly';
}

/** 审批收件箱：executive 只看 L3 与指派给我的。 */
export function inboxScope(role: Role): 'all' | 'l3_and_assigned' | 'none' {
  if (role === 'compliance_owner') return 'all';
  if (role === 'executive') return 'l3_and_assigned';
  return 'none';
}

/** 卡片作答：sales/contributor 仅指派给本角色的卡片。 */
export function canAnswerCard(role: Role, assigneeRole: Role): boolean {
  if (role === 'compliance_owner' || role === 'executive') return true;
  return role === assigneeRole;
}

/** 06 §3.2：block 冲突只有 compliance_owner 可选 B（接受差异）或 C（驳回）；A 可由有审批权限的角色发起。 */
export function conflictActions(role: Role): { unify: boolean; accept: boolean; dismiss: boolean } {
  return {
    unify: role === 'compliance_owner' || role === 'executive',
    accept: role === 'compliance_owner',
    dismiss: role === 'compliance_owner',
  };
}

/** 03 §7：审计包由 compliance_owner、auditor_readonly、org_admin 触发。 */
export function canExportAuditPack(role: Role): boolean {
  return role === 'compliance_owner' || role === 'auditor_readonly' || role === 'org_admin';
}

/** UI-05/上游：executive 只能"提醒"，不能改数字；首次发送确认归 compliance_owner（06 §4.4 non_answer_subjects）。 */
export function supplierActions(role: Role): { remind: boolean; approveFirstSend: boolean } {
  return { remind: role === 'compliance_owner' || role === 'executive', approveFirstSend: role === 'compliance_owner' };
}

// Spec: 06 §5.2（撤销窗口：延迟提交）、01 §3.1（批量批准后 10 分钟内可撤销）
// 窗口内批次决定只暂存（Approval 仍 pending、Answer 仍 in_review），撤销即丢弃；
// 窗口结束后再跑门禁子集（条件 2–4），通过才落图（graph.commit_batch，同一 batch_id=apb_…）。
// 暂存与结算在服务端（belegt-core BatchStager）；前端只用这里的函数显示倒计时（fromServerBatch），
// MockDataSource 用同一组函数模拟服务端。
import type { ServerBatch } from '../api/types';
import { clampUndoWindowMinutes } from './policy';

export type StagedStatus = 'staged' | 'committed' | 'undone' | 'voided';

export interface StagedBatch {
  batch_id: string; // apb_…
  approval_ids: string[];
  staged_at: number; // epoch ms
  window_ms: number;
  status: StagedStatus;
  /** 06 §5.2：due_date 剩余时间小于窗口时允许"立即提交"，放弃撤销能力 */
  immediate: boolean;
  removed: string[]; // 窗口内被剔除的条目
}

export function stageBatch(batch_id: string, approval_ids: string[], now: number, windowMinutes = 10, immediate = false): StagedBatch {
  if (approval_ids.length === 0) throw new Error('empty batch');
  return {
    batch_id,
    approval_ids: [...approval_ids],
    staged_at: now,
    window_ms: immediate ? 0 : clampUndoWindowMinutes(windowMinutes) * 60_000,
    status: 'staged',
    immediate,
    removed: [],
  };
}

export function remainingMs(b: StagedBatch, now: number): number {
  if (b.status !== 'staged') return 0;
  return Math.max(0, b.staged_at + b.window_ms - now);
}

export function canUndo(b: StagedBatch, now: number): boolean {
  return b.status === 'staged' && !b.immediate && remainingMs(b, now) > 0;
}

/** 整批撤销。窗口外或已提交 ⇒ 抛错（只能走新 Answer 版本 + 重新审批）。 */
export function undoBatch(b: StagedBatch, now: number): StagedBatch {
  if (!canUndo(b, now)) throw new Error('undo window closed');
  return { ...b, status: 'undone' };
}

/** 窗口内剔除部分条目；剔光则等同撤销。 */
export function removeFromBatch(b: StagedBatch, ids: string[], now: number): StagedBatch {
  if (!canUndo(b, now)) throw new Error('undo window closed');
  const drop = new Set(ids);
  const keep = b.approval_ids.filter((id) => !drop.has(id));
  return { ...b, approval_ids: keep, removed: [...b.removed, ...b.approval_ids.filter((id) => drop.has(id))], status: keep.length ? 'staged' : 'undone' };
}

export function isDue(b: StagedBatch, now: number): boolean {
  return b.status === 'staged' && remainingMs(b, now) === 0;
}

/** 窗口到期结算：recheckOk=false ⇒ 整批作废（voided），需重建。 */
export function settle(b: StagedBatch, now: number, recheckOk: boolean): StagedBatch {
  if (!isDue(b, now)) return b;
  return { ...b, status: recheckOk ? 'committed' : 'voided' };
}

/** 06 §5.2：Request 剩余时间 < 窗口 ⇒ 界面提示并允许"立即提交"。 */
export function dueSoonerThanWindow(dueDateIso: string | undefined, now: number, windowMinutes = 10): boolean {
  if (!dueDateIso) return false;
  // 截止日按当天 23:59:59 UTC 计（租户时区换算由后端负责）
  const dueEnd = Date.parse(`${dueDateIso}T23:59:59Z`);
  return dueEnd - now < clampUndoWindowMinutes(windowMinutes) * 60_000;
}

export function formatCountdown(ms: number): string {
  const s = Math.ceil(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, '0')}`;
}

/**
 * 服务端批次 → 本地倒计时视图。窗口长度取服务端 ``undo_until − staged_at``，起点用本地接收时刻
 * （避免浏览器与服务器时钟偏差让倒计时提前/滞后）；状态以服务端为准。
 */
export function fromServerBatch(sb: ServerBatch, receivedAt: number, prev?: StagedBatch): StagedBatch {
  const window = Math.max(0, Date.parse(sb.undo_until) - Date.parse(sb.staged_at));
  const removed = prev ? prev.approval_ids.filter((id) => !sb.approval_ids.includes(id)) : [];
  return {
    batch_id: sb.batch_id,
    approval_ids: [...sb.approval_ids],
    staged_at: prev?.staged_at ?? receivedAt,
    window_ms: Number.isFinite(window) ? window : 0,
    status: sb.status,
    immediate: Number.isFinite(window) ? window === 0 : false,
    removed: [...(prev?.removed ?? []), ...removed.filter((id) => !(prev?.removed ?? []).includes(id))],
  };
}

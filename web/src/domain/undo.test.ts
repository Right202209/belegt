// 覆盖 06 §5.2 撤销窗口（10 分钟延迟提交、租户 5–60 分钟、立即提交放弃撤销、到期复检）
import { describe, expect, it } from 'vitest';
import { canUndo, dueSoonerThanWindow, formatCountdown, isDue, remainingMs, removeFromBatch, settle, stageBatch, undoBatch } from './undo';
import { clampUndoWindowMinutes } from './policy';

const T0 = Date.parse('2026-10-19T08:00:00Z');
const MIN = 60_000;

describe('undo window – 06 §5.2', () => {
  it('defaults to a 10-minute window and counts down', () => {
    const b = stageBatch('apb_1', ['a', 'b'], T0);
    expect(b.window_ms).toBe(10 * MIN);
    expect(remainingMs(b, T0)).toBe(10 * MIN);
    expect(remainingMs(b, T0 + 3 * MIN)).toBe(7 * MIN);
    expect(formatCountdown(remainingMs(b, T0 + 3 * MIN + 15_000))).toBe('6:45');
    expect(remainingMs(b, T0 + 11 * MIN)).toBe(0);
  });

  it('allows undo inside the window and discards the staged decision', () => {
    const b = stageBatch('apb_1', ['a', 'b'], T0);
    expect(canUndo(b, T0 + 9 * MIN + 59_000)).toBe(true);
    const u = undoBatch(b, T0 + 5 * MIN);
    expect(u.status).toBe('undone');
    expect(remainingMs(u, T0 + 5 * MIN)).toBe(0);
    expect(isDue(u, T0 + 20 * MIN)).toBe(false); // 撤销后不会再落图
  });

  it('refuses undo after the window closed', () => {
    const b = stageBatch('apb_1', ['a'], T0);
    expect(canUndo(b, T0 + 10 * MIN)).toBe(false);
    expect(() => undoBatch(b, T0 + 10 * MIN)).toThrow();
  });

  it('settles at window end: commit when recheck passes, void otherwise', () => {
    const b = stageBatch('apb_1', ['a'], T0);
    expect(settle(b, T0 + 5 * MIN, true).status).toBe('staged'); // 未到期不结算
    expect(isDue(b, T0 + 10 * MIN)).toBe(true);
    expect(settle(b, T0 + 10 * MIN, true).status).toBe('committed');
    expect(settle(b, T0 + 10 * MIN, false).status).toBe('voided');
  });

  it('supports removing single items; removing all equals undo', () => {
    const b = stageBatch('apb_1', ['a', 'b', 'c'], T0);
    const r = removeFromBatch(b, ['b'], T0 + MIN);
    expect(r.approval_ids).toEqual(['a', 'c']);
    expect(r.removed).toEqual(['b']);
    expect(removeFromBatch(r, ['a', 'c'], T0 + 2 * MIN).status).toBe('undone');
  });

  it('tenant override is clamped to 5–60 minutes', () => {
    expect(clampUndoWindowMinutes(1)).toBe(5);
    expect(clampUndoWindowMinutes(90)).toBe(60);
    expect(clampUndoWindowMinutes(15)).toBe(15);
    expect(clampUndoWindowMinutes(Number.NaN)).toBe(10);
    expect(stageBatch('apb_2', ['a'], T0, 120).window_ms).toBe(60 * MIN);
  });

  it('immediate commit gives up undo (due date sooner than window)', () => {
    const b = stageBatch('apb_3', ['a'], T0, 10, true);
    expect(canUndo(b, T0)).toBe(false);
    expect(isDue(b, T0)).toBe(true);
    expect(dueSoonerThanWindow('2026-10-19', Date.parse('2026-10-19T23:55:00Z'))).toBe(true);
    expect(dueSoonerThanWindow('2026-10-20', Date.parse('2026-10-19T23:55:00Z'))).toBe(false);
    expect(dueSoonerThanWindow(undefined, T0)).toBe(false);
  });

  it('rejects empty batches', () => {
    expect(() => stageBatch('apb_4', [], T0)).toThrow();
  });
});

describe('fromServerBatch (server-side staging, countdown only)', () => {
  it('derives the window from undo_until − staged_at and counts down from local receipt time', async () => {
    const { fromServerBatch, remainingMs, canUndo } = await import('./undo');
    const sb = {
      batch_id: 'apb_01JD0000000000000000000001',
      status: 'staged' as const,
      approval_ids: ['apr_A', 'apr_B'],
      staged_at: '2026-11-03T09:00:00Z',
      commit_at: '2026-11-03T09:10:00Z',
      undo_until: '2026-11-03T09:10:00Z',
    };
    const local = 1_000_000;
    const b = fromServerBatch(sb, local);
    expect(b.window_ms).toBe(600_000);
    expect(remainingMs(b, local + 60_000)).toBe(540_000);
    expect(canUndo(b, local + 599_000)).toBe(true);
    expect(canUndo(b, local + 600_000)).toBe(false);
    const partial = fromServerBatch({ ...sb, approval_ids: ['apr_B'] }, local + 5_000, b);
    expect(partial.staged_at).toBe(local);
    expect(partial.removed).toEqual(['apr_A']);
    const done = fromServerBatch({ ...sb, status: 'committed' }, local + 700_000, partial);
    expect(done.status).toBe('committed');
    expect(remainingMs(done, local)).toBe(0);
    expect(fromServerBatch({ ...sb, undo_until: sb.staged_at }, local).immediate).toBe(true);
  });
});

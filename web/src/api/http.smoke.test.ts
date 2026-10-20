// 前后端对接冒烟（需要运行中的 `belegt serve --dev-auth`，由 scripts/smoke.sh 启动并准备数据；否则整组跳过）：
// 列表、收件箱、单条批准、批量暂存 + 部分/整批撤销（服务端暂存，06 §5.2）、/blobs、供应商视图、开发头。
import { writeFileSync } from 'node:fs';
import { afterAll, describe, expect, it } from 'vitest';
import { HttpDataSource } from './HttpDataSource';
import { authConfigFromEnv, authHeaders } from './auth';
import { ApiException } from './types';
import type { InboxItem } from '../types';

const BASE = process.env.BELEGT_SMOKE_API ?? '';
const REQ = process.env.BELEGT_SMOKE_REQUEST ?? '';
const results: { step: string; ok: boolean; detail: string }[] = [];
const record = (step: string, ok: boolean, detail = '') => results.push({ step, ok, detail });

describe.skipIf(!BASE)('HttpDataSource ↔ belegt serve --dev-auth (smoke)', () => {
  const cfg = authConfigFromEnv({ VITE_API_TOKENS: 'compliance_owner=demo-owner,executive=demo-exec,sales=demo-sales' });
  const owner = new HttpDataSource(BASE, () => authHeaders(cfg, 'compliance_owner'));
  const dev = new HttpDataSource(BASE, () => authHeaders(authConfigFromEnv({ VITE_DEV_ACTORS: 'compliance_owner=user:u_owner' }), 'compliance_owner'));
  let inbox: InboxItem[] = [];
  const clean = (it: InboxItem) => !it.conflicts.length && !it.answer.legal_commitment;

  afterAll(() => {
    if (process.env.BELEGT_SMOKE_OUT) writeFileSync(process.env.BELEGT_SMOKE_OUT, JSON.stringify({ base: BASE, request: REQ, results }, null, 2));
  });

  it('lists requests (Bearer token)', async () => {
    const reqs = await owner.listRequests();
    const r = reqs.find((x) => x.request.id === REQ);
    expect(r).toBeTruthy();
    expect(r!.request.status).toBe('in_review');
    expect(r!.progress.questions).toBe(15);
    record('列表 GET /requests', true, `${reqs.length} requests, ${REQ} in_review, ${r!.progress.questions} questions`);
  });

  it('dev mode still works with X-Belegt-Actor', async () => {
    const reqs = await dev.listRequests();
    expect(reqs.some((x) => x.request.id === REQ)).toBe(true);
    const anon = new HttpDataSource(BASE, () => ({}));
    const err = await anon.listInbox().catch((e) => e);
    expect(err).toBeInstanceOf(ApiException);
    expect(err.status).toBe(401);
    record('开发头 X-Belegt-Actor / 无身份 401', true, 'X-Belegt-Actor: user:u_owner → 200；无头 → 401 GRAPH_UNAUTHENTICATED');
  });

  it('loads the inbox', async () => {
    inbox = (await owner.listInbox()).filter((i) => i.approval.request_id === REQ);
    expect(inbox.length).toBeGreaterThan(3);
    const by: Record<string, number> = {};
    for (const i of inbox) by[i.approval.risk_level] = (by[i.approval.risk_level] ?? 0) + 1;
    record('收件箱 GET /inbox', true, `${inbox.length} pending approvals ${JSON.stringify(by)}`);
  });

  it('approves a single item', async () => {
    const it1 = inbox.find((i) => i.approval.risk_level === 'L1' && clean(i)) ?? inbox.find((i) => i.approval.risk_level === 'L0' && clean(i))!;
    await owner.decide({ approval_id: it1.approval.id, decision: 'approved' }, 'user:u_owner');
    const after = (await owner.listInbox()).map((i) => i.approval.id);
    expect(after).not.toContain(it1.approval.id);
    record('单条批准 POST /approvals {approval_id}', true, `${it1.approval.id} (${it1.approval.risk_level}) → approved`);
    inbox = inbox.filter((i) => i.approval.id !== it1.approval.id);
  });

  it('stages a batch on the server, partially and fully undoes it', async () => {
    const items = inbox.filter((i) => (i.approval.risk_level === 'L0' || i.approval.risk_level === 'L1') && clean(i)).slice(0, 3);
    expect(items.length).toBeGreaterThanOrEqual(2);
    const ids = items.map((i) => i.approval.id);
    const b = await owner.stageBatch({ approval_ids: ids, viewed_approval_ids: ids }, 'user:u_owner');
    expect(b.status).toBe('staged');
    expect(b.batch_id).toMatch(/^apb_/);
    const windowMin = (Date.parse(b.undo_until) - Date.parse(b.staged_at)) / 60_000;
    expect(windowMin).toBe(10);
    const staged = (await owner.listInbox()).filter((i) => ids.includes(i.approval.id));
    expect(staged).toHaveLength(ids.length); // 窗口内仍 pending（只暂存）
    expect(staged.every((i) => (i as InboxItem & { ui_staged_batch?: string }).ui_staged_batch === b.batch_id)).toBe(true);
    const part = await owner.undoBatch(b.batch_id, [ids[0]], 'user:u_owner');
    expect(part.status).toBe('staged');
    expect(part.approval_ids).toEqual(ids.slice(1));
    const full = await owner.undoBatch(b.batch_id, undefined, 'user:u_owner');
    expect(full.status).toBe('undone');
    expect((await owner.getBatch(b.batch_id, 'user:u_owner')).status).toBe('undone');
    const still = (await owner.listInbox()).filter((i) => ids.includes(i.approval.id));
    expect(still).toHaveLength(ids.length);
    const err = await owner.stageBatch({ approval_ids: ids, viewed_approval_ids: ids, immediate: true }, 'user:u_owner').catch((e) => e);
    expect(err).toBeInstanceOf(ApiException);
    expect(err.error.code).toBe('GRAPH_ILLEGAL_TRANSITION'); // 截止日远于窗口：不允许"立即提交"
    record('批量 + 撤销 POST /approvals(批量) → /approvals/undo', true,
      `${b.batch_id}: staged ${ids.length}（202，undo_until 窗口 ${windowMin} min）→ 部分撤销 1 → 整批 undone；immediate 被拒 GRAPH_ILLEGAL_TRANSITION`);
  });

  it('L2 batch needs viewed_approval_ids', async () => {
    const l2 = inbox.filter((i) => i.approval.risk_level === 'L2' && clean(i) && !i.answer.is_estimate).slice(0, 1);
    if (!l2.length) {
      record('L2 批量 viewed_approval_ids', true, 'skipped: no clean L2 item');
      return;
    }
    const ids = l2.map((i) => i.approval.id);
    const err = await owner.stageBatch({ approval_ids: ids, viewed_approval_ids: [] }, 'user:u_owner').catch((e) => e);
    expect(err).toBeInstanceOf(ApiException);
    expect(err.error.code).toBe('GRAPH_APPROVAL_REQUIRED');
    const b = await owner.stageBatch({ approval_ids: ids, viewed_approval_ids: ids }, 'user:u_owner');
    expect(b.status).toBe('staged');
    await owner.undoBatch(b.batch_id, undefined, 'user:u_owner');
    record('L2 批量 viewed_approval_ids', true, `无 viewed → 422 GRAPH_APPROVAL_REQUIRED；带 viewed → staged（随后撤销）`);
  });

  it('uploads a blob and lists supplier requests', async () => {
    const sha = await owner.uploadBlob(new File(['Nachweis'], 'nachweis.txt', { type: 'text/plain' }));
    expect(sha).toMatch(/^sha256:[0-9a-f]{64}$/);
    const sreqs = await owner.listSupplierRequests();
    expect(sreqs.length).toBeGreaterThanOrEqual(1);
    expect(sreqs[0].history.length).toBeGreaterThanOrEqual(2);
    record('POST /blobs + GET /graph/objects?type=SupplierRequest&view=timeline', true, `${sha.slice(0, 19)}…；${sreqs.length} supplier request(s)`);
  });
});

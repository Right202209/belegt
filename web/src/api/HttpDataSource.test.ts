// 覆盖 HttpDataSource 与 belegt-core 的接口约定：Bearer 令牌 / 开发头、服务端批量暂存与撤销、L2 viewed_approval_ids、
// /blobs 读 blob_sha256、03 §6.8 错误体
import { describe, expect, it } from 'vitest';
import { HttpDataSource } from './HttpDataSource';
import { authConfigFromEnv, authHeaders, parseRoleMap } from './auth';
import { ApiException } from './types';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: unknown;
}

function fakeFetch(routes: Record<string, (c: Call) => { status?: number; json?: unknown }>) {
  const calls: Call[] = [];
  const impl = (async (url: string, init: RequestInit = {}) => {
    const headers = (init.headers ?? {}) as Record<string, string>;
    let body: unknown = init.body;
    if (typeof body === 'string' && headers['Content-Type'] === 'application/json') body = JSON.parse(body);
    const c: Call = { url, method: init.method ?? 'GET', headers, body };
    calls.push(c);
    const key = `${c.method} ${url.replace('http://api.test', '')}`;
    const r = routes[key]?.(c) ?? { status: 404, json: { error: { code: 'GRAPH_NOT_FOUND', message: key } } };
    return new Response(JSON.stringify(r.json ?? {}), { status: r.status ?? 200, headers: { 'Content-Type': 'application/json' } });
  }) as unknown as typeof fetch;
  return { calls, impl };
}

const BATCH = {
  batch_id: 'apb_01JD0000000000000000000001',
  status: 'staged',
  approval_ids: ['apr_1', 'apr_2'],
  staged_at: '2026-11-03T09:00:00Z',
  commit_at: '2026-11-03T09:10:00Z',
  undo_until: '2026-11-03T09:10:00Z',
};

describe('auth headers', () => {
  it('parses role maps and prefers Bearer tokens over the dev actor header', () => {
    expect(parseRoleMap('compliance_owner=demo-owner, executive=demo-exec')).toEqual({ compliance_owner: 'demo-owner', executive: 'demo-exec' });
    expect(parseRoleMap('{"sales":"demo-sales"}')).toEqual({ sales: 'demo-sales' });
    expect(parseRoleMap('garbage')).toEqual({});
    const cfg = authConfigFromEnv({ VITE_API_TOKENS: 'compliance_owner=demo-owner', VITE_DEV_ACTORS: 'executive=user:u_exec' });
    expect(authHeaders(cfg, 'compliance_owner')).toEqual({ Authorization: 'Bearer demo-owner' });
    expect(authHeaders(cfg, 'executive')).toEqual({ 'X-Belegt-Actor': 'user:u_exec' });
    expect(authHeaders(cfg, 'sales', 'user:fallback')).toEqual({ 'X-Belegt-Actor': 'user:fallback' });
    expect(authHeaders(authConfigFromEnv({ VITE_API_TOKEN: 'one' }), 'sales')).toEqual({ Authorization: 'Bearer one' });
  });
});

describe('HttpDataSource ↔ belegt-core', () => {
  it('sends the Bearer token on every request and unwraps {items}', async () => {
    const { calls, impl } = fakeFetch({ 'GET /inbox': () => ({ json: { items: [{ approval: { id: 'apr_1' } }] } }) });
    const ds = new HttpDataSource('http://api.test/', () => ({ Authorization: 'Bearer demo-owner' }), impl);
    const inbox = await ds.listInbox();
    expect(inbox).toHaveLength(1);
    expect(calls[0].headers.Authorization).toBe('Bearer demo-owner');
    expect(calls[0].headers['X-Belegt-Actor']).toBeUndefined();
  });

  it('stages batches on the server with viewed_approval_ids, undoes and polls them', async () => {
    const { calls, impl } = fakeFetch({
      'POST /approvals': () => ({ status: 202, json: { batch: BATCH } }),
      'POST /approvals/undo': (c) => ({ json: { batch: { ...BATCH, status: 'undone', approval_ids: [] }, echo: c.body } }),
      [`GET /approvals/batches/${BATCH.batch_id}`]: () => ({ json: { batch: { ...BATCH, status: 'committed' } } }),
    });
    const ds = new HttpDataSource('http://api.test', () => ({ Authorization: 'Bearer t' }), impl);
    const b = await ds.stageBatch({ approval_ids: ['apr_1', 'apr_2'], viewed_approval_ids: ['apr_2'], confirmations: { estimate_ack: true } }, 'user:x');
    expect(b.status).toBe('staged');
    expect(calls[0].body).toEqual({ approval_ids: ['apr_1', 'apr_2'], viewed_approval_ids: ['apr_2'], confirmations: { estimate_ack: true }, decision: 'approved' });
    expect((calls[0].body as Record<string, unknown>).batch_id).toBeUndefined(); // 批次 ID 由服务端生成
    const u = await ds.undoBatch(BATCH.batch_id, undefined, 'user:x');
    expect(u.status).toBe('undone');
    expect(calls[1].body).toEqual({ batch_id: BATCH.batch_id });
    await ds.undoBatch(BATCH.batch_id, ['apr_1'], 'user:x');
    expect(calls[2].body).toEqual({ batch_id: BATCH.batch_id, approval_ids: ['apr_1'] });
    expect((await ds.getBatch(BATCH.batch_id, 'user:x')).status).toBe('committed');
  });

  it('reads blob_sha256 (falls back to sha256) from /blobs', async () => {
    const { calls, impl } = fakeFetch({ 'POST /blobs': () => ({ status: 201, json: { sha256: 'sha256:abc' } }) });
    const ds = new HttpDataSource('http://api.test', () => ({ Authorization: 'Bearer t' }), impl);
    const file = new File(['a;b'], 'q.csv', { type: 'text/csv' });
    expect(await ds.uploadBlob(file)).toBe('sha256:abc');
    expect(calls[0].headers.Authorization).toBe('Bearer t');
    expect(calls[0].headers['Content-Type']).toBe('text/csv');
    expect(calls[0].headers['X-Filename']).toBe('q.csv');
  });

  it('maps the 03 §6.8 error body to ApiException', async () => {
    const { impl } = fakeFetch({
      'POST /approvals': () => ({ status: 422, json: { error: { code: 'GRAPH_APPROVAL_REQUIRED', message: 'batch approval conditions not met', rule_id: '06-5.1' } } }),
    });
    const ds = new HttpDataSource('http://api.test', () => ({}), impl);
    const err = await ds.stageBatch({ approval_ids: ['apr_9'], viewed_approval_ids: [] }, 'u').catch((e) => e);
    expect(err).toBeInstanceOf(ApiException);
    expect(err.status).toBe(422);
    expect(err.error.code).toBe('GRAPH_APPROVAL_REQUIRED');
  });

  it('uses the dev actor header when no token is configured', async () => {
    const { calls, impl } = fakeFetch({ 'GET /requests': () => ({ json: [] }) });
    const cfg = authConfigFromEnv({ VITE_DEV_ACTORS: 'compliance_owner=user:u_owner' });
    const ds = new HttpDataSource('http://api.test', () => authHeaders(cfg, 'compliance_owner'), impl);
    await ds.listRequests();
    expect(calls[0].headers['X-Belegt-Actor']).toBe('user:u_owner');
    expect(calls[0].headers.Authorization).toBeUndefined();
  });
});

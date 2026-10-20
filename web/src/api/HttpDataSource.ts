// Spec: 03 §6.8（API 与错误体 {"error": {code, message, rule_id, path, details}}）、§7（审计包）、06 §5.2（服务端暂存批量 + 撤销）
// 连接 belegt-core（VITE_API_BASE）。数据形状按 03 字段名；列表端点兼容 `[...]` 与 `{items: [...]}`。
// 认证：每个请求带 getAuth() 给出的头（Bearer 令牌；开发模式可退回 X-Belegt-Actor），见 api/auth.ts。
import type { ClosedQuestion, ConflictView, Evidence, InboxItem, Lineage, RequestView, SupplierRequestView } from '../types';
import {
  ApiException,
  type CardAnswerInput,
  type DataSource,
  type DecisionInput,
  type QuestionRow,
  type ResolveConflictInput,
  type ServerBatch,
  type StageBatchInput,
} from './types';

type Listish<T> = T[] | { items: T[] };
const items = <T>(x: Listish<T>): T[] => (Array.isArray(x) ? x : x.items ?? []);

export type AuthProvider = () => Record<string, string>;
type FetchLike = typeof fetch;

export class HttpDataSource implements DataSource {
  readonly mode = 'http' as const;
  private base: string;
  constructor(
    base: string,
    private getAuth: AuthProvider = () => ({}),
    private fetchImpl: FetchLike = (...a) => fetch(...a),
  ) {
    this.base = base.replace(/\/$/, '');
  }

  today() {
    return new Date().toISOString().slice(0, 10);
  }

  private async raw(method: string, path: string, init: { body?: BodyInit; headers?: Record<string, string> } = {}): Promise<Response> {
    const headers: Record<string, string> = { Accept: 'application/json', ...this.getAuth(), ...(init.headers ?? {}) };
    const res = await this.fetchImpl(`${this.base}${path}`, { method, headers, body: init.body });
    if (!res.ok) {
      let err: ApiException['error'] = { code: `HTTP_${res.status}`, message: res.statusText };
      try {
        const j = await res.json();
        if (j?.error) err = j.error;
      } catch {
        /* ignore */
      }
      throw new ApiException(err, res.status);
    }
    return res;
  }

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await this.raw(method, path, body === undefined ? {} : { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } });
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  async listRequests() {
    return items(await this.req<Listish<RequestView>>('GET', '/requests'));
  }
  async listQuestions(requestId: string) {
    return items(await this.req<Listish<QuestionRow>>('GET', `/requests/${encodeURIComponent(requestId)}/questions`));
  }
  async listInbox() {
    return items(await this.req<Listish<InboxItem>>('GET', '/inbox'));
  }
  async decide(input: DecisionInput, _actor: string) {
    await this.req('POST', '/approvals', input);
  }
  async stageBatch(input: StageBatchInput, _actor: string): Promise<ServerBatch> {
    // 06 §5.2：服务端暂存（202 + undo_until）；窗口结束由服务端复检条件 2–4 后 graph.commit_batch 或整批作废
    const r = await this.req<{ batch: ServerBatch }>('POST', '/approvals', { ...input, decision: 'approved' });
    return r.batch;
  }
  async undoBatch(batchId: string, approvalIds: string[] | undefined, _actor: string): Promise<ServerBatch> {
    const body: Record<string, unknown> = { batch_id: batchId };
    if (approvalIds) body.approval_ids = approvalIds;
    return (await this.req<{ batch: ServerBatch }>('POST', '/approvals/undo', body)).batch;
  }
  async getBatch(batchId: string, _actor: string): Promise<ServerBatch> {
    return (await this.req<{ batch: ServerBatch }>('GET', `/approvals/batches/${encodeURIComponent(batchId)}`)).batch;
  }
  async listClosedQuestions() {
    return items(await this.req<Listish<ClosedQuestion>>('GET', '/closed-questions'));
  }
  async answerClosedQuestion(input: CardAnswerInput, _actor: string) {
    await this.req('POST', `/closed-questions/${encodeURIComponent(input.cq_id)}/answer`, input);
  }
  async uploadBlob(file: File): Promise<string> {
    const res = await this.raw('POST', '/blobs', {
      body: file,
      headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name || 'upload') },
    });
    const j = (await res.json()) as { blob_sha256?: string; sha256?: string };
    const sha = j.blob_sha256 ?? j.sha256;
    if (!sha) throw new ApiException({ code: 'HTTP_BAD_RESPONSE', message: 'blob_sha256 missing' }, 502);
    return sha;
  }
  async listConflicts() {
    return items(await this.req<Listish<ConflictView>>('GET', '/conflicts'));
  }
  async resolveConflict(input: ResolveConflictInput, _actor: string) {
    await this.req('POST', `/conflicts/${encodeURIComponent(input.conflict_id)}/resolve`, input);
  }
  async listSupplierRequests() {
    return items(await this.req<Listish<SupplierRequestView>>('GET', '/graph/objects?type=SupplierRequest&view=timeline'));
  }
  async approveSupplierFirstSend(id: string, _actor: string) {
    // 06 §4.4 non_answer_subjects.supplier_request_first_send：Approval(subject_ref=sreq_…#vN)
    await this.req('POST', '/approvals', { subject_id: id, decision: 'approved', kind: 'supplier_request_first_send' });
  }
  async listEvidence() {
    return items(await this.req<Listish<Evidence>>('GET', '/graph/objects?type=Evidence&status=verified,expired'));
  }
  async lineage(ref: string) {
    return this.req<Lineage>('GET', `/graph/lineage/${encodeURIComponent(ref)}`);
  }
  async exportAuditPack(requestId: string) {
    const res = await this.raw('POST', `/requests/${encodeURIComponent(requestId)}/audit-pack`);
    const cd = res.headers.get('Content-Disposition') ?? '';
    const m = /filename="?([^";]+)"?/.exec(cd);
    return { filename: m?.[1] ?? `belegt-auditpack_${requestId}.zip`, blob: await res.blob() };
  }
}

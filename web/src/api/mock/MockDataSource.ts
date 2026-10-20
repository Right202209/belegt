// Spec: 03 §3（状态转移）、§6.8（错误码）、§7（审计包 manifest）、06 §3.2/§5.2
// 内存 mock：模拟 belegt-core 的行为，含 R-APR（角色/L3 批量）、R-NUM（修改须有出处）等关键写入校验。
import type { Answer, ClosedQuestion, ConflictView, Evidence, InboxItem, Lineage, LineageEdge, LineageNode, RequestView, SupplierRequestView } from '../../types';
import { APPROVAL_POLICY_V1, canApprove, clampUndoWindowMinutes } from '../../domain/policy';
import { scopesOverlap } from '../../domain/conflict';
import { recheckBeforeCommit } from '../../domain/batch';
import { canUndo, dueSoonerThanWindow, isDue, removeFromBatch, settle, stageBatch as stageLocal, undoBatch as undoLocal, type StagedBatch } from '../../domain/undo';
import {
  ApiException,
  type CardAnswerInput,
  type DataSource,
  type DecisionInput,
  type QuestionRow,
  type ResolveConflictInput,
  type ServerBatch,
  type StageBatchInput,
} from '../types';
import { generateWorld, type MockWorld, USERS } from './generate';
import { fakeSha, mkId, ref } from './ids';

const stripVersion = (r: string) => r.split('#')[0];
const delay = <T>(v: T, ms = 60) => new Promise<T>((res) => setTimeout(() => res(v), ms));

const ROLE_OF: Record<string, string> = Object.fromEntries(Object.entries(USERS).map(([role, u]) => [u.id, role]));

interface MockBatch {
  b: StagedBatch;
  actor: string;
  reasons: string[];
}

export class MockDataSource implements DataSource {
  readonly mode = 'mock' as const;
  private w: MockWorld;
  private seq = 900_000;
  /** 模拟 belegt-core BatchStager（06 §5.2 服务端暂存）：批次只存 approval ID，窗口结束复检后落图或作废 */
  private batches = new Map<string, MockBatch>();
  undoWindowMinutes: number = APPROVAL_POLICY_V1.batch_undo_window_minutes;
  /** 可注入时钟（测试） */
  clock: () => number = () => Date.now();
  constructor(world?: MockWorld, private latency = 60) {
    this.w = world ?? generateWorld();
  }

  today() {
    return this.w.today;
  }

  private headAnswer(id: string): Answer | undefined {
    return this.w.answers.find((a) => a.id === id);
  }

  private progress(reqId: string): RequestView['progress'] {
    const st = this.w.staticProgress[reqId];
    if (st) return st;
    const qs = this.w.questions.filter((q) => q.request_id === reqId);
    const qIds = new Set(qs.map((q) => q.id));
    const ans = this.w.answers.filter((a) => a.question_refs?.some((r) => qIds.has(r)));
    const approved = ans.filter((a) => a.status === 'approved' || a.status === 'submitted').length;
    const answeredOnly = qs.filter((q) => q.status === 'answered' && !ans.some((a) => a.question_refs?.includes(q.id))).length;
    const pending = this.w.approvals.filter((a) => a.request_id === reqId && a.status === 'pending');
    const minutes = pending.reduce((m, a) => m + (a.risk_level === 'L0' ? 0.1 : a.risk_level === 'L1' ? 0.5 : a.risk_level === 'L2' ? 2 : 3), 0);
    const openConf = this.w.conflicts.filter((c) => c.request_id === reqId && c.status === 'open');
    const openCards = this.w.closedQuestions.filter((c) => c.request_id === reqId && c.status === 'open');
    return {
      questions: qs.length,
      mapped: qs.filter((q) => q.status === 'mapped' || q.status === 'answered').length,
      evidenced: ans.filter((a) => (a.evidence_refs?.length ?? 0) > 0 || !!a.calculation_ref || a.value_type === 'text').length + answeredOnly,
      approved: approved + answeredOnly,
      submitted: ans.filter((a) => a.status === 'submitted').length,
      est_minutes_remaining: Math.ceil(minutes + openConf.length * 5 + openCards.length * 0.5),
      open_conflicts: openConf.length,
      open_cards: openCards.length,
    };
  }

  async listRequests(): Promise<RequestView[]> {
    const views = this.w.requests.map((r) => ({
      request: r,
      counterparty_name: this.w.counterparties.find((c) => c.id === r.counterparty_id)?.legal_name ?? r.counterparty_id,
      progress: this.progress(r.id),
    }));
    return delay(structuredClone(views), this.latency);
  }

  async listQuestions(requestId: string): Promise<QuestionRow[]> {
    const rows = this.w.questions
      .filter((q) => q.request_id === requestId)
      .sort((a, b) => a.ordinal - b.ordinal)
      .map((q) => {
        const answer = this.w.answers.find((a) => a.question_refs?.includes(q.id));
        return { question: q, answer, risk_level: answer?.risk_level, evidence_count: answer?.evidence_refs?.length ?? 0 };
      });
    return delay(structuredClone(rows), this.latency);
  }

  private buildInboxItem(ap: MockWorld['approvals'][number]): InboxItem | undefined {
    const ansId = stripVersion(ap.subject_ref);
    const answer = this.headAnswer(ansId);
    if (!answer) return undefined;
    const req = this.w.requests.find((r) => r.id === ap.request_id);
    const evIds = new Set((answer.evidence_refs ?? []).map(stripVersion));
    const calc = answer.calculation_ref ? this.w.calculations.find((c) => c.id === stripVersion(answer.calculation_ref!)) : undefined;
    return {
      approval: ap,
      answer,
      head_version: this.w.answerHead[answer.id] ?? answer.version,
      questions: this.w.questions.filter((q) => answer.question_refs?.includes(q.id)),
      evidence: this.w.evidence.filter((e) => evIds.has(e.id)),
      calculation: calc,
      conflicts: this.w.conflicts.filter((c) => c.subject_refs.some((r) => stripVersion(r) === answer.id)),
      customer_tier: req ? this.w.tiers[req.counterparty_id] : undefined,
      request_title: req?.title ?? '',
      request_due_date: req?.due_date,
    };
  }

  async listInbox(): Promise<InboxItem[]> {
    const items = this.w.approvals
      .filter((a) => a.status === 'pending' && !a.subject_ref.startsWith('sreq_'))
      .map((a) => this.buildInboxItem(a))
      .filter((x): x is InboxItem => !!x);
    return delay(structuredClone(items), this.latency);
  }

  private applyDecision(input: DecisionInput, actor: string, batchId?: string) {
    const ap = this.w.approvals.find((a) => a.id === input.approval_id);
    if (!ap) throw new ApiException({ code: 'GRAPH_REF_NOT_FOUND', message: 'Approval not found' }, 404);
    if (ap.status !== 'pending') throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'Approval already decided' }, 409);
    const role = ROLE_OF[actor] as Parameters<typeof canApprove>[0];
    const answer = this.headAnswer(stripVersion(ap.subject_ref));
    const req = this.w.requests.find((r) => r.id === ap.request_id);
    const tier = req ? this.w.tiers[req.counterparty_id] : undefined;
    // R-APR：角色不足或 L3 批量
    if (!role || !canApprove(role, ap.risk_level, { legal_commitment: answer?.legal_commitment, customer_tier: tier, required_role: ap.required_role })) {
      throw new ApiException({ code: 'GRAPH_APPROVAL_REQUIRED', message: 'Role not permitted for this approval', rule_id: 'R-APR' });
    }
    if (batchId && (ap.risk_level === 'L3' || answer?.legal_commitment)) {
      throw new ApiException({ code: 'GRAPH_APPROVAL_REQUIRED', message: 'L3 cannot be batch approved', rule_id: 'R-APR' });
    }
    if (input.decision === 'rejected' && !input.comment?.trim()) {
      throw new ApiException({ code: 'GRAPH_SCHEMA_INVALID', message: 'Rejection requires a reason', rule_id: 'R-SCHEMA' }, 400);
    }
    if (input.decision === 'modified' && answer?.value_type === 'number') {
      const m = input.modification;
      const ok = m && ((m.source === 'existing_evidence' && m.evidence_ref) || (m.source === 'upload' && m.blob_sha256) || (m.source === 'human_confirmation' && m.value && m.note?.trim()));
      if (!ok) throw new ApiException({ code: 'GRAPH_NUM_UNREFERENCED', message: 'A number without evidence cannot be saved', rule_id: 'R-NUM' });
    }
    const now = new Date().toISOString();
    ap.status = input.decision;
    ap.decision = input.decision;
    ap.decided_by = actor;
    ap.decided_at = now;
    ap.comment = input.comment;
    ap.version += 1;
    if (batchId) ap.batch_id = batchId;
    if (answer) {
      if (input.decision === 'approved') {
        answer.status = 'approved';
        answer.approval_ref = ref(ap.id, ap.version);
      } else if (input.decision === 'rejected') {
        answer.status = 'rejected';
      } else {
        // 修改：先写 human_confirmation Evidence，再生成引用它的新 Answer 版本（03 §3.7）
        const m = input.modification!;
        let evRef = m.evidence_ref;
        if (m.source !== 'existing_evidence') {
          const ev: Evidence = {
            id: mkId('ev_', this.seq++),
            org_id: answer.org_id,
            version: 1,
            created_at: now,
            created_by: actor,
            trust_zone: 'trusted',
            provenance: mkId('trc_', this.seq++),
            supersedes: null,
            schema_version: answer.schema_version,
            content_hash: fakeSha(now),
            source_type: m.source === 'upload' ? 'document' : 'human_confirmation',
            fact_key: answer.datapoint_id,
            extracted_value: m.value ?? String(answer.value),
            value_type: answer.value_type,
            unit: answer.unit,
            boundary: answer.boundary,
            locator: m.source === 'upload' ? { blob_sha256: m.blob_sha256!, mime: 'application/pdf' } : { blob_sha256: fakeSha(actor + now), mime: 'application/json', approval_ref: ref(ap.id, ap.version) },
            valid_from: answer.period.start,
            confidence: 1,
            verification: { method: 'human_review', by: actor },
            status: m.source === 'upload' ? 'candidate' : 'verified',
          };
          this.w.evidence.push(ev);
          evRef = ref(ev.id, 1);
        }
        const newVersion = answer.version + 1;
        answer.version = newVersion;
        answer.supersedes = ref(answer.id, newVersion - 1);
        answer.status = 'draft';
        if (m.value) answer.value = m.value;
        else if (m.evidence_ref) {
          const ev = this.w.evidence.find((e) => e.id === stripVersion(m.evidence_ref!));
          if (ev && typeof ev.extracted_value === 'string') answer.value = ev.extracted_value;
        }
        answer.evidence_refs = [...(answer.evidence_refs ?? []), evRef!];
        ap.modified_answer_ref = ref(answer.id, newVersion);
      }
    }
  }

  async decide(input: DecisionInput, actor: string): Promise<void> {
    this.applyDecision(input, actor);
    return delay(undefined, this.latency);
  }

  // ------------------------------------------------------------------ 06 §5.2 批量：服务端暂存（mock）
  private toServer(m: MockBatch): ServerBatch {
    const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const until = m.b.staged_at + m.b.window_ms;
    return {
      batch_id: m.b.batch_id,
      status: m.b.status,
      approval_ids: [...m.b.approval_ids],
      staged_at: iso(m.b.staged_at),
      commit_at: iso(until),
      undo_until: iso(until),
      decided_by: m.actor,
      reasons: [...m.reasons],
    };
  }

  private newBatchId(): string {
    return mkId('apb_', this.seq++);
  }

  /** 到期批次：复检条件 2–4 → 同事务落图，否则整批作废（与 BatchStager.settle 相同语义）。 */
  private settleDue(): void {
    const now = this.clock();
    for (const m of this.batches.values()) {
      if (!isDue(m.b, now)) continue;
      this.commit(m);
    }
  }

  private commit(m: MockBatch): void {
    const now = this.clock();
    const current = m.b.approval_ids
      .map((id) => this.w.approvals.find((a) => a.id === id))
      .filter((a): a is MockWorld['approvals'][number] => !!a && a.status === 'pending')
      .map((a) => this.buildInboxItem(a))
      .filter((x): x is InboxItem => !!x);
    const re = recheckBeforeCommit(current, new Date(now));
    if (current.length !== m.b.approval_ids.length || !re.ok) {
      m.reasons = re.failing.map((f) => `${f.approval_id}: ${f.reasons.join(',')}`);
      if (current.length !== m.b.approval_ids.length) m.reasons.push('approval no longer pending');
      m.b = settle(m.b, now, false); // 只在到期（或 immediate）时调用
      return;
    }
    for (const id of m.b.approval_ids) this.applyDecision({ approval_id: id, decision: 'approved' }, m.actor, m.b.batch_id);
    m.b = settle(m.b, now, true);
  }

  async stageBatch(input: StageBatchInput, actor: string): Promise<ServerBatch> {
    this.settleDue();
    const ids = [...new Set(input.approval_ids)];
    if (!ids.length) throw new ApiException({ code: 'GRAPH_SCHEMA_INVALID', message: 'empty batch' }, 400);
    const viewed = new Set(input.viewed_approval_ids ?? []);
    const role = ROLE_OF[actor] as Parameters<typeof canApprove>[0];
    const reasons: string[] = [];
    let dueSoon = false;
    const minutes = clampUndoWindowMinutes(this.undoWindowMinutes);
    for (const id of ids) {
      const ap = this.w.approvals.find((a) => a.id === id);
      if (!ap || ap.status !== 'pending') {
        reasons.push(`${id}: not pending`);
        continue;
      }
      const answer = this.headAnswer(stripVersion(ap.subject_ref));
      if (ap.risk_level === 'L3' || answer?.legal_commitment) reasons.push(`${id}: L3 cannot be batch-approved (R-APR)`);
      if (!role || !canApprove(role, ap.risk_level, { legal_commitment: answer?.legal_commitment, required_role: ap.required_role })) {
        reasons.push(`${id}: approver lacks role for ${ap.risk_level}`);
      }
      if (ap.risk_level === 'L2' && !viewed.has(id)) reasons.push(`${id}: L2 requires the detail view to be opened at least once`);
      if (ap.risk_level === 'L2' && answer?.is_estimate && !input.confirmations?.estimate_ack) reasons.push(`${id}: L2 estimate requires estimate_ack`);
      const req = this.w.requests.find((r) => r.id === ap.request_id);
      if (dueSoonerThanWindow(req?.due_date, this.clock(), minutes)) dueSoon = true;
    }
    if (reasons.length) {
      throw new ApiException({ code: 'GRAPH_APPROVAL_REQUIRED', message: 'batch approval conditions not met (06 §5.1)', rule_id: '06-5.1', details: { reasons } }, 422);
    }
    if (input.immediate && !dueSoon) {
      throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'immediate commit is only offered when a Request due date is closer than the undo window (06 §5.2)' }, 409);
    }
    const m: MockBatch = { b: stageLocal(this.newBatchId(), ids, this.clock(), minutes, !!input.immediate), actor, reasons: [] };
    this.batches.set(m.b.batch_id, m);
    if (input.immediate) this.commit(m);
    return delay(this.toServer(m), this.latency);
  }

  async undoBatch(batchId: string, approvalIds: string[] | undefined, actor: string): Promise<ServerBatch> {
    this.settleDue();
    const m = this.batches.get(batchId);
    if (!m) throw new ApiException({ code: 'GRAPH_NOT_FOUND', message: `batch ${batchId} not found` }, 404);
    if (m.actor !== actor && ROLE_OF[actor] !== 'compliance_owner') {
      throw new ApiException({ code: 'GRAPH_ACTOR_NOT_ALLOWED', message: 'only the approver may undo the batch' }, 403);
    }
    const now = this.clock();
    if (!canUndo(m.b, now)) {
      throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'undo window has ended (06 §5.2)', details: { status: m.b.status } }, 409);
    }
    m.b = approvalIds ? removeFromBatch(m.b, approvalIds, now) : undoLocal(m.b, now);
    return delay(this.toServer(m), this.latency);
  }

  async getBatch(batchId: string, _actor: string): Promise<ServerBatch> {
    this.settleDue();
    const m = this.batches.get(batchId);
    if (!m) throw new ApiException({ code: 'GRAPH_NOT_FOUND', message: `batch ${batchId} not found` }, 404);
    return delay(this.toServer(m), this.latency);
  }

  async listClosedQuestions(): Promise<ClosedQuestion[]> {
    return delay(structuredClone(this.w.closedQuestions), this.latency);
  }

  async answerClosedQuestion(input: CardAnswerInput, actor: string): Promise<void> {
    const cq = this.w.closedQuestions.find((c) => c.id === input.cq_id);
    if (!cq) throw new ApiException({ code: 'GRAPH_REF_NOT_FOUND', message: 'Card not found' }, 404);
    if (cq.status !== 'open') throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'Card already answered' }, 409);
    if (cq.kind === 'file_request' && !(input.blob_sha256?.length || input.bool === false)) {
      throw new ApiException({ code: 'GRAPH_SCHEMA_INVALID', message: 'file_request needs ≥1 blob or bool=false' }, 400);
    }
    cq.status = 'answered';
    cq.version += 1;
    cq.answer = { choice: input.choice, bool: input.bool, corrected_value: input.corrected_value, blob_sha256: input.blob_sha256, answered_by: actor, answered_at: new Date().toISOString() };
    // 同事务推进目标对象（03 §3.11 不变量）
    const target = this.w.evidence.find((e) => e.id === stripVersion(cq.target_ref));
    if (target && cq.kind === 'numeric_confirm') target.status = input.corrected_value ? 'superseded' : 'verified';
    if (cq.target_ref.startsWith('map_')) {
      const q = this.w.questions.find((x) => x.native_id === 'C8.2a');
      if (q) q.status = input.choice === 'C' ? 'unmapped' : 'mapped';
    }
    return delay(undefined, this.latency);
  }

  async uploadBlob(file: File): Promise<string> {
    // POST /blobs 的 mock：计算真实 sha256（浏览器 SubtleCrypto）
    const buf = await file.arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', buf);
    const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
    return `sha256:${hex}`;
  }

  async listConflicts(): Promise<ConflictView[]> {
    const views: ConflictView[] = this.w.conflicts.map((c) => ({
      conflict: c,
      sides: c.subject_refs
        .map((r) => this.headAnswer(stripVersion(r)))
        .filter((a): a is Answer => !!a)
        .map((a) => {
          const scope = a.customer_scope;
          const name = scope === 'all' ? 'all' : scope.map((id) => this.w.counterparties.find((cp) => cp.id === id)?.legal_name ?? id).join(', ');
          const calc = a.calculation_ref ? this.w.calculations.find((k) => k.id === stripVersion(a.calculation_ref!)) : undefined;
          return { answer: a, customer_name: name, evidence_count: a.evidence_refs?.length ?? 0, methodology_version: calc?.methodology_version };
        }),
      questions: this.w.questions.filter((q) => c.subject_refs.some((r) => this.headAnswer(stripVersion(r))?.question_refs?.includes(q.id))),
    }));
    return delay(structuredClone(views), this.latency);
  }

  async resolveConflict(input: ResolveConflictInput, actor: string): Promise<void> {
    const c = this.w.conflicts.find((x) => x.id === input.conflict_id);
    if (!c) throw new ApiException({ code: 'GRAPH_REF_NOT_FOUND', message: 'Conflict not found' }, 404);
    if (c.status !== 'open') throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'Conflict not open' }, 409);
    const role = ROLE_OF[actor];
    if ((input.kind === 'accepted_divergence' || input.kind === 'dismissed') && c.severity === 'blocking' && role !== 'compliance_owner') {
      throw new ApiException({ code: 'GRAPH_ACTOR_NOT_ALLOWED', message: 'Only compliance_owner', rule_id: '06 §3.2' }, 403);
    }
    if (input.kind === 'accepted_divergence') {
      const d = input.divergence;
      if (!d) throw new ApiException({ code: 'GRAPH_SCHEMA_INVALID', message: 'divergence required' }, 400);
      if (scopesOverlap(d.sides[0].customer_scope, d.sides[1].customer_scope).length) {
        throw new ApiException({ code: 'GRAPH_SCHEMA_INVALID', message: 'customer_scope overlap', rule_id: 'R-SCHEMA' }, 400);
      }
    }
    const aprId = mkId('apr_', this.seq++);
    c.version += 1;
    c.status = input.kind === 'corrected' ? 'resolved' : input.kind;
    c.resolution = { kind: input.kind, note: input.note, approval_ref: ref(aprId, 1), resolved_by: actor, divergence: input.divergence };
    // 全部 blocking 冲突关闭 ⇒ awaiting_human → return_to（06 §3.4）
    const req = this.w.requests.find((r) => r.id === c.request_id);
    if (req && req.status === 'awaiting_human' && !this.w.conflicts.some((x) => x.request_id === req.id && x.status === 'open' && x.severity === 'blocking')) {
      req.status = 'auditing';
      req.status_reason = undefined;
      req.version += 1;
    }
    return delay(undefined, this.latency);
  }

  async listSupplierRequests(): Promise<SupplierRequestView[]> {
    return delay(structuredClone(this.w.supplierRequests), this.latency);
  }

  async approveSupplierFirstSend(id: string, actor: string): Promise<void> {
    const v = this.w.supplierRequests.find((s) => s.supplier_request.id === id);
    if (!v) throw new ApiException({ code: 'GRAPH_REF_NOT_FOUND', message: 'SupplierRequest not found' }, 404);
    if (ROLE_OF[actor] !== 'compliance_owner') throw new ApiException({ code: 'GRAPH_APPROVAL_REQUIRED', message: 'Role not permitted', rule_id: 'R-APR' });
    if (v.supplier_request.status !== 'draft') throw new ApiException({ code: 'GRAPH_ILLEGAL_TRANSITION', message: 'Not draft' }, 409);
    const now = new Date().toISOString();
    v.supplier_request.status = 'sent';
    v.supplier_request.version += 1;
    v.supplier_request.last_sent_at = now;
    v.history.push({ version: v.supplier_request.version, status: 'sent', created_at: now, reminder_count: 0, created_by: actor });
    return delay(undefined, this.latency);
  }

  async listEvidence(): Promise<Evidence[]> {
    return delay(structuredClone(this.w.evidence), this.latency);
  }

  async lineage(rootRef: string): Promise<Lineage> {
    const nodes: LineageNode[] = [];
    const edges: LineageEdge[] = [];
    const seen = new Set<string>();
    const addEvidence = (r: string, from: string) => {
      const ev = this.w.evidence.find((e) => e.id === stripVersion(r));
      if (!ev) return;
      edges.push({ from, to: r, kind: 'cites' });
      if (seen.has(r)) return;
      seen.add(r);
      nodes.push({ ref: r, type: 'Evidence', object: ev, ui_excerpt: this.w.excerpts[ev.id] });
    };
    const a = this.headAnswer(stripVersion(rootRef));
    if (a) {
      const aref = ref(a.id, a.version);
      nodes.push({ ref: aref, type: 'Answer', object: a });
      seen.add(aref);
      if (a.calculation_ref) {
        const c = this.w.calculations.find((x) => x.id === stripVersion(a.calculation_ref!));
        if (c) {
          nodes.push({ ref: a.calculation_ref, type: 'Calculation', object: c });
          edges.push({ from: aref, to: a.calculation_ref, kind: 'derived_from' });
          c.inputs.forEach((i) => addEvidence(i.ref, a.calculation_ref!));
          const fs = this.w.factorSets.find((f) => f.id === c.factor_set_version);
          if (fs) {
            nodes.push({ ref: fs.id, type: 'FactorSet', object: fs });
            edges.push({ from: a.calculation_ref, to: fs.id, kind: 'uses_factor_set' });
          }
        }
      }
      (a.evidence_refs ?? []).forEach((r) => {
        if (!seen.has(r)) addEvidence(r, aref);
      });
    }
    return delay({ root: rootRef, nodes: structuredClone(nodes), edges }, this.latency);
  }

  async exportAuditPack(requestId: string): Promise<{ filename: string; blob: Blob }> {
    // 03 §7：真实后端返回 belegt-auditpack/1 zip；mock 只导出 manifest 等价 JSON（便于演示）
    const req = this.w.requests.find((r) => r.id === requestId);
    const qIds = new Set(this.w.questions.filter((q) => q.request_id === requestId).map((q) => q.id));
    const answers = this.w.answers.filter((a) => a.question_refs?.some((r) => qIds.has(r)));
    const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    const files = [
      { path: 'request/request.json', body: req },
      { path: 'answers.jsonl', body: answers },
      { path: 'approvals.jsonl', body: this.w.approvals.filter((a) => a.request_id === requestId) },
      { path: 'conflicts.jsonl', body: this.w.conflicts.filter((c) => c.request_id === requestId) },
      { path: 'closed_questions.jsonl', body: this.w.closedQuestions.filter((c) => c.request_id === requestId) },
    ].map((f) => {
      const s = JSON.stringify(f.body);
      return { path: f.path, sha256: fakeSha(s), bytes: s.length };
    });
    const manifest = {
      format: 'belegt-auditpack/1',
      req_id: requestId,
      org_id: req?.org_id,
      as_of_tx: new Date().toISOString(),
      generated_at: new Date().toISOString(),
      generated_by: 'service:web-mock',
      dictionary_release: 'belegt-dictionary v2027.1',
      engine_versions: ['belegt-calc@0.3.0'],
      factor_sets: this.w.factorSets.map((f) => f.id),
      files,
      pack_hash: fakeSha(JSON.stringify(files)),
      note: 'Mock export (manifest only). The belegt-core API returns the signed zip.',
    };
    const blob = new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' });
    return delay({ filename: `belegt-auditpack_${requestId}_${ts}.manifest.json`, blob }, this.latency);
  }
}

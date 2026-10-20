// Spec: 03 §6.8（写入 API）、§7（审计包）、01 §3（UI 所需端点：/requests、/inbox、/approvals、/closed-questions、/conflicts）
import type {
  ClosedQuestion,
  ConflictView,
  Divergence,
  Evidence,
  InboxItem,
  Lineage,
  Question,
  Answer,
  RequestView,
  RiskLevel,
  SupplierRequestView,
} from '../types';

export interface QuestionRow {
  question: Question;
  answer?: Answer;
  risk_level?: RiskLevel;
  evidence_count: number;
}

export interface DecisionInput {
  approval_id: string;
  decision: 'approved' | 'rejected' | 'modified';
  comment?: string;
  /** 06 §4.3 L2 warn 处置说明 / L3 二次确认记录 */
  confirmations?: { estimate_ack?: boolean; warn_note?: string; step_up_auth?: boolean; clause_read_ack?: boolean };
  /** 01 §3.1 修改数字：必须选择出处（R-NUM） */
  modification?: {
    source: 'existing_evidence' | 'upload' | 'human_confirmation';
    evidence_ref?: string;
    blob_sha256?: string;
    value?: string;
    note?: string;
  };
}

export interface ResolveConflictInput {
  conflict_id: string;
  kind: 'corrected' | 'accepted_divergence' | 'dismissed';
  note: string;
  base_side?: number; // 统一口径：选定基准侧
  dismiss_reason?: string;
  divergence?: Divergence;
}

export interface CardAnswerInput {
  cq_id: string;
  choice?: string;
  bool?: boolean;
  corrected_value?: string;
  blob_sha256?: string[];
}

/** 06 §5.2：服务端暂存的批量批准（approval-router 工作流状态；``POST /approvals`` 批量体）。 */
export interface StageBatchInput {
  approval_ids: string[];
  /** 06 §5.1 L2 附加条件"每条至少打开一次详情"：服务端无从得知，由客户端报告 */
  viewed_approval_ids: string[];
  confirmations?: { estimate_ack?: boolean; warn_note?: string };
  /** due_date 剩余时间小于撤销窗口时才允许（放弃撤销能力） */
  immediate?: boolean;
}

export type ServerBatchStatus = 'staged' | 'committed' | 'voided' | 'undone';

/** ``StagedBatch.to_dict()``（belegt-core approval/batch.py） */
export interface ServerBatch {
  batch_id: string; // apb_…
  status: ServerBatchStatus;
  approval_ids: string[];
  answer_refs?: string[];
  staged_at: string;
  commit_at: string;
  undo_until: string;
  decided_by?: string;
  reasons?: string[];
  committed_refs?: string[];
}

export interface ApiError {
  code: string; // GRAPH_*（03 §6.8）
  message: string;
  rule_id?: string;
  details?: Record<string, unknown>;
}

export class ApiException extends Error {
  constructor(public error: ApiError, public status = 422) {
    super(error.message);
  }
}

export interface DataSource {
  readonly mode: 'mock' | 'http';
  /** 业务"今天"（ISO 日期） */
  today(): string;
  listRequests(): Promise<RequestView[]>;
  listQuestions(requestId: string): Promise<QuestionRow[]>;
  listInbox(): Promise<InboxItem[]>;
  decide(input: DecisionInput, actor: string): Promise<void>;
  /** 06 §5.2：批量批准由服务端暂存（返回 202 + undo_until）；窗口结束服务端复检条件 2–4 后落图或整批作废 */
  stageBatch(input: StageBatchInput, actor: string): Promise<ServerBatch>;
  /** 窗口内整批（approvalIds 省略）或部分撤销 */
  undoBatch(batchId: string, approvalIds: string[] | undefined, actor: string): Promise<ServerBatch>;
  /** 读取批次状态（服务端读取时顺带结算到期批次） */
  getBatch(batchId: string, actor: string): Promise<ServerBatch>;
  listClosedQuestions(): Promise<ClosedQuestion[]>;
  answerClosedQuestion(input: CardAnswerInput, actor: string): Promise<void>;
  uploadBlob(file: File): Promise<string>;
  listConflicts(): Promise<ConflictView[]>;
  resolveConflict(input: ResolveConflictInput, actor: string): Promise<void>;
  listSupplierRequests(): Promise<SupplierRequestView[]>;
  approveSupplierFirstSend(supplierRequestId: string, actor: string): Promise<void>;
  listEvidence(): Promise<Evidence[]>;
  lineage(ref: string): Promise<Lineage>;
  exportAuditPack(requestId: string): Promise<{ filename: string; blob: Blob }>;
}

// Spec: 03 §2（通用字段与值表示）、§3.2–§3.13（核心对象）；命名基准 _baseline §B/§E/§F/§G
// 字段名与 03 完全一致。仅以 `ui_` 前缀或单独的 *View 类型承载界面辅助数据（不属于证据图）。

export type ObjectRef = string; // "<id>#v<version>"
export type DecStr = string; // 十进制字符串（DEC-STR，03 §2.2）
export type IsoDate = string;
export type IsoDateTime = string;

export interface Period {
  start: IsoDate;
  end: IsoDate;
}

/** 03 §2.2：多语言文本，至少一种语言。界面额外支持 zh。 */
export type I18nText = Partial<Record<'de' | 'en' | 'zh', string>>;

export type TrustZone = 'quarantine' | 'trusted';

export interface GraphBase {
  id: string;
  org_id: string;
  version: number;
  created_at: IsoDateTime;
  created_by: string; // user:<id> | agent:<agent_id> | service:<svc_id>
  trust_zone: TrustZone;
  provenance: string; // trc_…
  supersedes: ObjectRef | null;
  schema_version: string;
  content_hash: string;
  tags?: string[];
}

// _baseline §E
export type RequestStatus =
  | 'received'
  | 'triaged'
  | 'parsed'
  | 'mapped'
  | 'evidence_gathering'
  | 'drafting'
  | 'auditing'
  | 'in_review'
  | 'approved'
  | 'filling'
  | 'submitted'
  | 'archived'
  | 'awaiting_human'
  | 'awaiting_supplier'
  | 'halted'
  | 'cancelled';

export interface Request extends GraphBase {
  counterparty_id: string;
  channel: 'email' | 'portal' | 'upload' | 'api' | 'a2a';
  source_ref: { message_id?: string; portal_task_id?: string; blob_sha256: string[] };
  template_ref: string | null;
  title: string;
  language: string;
  due_date?: IsoDate;
  reporting_period?: Period;
  priority: 'low' | 'normal' | 'high' | 'urgent';
  triage_confidence: number;
  status: RequestStatus;
  status_reason?: string;
  workflow_id?: string;
  assignee?: string;
}

export interface Locator {
  blob_sha256: string;
  mime: string;
  page?: number;
  sheet?: string;
  cell?: string;
  bbox?: [number, number, number, number];
  text_excerpt_hash?: string;
  connector?: { connector_id: string; query_hash: string; record_key: string; fetched_at: IsoDateTime };
  cq_ref?: string;
  approval_ref?: string;
}

export interface Question extends GraphBase {
  request_id: string;
  ordinal: number;
  native_id?: string;
  text: I18nText;
  answer_format: 'number' | 'text' | 'enum' | 'bool' | 'date' | 'file' | 'geo' | 'table';
  options?: string[];
  unit_requested?: string;
  period_requested?: Period;
  locator: Locator;
  parent_question_id?: string;
  status: 'parsed' | 'mapped' | 'unmapped' | 'answered';
}

export type ValueType = 'number' | 'text' | 'enum' | 'bool' | 'date' | 'file' | 'geo';

export interface Evidence extends GraphBase {
  source_type: 'document' | 'erp' | 'human_confirmation' | 'supplier_response' | 'historical_answer' | 'public_source';
  datapoint_id?: string;
  fact_key: string;
  extracted_value: DecStr | string | boolean | Record<string, unknown>;
  value_type: ValueType;
  unit?: string;
  period?: Period;
  boundary: string;
  locator: Locator;
  valid_from: IsoDate;
  valid_until?: IsoDate;
  confidence: number;
  verification?: { method: 'auto_rule' | 'closed_question' | 'human_review' | 'connector_attested'; cq_ref?: string; rule_id?: string; by: string };
  supplier_request_ref?: string;
  corrects?: ObjectRef;
  plausibility?: { flag: 'ok' | 'outlier' | 'unit_suspect'; basis?: string };
  status: 'candidate' | 'verified' | 'expired' | 'superseded' | 'rejected';
}

export interface Calculation extends GraphBase {
  method_id: string;
  methodology_version: string;
  factor_set_version: string;
  engine_version: string;
  inputs: { name: string; ref: ObjectRef; value: DecStr; unit: string }[];
  factors_used: { factor_key: string; value: DecStr; unit: string; source_row_id: string }[];
  params?: Record<string, string>;
  result: { value: DecStr; unit: string; rounding: { mode: string; digits: number } };
  breakdown?: { name: string; value: DecStr; unit: string }[];
  input_hash: string;
  output_hash: string;
  boundary: string;
  period: Period;
  customer_scope: string[] | 'all';
  status: 'computed' | 'superseded';
}

export type RiskLevel = 'L0' | 'L1' | 'L2' | 'L3';

export type AnswerStatus = 'draft' | 'in_review' | 'approved' | 'rejected' | 'submitted' | 'superseded';

export interface Answer extends GraphBase {
  datapoint_id: string;
  value: DecStr | string | boolean | { blob_sha256: string } | Record<string, unknown>;
  value_type: ValueType;
  unit?: string;
  text_lang?: string;
  period: Period;
  boundary: string;
  consolidation?: 'operational_control' | 'financial_control' | 'equity_share';
  customer_scope: string[] | 'all';
  question_refs?: string[];
  evidence_refs?: ObjectRef[];
  calculation_ref?: ObjectRef | null;
  reused_from?: ObjectRef;
  origin: 'generated' | 'imported' | 'human';
  answer_key: string;
  status: AnswerStatus;
  risk_level?: RiskLevel;
  is_estimate: boolean;
  estimate_method?: string;
  legal_commitment: boolean;
  approval_ref?: string;
  submission?: { request_id: string; channel: string; submitted_at: IsoDateTime; export_input_hash: string };
  drafting_note?: I18nText;
}

// _baseline §G
export type Role =
  | 'compliance_owner'
  | 'executive'
  | 'sales'
  | 'contributor'
  | 'supplier_user'
  | 'customer_buyer'
  | 'org_admin'
  | 'auditor_readonly';

export interface Approval extends GraphBase {
  subject_ref: ObjectRef;
  request_id?: string;
  risk_level: RiskLevel;
  required_role: Role[];
  assignee?: string;
  decision?: 'approved' | 'rejected' | 'modified';
  decided_by?: string;
  decided_at?: IsoDateTime;
  comment?: string;
  modified_answer_ref?: ObjectRef;
  batch_id?: string; // apb_…
  policy_version: string;
  status: 'pending' | 'approved' | 'rejected' | 'modified';
}

export type ReasonCode = 'customer_methodology' | 'consolidation' | 'period' | 'product_scope' | 'boundary' | 'timing_correction';

export interface Divergence {
  format: 'ADR/1';
  reason_code: ReasonCode;
  customer_requirement_refs: string[];
  sides: { answer: ObjectRef; customer_scope: string[] | 'all'; basis: string }[];
  disclosure_text: I18nText;
  include_in_export: boolean;
  review_by: IsoDate;
}

export type ConflictStatus = 'open' | 'resolved' | 'accepted_divergence' | 'dismissed';

export interface Conflict extends GraphBase {
  rule_id: string;
  severity: 'blocking' | 'warning';
  datapoint_id: string;
  subject_refs: ObjectRef[];
  detail: { values: { ref: ObjectRef; value: DecStr | string; unit?: string }[]; delta?: DecStr; threshold?: DecStr };
  explanation?: I18nText;
  request_id?: string;
  resolution?: {
    kind: 'corrected' | 'accepted_divergence' | 'dismissed' | 'auto_cleared';
    note: string;
    approval_ref: string;
    resolved_by: string;
    divergence?: Divergence;
  };
  status: ConflictStatus;
}

export type SupplierRequestStatus = 'draft' | 'sent' | 'reminded' | 'responded' | 'reviewed' | 'closed' | 'escalated' | 'expired';

export interface SupplierRequest extends GraphBase {
  counterparty_id: string;
  datapoint_ids: string[];
  product_ids?: string[];
  period: Period;
  language: string;
  template_id: string;
  channel: 'email' | 'supplier_portal';
  portal_token_hash?: string;
  due_date: IsoDate;
  reminder_count: number;
  last_sent_at?: IsoDateTime;
  trigger_ref?: ObjectRef;
  default_cost_estimate_ref?: ObjectRef;
  status: SupplierRequestStatus;
}

export type ClosedQuestionKind = 'yes_no' | 'single_choice' | 'numeric_confirm' | 'file_request';

export interface ClosedQuestion extends GraphBase {
  target_ref: ObjectRef;
  request_id?: string;
  kind: ClosedQuestionKind;
  prompt: I18nText;
  options?: { key: string; label: I18nText; ref?: ObjectRef }[];
  proposed_value?: { value: DecStr; unit: string; evidence_ref: ObjectRef };
  context_refs?: ObjectRef[];
  assignee_role: Role;
  assignee?: string;
  expires_at: IsoDateTime;
  answer?: {
    choice?: string;
    bool?: boolean;
    corrected_value?: DecStr;
    blob_sha256?: string[];
    answered_by: string;
    answered_at: IsoDateTime;
  };
  status: 'open' | 'answered' | 'expired';
}

export interface Counterparty extends GraphBase {
  role: 'customer' | 'supplier';
  legal_name: string;
  country: string;
}

export interface FactorSet {
  id: string; // fs:<source>@<version>
  source: string;
  license: string;
  published_at: IsoDate;
  valid_from: IsoDate;
  valid_until?: IsoDate;
  row_count: number;
  rows_hash: string;
  status: 'active' | 'deprecated';
}

// ───────────── 界面视图（API 响应包装，非证据图对象） ─────────────

/** GET /requests 的元素：Request head + 进度聚合（01 §3.4 进度条四段）。 */
export interface RequestView {
  request: Request;
  counterparty_name: string;
  progress: {
    questions: number;
    mapped: number;
    evidenced: number;
    approved: number;
    submitted: number;
    est_minutes_remaining: number;
    open_conflicts: number;
    open_cards: number;
  };
}

/** GET /inbox 的元素：pending Approval + 被批 Answer（钉住版本）+ 侧栏所需对象。 */
export interface InboxItem {
  approval: Approval;
  answer: Answer;
  head_version: number; // Answer 当前 head 版本；≠ answer.version ⇒ stale（03 §3.8）
  questions: Question[];
  evidence: Evidence[];
  calculation?: Calculation;
  conflicts: Conflict[];
  customer_tier?: 'tier_a' | 'tier_b' | 'tier_c';
  request_title: string;
  request_due_date?: IsoDate;
}

export interface ConflictView {
  conflict: Conflict;
  sides: { answer: Answer; customer_name: string; evidence_count: number; methodology_version?: string }[];
  questions: Question[];
}

export interface SupplierRequestView {
  supplier_request: SupplierRequest;
  supplier_name: string;
  history: { version: number; status: SupplierRequestStatus; created_at: IsoDateTime; reminder_count: number; created_by: string }[];
  ui_cost_gap_eur?: DecStr; // 来自 default_cost_estimate_ref 指向的 Calculation（05 §6.6）
}

export interface LineageNode {
  ref: ObjectRef;
  type: 'Answer' | 'Calculation' | 'Evidence' | 'FactorSet' | 'ClosedQuestion' | 'Approval';
  object: Answer | Calculation | Evidence | FactorSet | ClosedQuestion | Approval;
  ui_excerpt?: string; // 原文片段预览（界面辅助）
}

export interface LineageEdge {
  from: ObjectRef;
  to: ObjectRef;
  kind: 'derived_from' | 'cites' | 'uses_factor_set' | 'confirmed_by' | 'approved_by';
}

/** GET /graph/lineage/{ref} */
export interface Lineage {
  root: ObjectRef;
  nodes: LineageNode[];
  edges: LineageEdge[];
}

-- Spec: 03 §6.2（主要表）、§6.4（PARTITION BY HASH (org_id)）、§1.5 SSOT-2/3
-- 各对象版本表：公共列 + 冗余热点列（由 graph.commit_version() 从 body 填充并 CHECK 一致）。

CREATE TABLE IF NOT EXISTS graph.request_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  counterparty_id text NOT NULL,
  due_date date,
  priority text NOT NULL,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.request_v');

CREATE TABLE IF NOT EXISTS graph.question_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  request_id text NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 1),
  native_id text,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.question_v');

CREATE TABLE IF NOT EXISTS graph.mapping_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  question_id text NOT NULL,
  datapoint_id text NOT NULL,
  confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  method text NOT NULL,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.mapping_v');

CREATE TABLE IF NOT EXISTS graph.evidence_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  source_type text NOT NULL,
  fact_key text NOT NULL,
  value_num numeric,
  unit text,
  period_end date,
  valid_from date NOT NULL,
  valid_until date,
  blob_sha256 text,
  CONSTRAINT ev_validity CHECK (valid_until IS NULL OR valid_from <= valid_until),
  CONSTRAINT ev_quarantine_candidate CHECK (trust_zone = 'trusted' OR status = 'candidate'),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.evidence_v');

CREATE TABLE IF NOT EXISTS graph.calculation_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  method_id text NOT NULL,
  factor_set_version text NOT NULL,
  methodology_version text NOT NULL,
  engine_version text NOT NULL,
  input_hash text NOT NULL,
  output_hash text NOT NULL,
  result_num numeric NOT NULL,
  CONSTRAINT calc_trusted CHECK (trust_zone = 'trusted' AND created_by = 'agent:calc-service'),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.calculation_v');

CREATE TABLE IF NOT EXISTS graph.answer_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  datapoint_id    text    NOT NULL,                -- 含 SemVer
  datapoint_base  text    GENERATED ALWAYS AS (split_part(datapoint_id,'@',1)) STORED,
  value_type      text    NOT NULL,
  value_num       numeric,                         -- value_type=number 时非空
  unit            text,
  period_start    date    NOT NULL,
  period_end      date    NOT NULL,
  boundary        text    NOT NULL,
  customer_scope  text[]  NOT NULL,                -- 'all' 存为 '{all}'
  calculation_ref text,
  evidence_refs   text[]  NOT NULL DEFAULT '{}',
  answer_key      text    NOT NULL,
  risk_level      text,
  CONSTRAINT r_num CHECK (value_type <> 'number'
        OR calculation_ref IS NOT NULL OR cardinality(evidence_refs) > 0),
  CONSTRAINT num_has_value CHECK (value_type <> 'number' OR value_num IS NOT NULL),
  CONSTRAINT period_ok CHECK (period_start <= period_end),
  CONSTRAINT answer_trusted CHECK (trust_zone = 'trusted'),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.answer_v');

CREATE TABLE IF NOT EXISTS graph.approval_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  subject_id text NOT NULL,
  subject_version integer NOT NULL,
  risk_level text NOT NULL,
  batch_id text,
  decided_by text,
  CONSTRAINT l3_no_batch CHECK (risk_level <> 'L3' OR batch_id IS NULL),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.approval_v');

CREATE TABLE IF NOT EXISTS graph.conflict_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  rule_id text NOT NULL,
  severity text NOT NULL CHECK (severity IN ('blocking','warning')),
  conflict_key text NOT NULL,
  request_id text,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.conflict_v');

CREATE TABLE IF NOT EXISTS graph.supplier_request_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  counterparty_id text NOT NULL,
  due_date date NOT NULL,
  reminder_count integer NOT NULL CHECK (reminder_count >= 0),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.supplier_request_v');

CREATE TABLE IF NOT EXISTS graph.closed_question_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  target_id text NOT NULL,
  assignee_role text NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.closed_question_v');

CREATE TABLE IF NOT EXISTS graph.organization_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  country text,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.organization_v');

CREATE TABLE IF NOT EXISTS graph.counterparty_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  role text NOT NULL,
  country text,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.counterparty_v');

CREATE TABLE IF NOT EXISTS graph.site_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  country text,
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.site_v');

CREATE TABLE IF NOT EXISTS graph.product_v (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  cn_code text CHECK (cn_code IS NULL OR cn_code ~ '^[0-9]{8}$'),
  PRIMARY KEY (org_id, id, version)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.product_v');

-- 幂等：约束已存在时跳过（迁移可重复执行）
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'question_ordinal_uq'
                   AND conrelid = 'graph.question_v'::regclass) THEN
    ALTER TABLE graph.question_v ADD CONSTRAINT question_ordinal_uq UNIQUE (org_id, request_id, ordinal, version);
  END IF;
END $$;


-- head 指针：唯一允许 UPDATE 的表，仅写入函数推进（乐观锁 expected_version）
CREATE TABLE IF NOT EXISTS graph.object_head (
  org_id text NOT NULL, id text NOT NULL, type text NOT NULL, head_version integer NOT NULL,
  status text, trust_zone text NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.object_head');

-- 边：全部由字段派生（SSOT-3），数值相关边必须钉住版本
CREATE TABLE IF NOT EXISTS graph.edge (
  org_id text NOT NULL, edge_type text NOT NULL CHECK (edge_type IN ('answers','cites','derived_from','maps_to','part_of',
    'supersedes','approves','conflicts_with','resolves','requested_from','fulfills','computed_with','scoped_to')),
  from_id text NOT NULL, from_version integer NOT NULL, to_id text NOT NULL, to_version integer,
  attrs jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT numeric_edges_pinned CHECK (edge_type NOT IN ('cites','derived_from','computed_with') OR to_version IS NOT NULL),
  PRIMARY KEY (org_id, edge_type, from_id, from_version, to_id)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('graph.edge');

CREATE TABLE IF NOT EXISTS graph.outbox (
  seq bigserial, org_id text NOT NULL, object_ref text NOT NULL, event_type text NOT NULL,
  payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), published_at timestamptz,
  PRIMARY KEY (seq)
);

CREATE TABLE IF NOT EXISTS graph.idempotency (
  org_id text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL, response jsonb NOT NULL,
  expires_at timestamptz NOT NULL, PRIMARY KEY (org_id, idempotency_key)
);

-- R-ONE（SSOT-5）
CREATE TABLE IF NOT EXISTS graph.effective_answer (
  org_id text NOT NULL, answer_key text NOT NULL, answer_id text NOT NULL, version integer NOT NULL,
  PRIMARY KEY (org_id, answer_key)
);

CREATE TABLE IF NOT EXISTS graph.principal (
  org_id text NOT NULL, actor text NOT NULL, roles text[] NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, actor)
);

-- 字典镜像（只读）
CREATE TABLE IF NOT EXISTS dict.datapoint (
  id text NOT NULL, semver text NOT NULL, value_type text NOT NULL, unit text, dimension text, status text NOT NULL,
  body jsonb NOT NULL, PRIMARY KEY (id, semver)
);
CREATE TABLE IF NOT EXISTS dict.template_mapping (
  id text PRIMARY KEY, body jsonb NOT NULL
);

-- 因子库：全局 org_global 或租户自带许可
CREATE TABLE IF NOT EXISTS ref.factor_set (
  org_id         text        NOT NULL,
  id             text        NOT NULL,
  version        integer     NOT NULL CHECK (version >= 1),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by     text        NOT NULL,
  trust_zone     text        NOT NULL CHECK (trust_zone IN ('quarantine','trusted')),
  provenance     text        NOT NULL,
  supersedes     text,
  schema_version text        NOT NULL,
  content_hash   text        NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  status         text,
  body           jsonb       NOT NULL,          -- 完整对象（权威）
  source text NOT NULL, license text NOT NULL, published_at date NOT NULL, valid_from date NOT NULL, valid_until date,
  row_count integer NOT NULL, rows_hash text NOT NULL,
  PRIMARY KEY (org_id, id, version)
);
CREATE TABLE IF NOT EXISTS ref.factor (
  factor_set_id text NOT NULL, factor_key text NOT NULL, geography text NOT NULL, year text NOT NULL,
  value numeric NOT NULL, unit text NOT NULL, source_row_id text NOT NULL,
  PRIMARY KEY (factor_set_id, source_row_id)
);

-- 对象存储登记（内容寻址，03 §6.7）
CREATE TABLE IF NOT EXISTS doc.blob (
  org_id text NOT NULL, sha256 text NOT NULL, size bigint NOT NULL, mime text, s3_key text NOT NULL, kms_key_id text,
  uploaded_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), retention_until date,
  PRIMARY KEY (org_id, sha256)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('doc.blob');

-- 审计（03 §3.14、07 §7）：只插入
CREATE TABLE IF NOT EXISTS audit.audit_event (
  org_id text NOT NULL, seq bigint NOT NULL, id text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by text NOT NULL, action text NOT NULL CHECK (action IN ('object.create','object.version','object.promote',
    'edge.derive','export.create','egress.send','egress.block','auth.login','authz.change','authz.deny','key.rotate',
    'policy.halt','crypto.erase','dsr.fulfil')),
  object_ref text, content_hash text, trace_id text NOT NULL, prev_hash text NOT NULL, event_hash text NOT NULL,
  ip text, user_agent text, details jsonb,
  PRIMARY KEY (org_id, seq)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('audit.audit_event');
CREATE TABLE IF NOT EXISTS audit.anchor (
  org_id text NOT NULL, anchor_date date NOT NULL, last_seq bigint NOT NULL, last_event_hash text NOT NULL,
  merkle_root text, tsa_token bytea, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (org_id, anchor_date)
);

-- 追踪：按月 RANGE(started_at) 再 HASH(org_id)（03 §6.4）
CREATE TABLE IF NOT EXISTS obs.trace_span (
  org_id text NOT NULL, id text NOT NULL, span_id text NOT NULL, parent_span_id text, actor text NOT NULL,
  request_id text, model text, model_provider text, prompt_version text, input_hash text NOT NULL, output_hash text NOT NULL,
  tokens_in integer, tokens_out integer, cost_eur numeric, started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
  status text NOT NULL CHECK (status IN ('ok','error','halted')), payload_blob text,
  PRIMARY KEY (org_id, id, span_id, started_at)
) PARTITION BY RANGE (started_at);
-- 月分区示例（运维任务按月创建；每个月分区再 HASH(org_id) 子分区）
CREATE TABLE IF NOT EXISTS obs.trace_span_2026_11 PARTITION OF obs.trace_span
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01') PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('obs.trace_span_2026_11', 8);

-- Spec: 03 §6.3（索引）
CREATE INDEX IF NOT EXISTS answer_v_key_idx       ON graph.answer_v (org_id, answer_key, version DESC);           -- R-ONE、按口径取最新
CREATE INDEX IF NOT EXISTS answer_v_reuse_idx     ON graph.answer_v (org_id, datapoint_base, period_end DESC)
  WHERE status IN ('approved','submitted');                                                                       -- L0 复用、一致性审计
CREATE INDEX IF NOT EXISTS answer_v_scope_gin     ON graph.answer_v USING gin (customer_scope);                   -- 按客户授权（NFR-01）
CREATE INDEX IF NOT EXISTS evidence_v_expiry_idx  ON graph.evidence_v (org_id, valid_until) WHERE status = 'verified'; -- FR-404 到期扫描
CREATE INDEX IF NOT EXISTS evidence_v_fact_idx    ON graph.evidence_v (org_id, fact_key, period_end);             -- 证据挖掘去重
CREATE INDEX IF NOT EXISTS evidence_v_blob_idx    ON graph.evidence_v (org_id, blob_sha256);                      -- 文档 → 证据反查
CREATE INDEX IF NOT EXISTS calculation_v_hash_idx ON graph.calculation_v (org_id, input_hash);                    -- 计算缓存命中
CREATE INDEX IF NOT EXISTS edge_to_idx            ON graph.edge (org_id, to_id, edge_type);                        -- 向下影响分析
CREATE INDEX IF NOT EXISTS edge_from_idx          ON graph.edge (org_id, from_id, from_version);                   -- 向上谱系
CREATE INDEX IF NOT EXISTS object_head_list_idx   ON graph.object_head (org_id, type, status);                     -- 工作台列表
CREATE INDEX IF NOT EXISTS conflict_v_open_idx    ON graph.conflict_v (org_id, conflict_key) WHERE status = 'open'; -- 去重
CREATE INDEX IF NOT EXISTS audit_event_object_idx ON audit.audit_event (org_id, object_ref);
CREATE INDEX IF NOT EXISTS outbox_pending_idx     ON graph.outbox (seq) WHERE published_at IS NULL;

-- as-of 查询：*_v (org_id, created_at) BRIN
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['request_v','question_v','mapping_v','evidence_v','calculation_v','answer_v','approval_v',
                           'conflict_v','supplier_request_v','closed_question_v','organization_v','counterparty_v','site_v','product_v']
  LOOP
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON graph.%I USING brin (org_id, created_at)', t || '_asof_brin', t);
  END LOOP;
END $$;

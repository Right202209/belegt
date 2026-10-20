-- Spec: 03 §6.5（行级安全）、§6.1（角色权限）、§1.5 SSOT-2；07 §2.1、§4.3、§7.3
-- 每个连接在事务开头由 API 层设置（客户端不可控）：
--   SET LOCAL app.org_id = '...'; SET LOCAL app.actor = '...'; SET LOCAL app.zone = 'trusted|quarantine';
--   SET LOCAL app.cp_scope = 'cp_a,cp_b' | '*'; SET LOCAL app.approved_only = 'on'|'off';

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['graph.request_v','graph.question_v','graph.mapping_v','graph.evidence_v','graph.calculation_v',
                           'graph.answer_v','graph.approval_v','graph.conflict_v','graph.supplier_request_v',
                           'graph.closed_question_v','graph.organization_v','graph.counterparty_v','graph.site_v',
                           'graph.product_v','graph.object_head']
  LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
    -- 1) 租户隔离
    EXECUTE format('DROP POLICY IF EXISTS p_tenant ON %s', t);
    EXECUTE format($p$CREATE POLICY p_tenant ON %s USING (org_id = current_setting('app.org_id', true))$p$, t);
    -- 2) 信任区：隔离区角色只能看 quarantine 行
    EXECUTE format('DROP POLICY IF EXISTS p_zone ON %s', t);
    EXECUTE format($p$CREATE POLICY p_zone ON %s AS RESTRICTIVE
      USING (current_setting('app.zone', true) = 'trusted' OR trust_zone = 'quarantine')$p$, t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['graph.edge','graph.effective_answer','graph.idempotency','graph.outbox','audit.audit_event','doc.blob']
  LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_tenant ON %s', t);
    EXECUTE format($p$CREATE POLICY p_tenant ON %s USING (org_id = current_setting('app.org_id', true))$p$, t);
  END LOOP;
END $$;

-- 3) 按客户授权（sales、customer_buyer、sales-assistant）
-- 3)/4) 是读过滤，只作用于 SELECT。实测：写成 FOR ALL 时它们也充当 INSERT 的 WITH CHECK，写入会话未设置
-- app.cp_scope（NULL）就无法插入 customer_scope ≠ {all} 的 Answer（"new row violates row-level security policy
-- p_customer_scope"）。写入范围由写入 API 的执行者授权（03 §1.6、07 §4.1）与 p_tenant/p_zone（FOR ALL）约束。
DROP POLICY IF EXISTS p_customer_scope ON graph.answer_v;
CREATE POLICY p_customer_scope ON graph.answer_v AS RESTRICTIVE FOR SELECT
  USING (
    current_setting('app.cp_scope', true) = '*'
    OR customer_scope && string_to_array(current_setting('app.cp_scope', true), ',')
    OR customer_scope = '{all}'
  );
-- 4) 只读已批准（sales-assistant、customer_buyer）
DROP POLICY IF EXISTS p_approved_only ON graph.answer_v;
CREATE POLICY p_approved_only ON graph.answer_v AS RESTRICTIVE FOR SELECT
  USING (current_setting('app.approved_only', true) IS DISTINCT FROM 'on'
         OR status IN ('approved','submitted'));

-- 因子库：全局行对所有角色可读，租户行按租户
ALTER TABLE ref.factor_set ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_tenant_or_global ON ref.factor_set;
CREATE POLICY p_tenant_or_global ON ref.factor_set USING (org_id IN ('org_global', current_setting('app.org_id', true)));

-- ---------------------------------------------------------------- 权限（SSOT-2：表无 INSERT/UPDATE/DELETE）
REVOKE ALL ON ALL TABLES IN SCHEMA graph, audit, ref, doc, vec, obs, dict FROM PUBLIC;
GRANT USAGE ON SCHEMA graph, audit, ref, doc, vec, obs, dict TO belegt_writer, belegt_reader_trusted, belegt_reader_quarantine, belegt_auditor;
GRANT SELECT ON ALL TABLES IN SCHEMA graph, ref, doc, vec, dict TO belegt_reader_trusted;
GRANT SELECT ON ALL TABLES IN SCHEMA dict TO belegt_reader_quarantine;
GRANT SELECT ON graph.request_v, graph.question_v, graph.mapping_v, graph.evidence_v, graph.object_head, graph.supplier_request_v
  TO belegt_reader_quarantine;   -- RLS p_zone 只放行 quarantine 行
-- 审计员只见 object_ref、哈希与动作，不见 ip（03 §6.5）
CREATE OR REPLACE VIEW audit.audit_event_public AS
  SELECT org_id, seq, id, created_at, created_by, action, object_ref, content_hash, trace_id, prev_hash, event_hash
  FROM audit.audit_event;
GRANT SELECT ON audit.audit_event_public TO belegt_auditor, belegt_reader_trusted;
-- 写入只经 SECURITY DEFINER 函数（0005）
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA graph, audit FROM belegt_writer, belegt_reader_trusted,
  belegt_reader_quarantine, belegt_auditor;

-- Spec: 03 §6.8 第 8 步（graph.commit_version(): 插版本行 + 推进 head(expected_version) + 派生边 + effective_answer
--       + audit_event + outbox）、§1.4（append-only、created_at 由 clock_timestamp() 赋值）、§3.14（哈希链）、
--       SSOT-2（belegt_writer 只能 EXECUTE 本函数）、R-IMM、R-ZONE（数据库层）、R-ONE。
-- 语义校验（R-SCHEMA/R-REF/R-STATE/R-VAL…）在 API 层（belegt_core.rules）完成；本函数是数据库层的最后防线：
-- 租户/执行者一致、信任区、乐观锁、R-NUM（表 CHECK）、R-ONE、content_hash 复核、审计哈希链。
-- 错误：RAISE EXCEPTION '<GRAPH_*>' USING ERRCODE = 'BG000', DETAIL = <json>；API 映射为 03 §6.8 错误响应。

-- RFC 8785 JCS（本函数所需子集）：删除对象中的 null 字段、键按 UTF-8 字节序（= BMP 内 UTF-16 序）、数字去掉无意义的 0。
-- 限制：不支持需要指数表示的极大/极小浮点；键含 U+E000–U+FFFF 与增补平面字符混排时排序可能与 JCS 不同。
CREATE OR REPLACE FUNCTION graph.jcs(j jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
  t text := jsonb_typeof(j);
  r text;
  k text;
  v jsonb;
  first boolean := true;
  n numeric;
BEGIN
  IF t = 'object' THEN
    r := '{';
    FOR k, v IN SELECT e.key, e.value FROM jsonb_each(j) AS e WHERE jsonb_typeof(e.value) <> 'null'
                ORDER BY convert_to(e.key, 'UTF8') LOOP
      IF NOT first THEN r := r || ','; END IF;
      first := false;
      r := r || to_json(k)::text || ':' || graph.jcs(v);
    END LOOP;
    RETURN r || '}';
  ELSIF t = 'array' THEN
    r := '[';
    FOR v IN SELECT e.value FROM jsonb_array_elements(j) AS e LOOP
      IF NOT first THEN r := r || ','; END IF;
      first := false;
      r := r || coalesce(graph.jcs(v), 'null');
    END LOOP;
    RETURN r || ']';
  ELSIF t = 'string' THEN
    RETURN to_json(j #>> '{}')::text;
  ELSIF t = 'number' THEN
    n := (j #>> '{}')::numeric;
    IF n = trunc(n) THEN
      RETURN trunc(n)::text;
    END IF;
    RETURN rtrim(n::text, '0');
  ELSIF t = 'boolean' THEN
    RETURN j #>> '{}';
  END IF;
  RETURN 'null';
END $$;

CREATE OR REPLACE FUNCTION graph.sha256_tag(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT 'sha256:' || encode(sha256(convert_to(t, 'UTF8')), 'hex') $$;

CREATE OR REPLACE FUNCTION graph.ts_utc(ts timestamptz) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') $$;

CREATE OR REPLACE FUNCTION graph.fail(p_code text, p_message text, p_details jsonb DEFAULT '{}'::jsonb) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '%', p_code USING ERRCODE = 'BG000', DETAIL = jsonb_build_object('message', p_message, 'details', p_details)::text;
END $$;

-- 审计事件追加（哈希链）：event_hash = sha256(prev_hash ‖ JCS(event 去掉 event_hash))
CREATE OR REPLACE FUNCTION audit.append_event(p_org text, p_action text, p_actor text, p_trace text, p_object_ref text,
                                              p_content_hash text, p_details jsonb, p_ip text DEFAULT NULL,
                                              p_user_agent text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, graph, pg_temp AS $$
DECLARE
  v_seq bigint;
  v_prev text;
  v_now timestamptz := clock_timestamp();
  v_ev jsonb;
  v_hash text;
  -- 用核心函数 gen_random_uuid()（PG13+，pg_catalog），不依赖 public 中的 pgcrypto：SECURITY DEFINER 的 search_path 不含 public
  v_id text := 'aud_' || upper(substr(replace(pg_catalog.gen_random_uuid()::text, '-', ''), 1, 26));
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('belegt.audit:' || p_org));
  SELECT seq, event_hash INTO v_seq, v_prev FROM audit.audit_event WHERE org_id = p_org ORDER BY seq DESC LIMIT 1;
  v_seq := coalesce(v_seq, 0) + 1;
  v_prev := coalesce(v_prev, 'sha256:' || repeat('0', 64));
  v_ev := jsonb_strip_nulls(jsonb_build_object(
    'id', v_id, 'org_id', p_org, 'version', 1, 'created_at', graph.ts_utc(v_now), 'created_by', p_actor,
    'seq', v_seq, 'action', p_action, 'object_ref', p_object_ref, 'content_hash', p_content_hash, 'trace_id', p_trace,
    'prev_hash', v_prev, 'ip', p_ip, 'user_agent', p_user_agent, 'details', p_details));
  v_hash := 'sha256:' || encode(sha256(convert_to(v_prev || graph.jcs(v_ev), 'UTF8')), 'hex');
  INSERT INTO audit.audit_event (org_id, seq, id, created_at, created_by, action, object_ref, content_hash, trace_id,
                                 prev_hash, event_hash, ip, user_agent, details)
  VALUES (p_org, v_seq, v_id, v_now, p_actor, p_action, p_object_ref, p_content_hash, p_trace, v_prev, v_hash, p_ip,
          p_user_agent, p_details);
  RETURN v_ev || jsonb_build_object('event_hash', v_hash);
END $$;

-- 审计链校验（10 SLO-07 每小时；07 §7.2）：在数据库内按 append_event 同算法重算，返回断链的 seq（空 = 连续）。
-- SECURITY DEFINER：belegt_auditor 无权读 audit.audit_event 原表（含 ip），但可调用本函数；FORCE RLS 使其只校验
-- 当前会话 app.org_id 的链。
CREATE OR REPLACE FUNCTION audit.verify_chain() RETURNS SETOF bigint
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = audit, graph, pg_temp AS $$
DECLARE
  r record;
  v_prev text := 'sha256:' || repeat('0', 64);
  v_calc text;
BEGIN
  FOR r IN SELECT * FROM audit.audit_event WHERE org_id = current_setting('app.org_id', true) ORDER BY seq LOOP
    v_calc := 'sha256:' || encode(sha256(convert_to(r.prev_hash || graph.jcs(jsonb_strip_nulls(jsonb_build_object(
      'id', r.id, 'org_id', r.org_id, 'version', 1, 'created_at', graph.ts_utc(r.created_at), 'created_by', r.created_by,
      'seq', r.seq, 'action', r.action, 'object_ref', r.object_ref, 'content_hash', r.content_hash, 'trace_id', r.trace_id,
      'prev_hash', r.prev_hash, 'ip', r.ip, 'user_agent', r.user_agent, 'details', r.details))), 'UTF8')), 'hex');
    IF r.prev_hash <> v_prev OR v_calc <> r.event_hash THEN
      RETURN NEXT r.seq;
    END IF;
    v_prev := r.event_hash;
  END LOOP;
END $$;

-- ---------------------------------------------------------------- commit_version
-- p_edges：API 由字段派生的边（belegt_core.store.edges.derive_edges），元素 {edge_type, from_id, from_version, to_id, to_version, attrs}
CREATE OR REPLACE FUNCTION graph.commit_version(
  p_type text, p_body jsonb, p_expected_version integer, p_edges jsonb DEFAULT '[]'::jsonb,
  p_idempotency_key text DEFAULT NULL, p_request_hash text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = graph, pg_temp AS $$
DECLARE
  v_org     text := current_setting('app.org_id', true);
  v_actor   text := current_setting('app.actor', true);
  v_zone    text := current_setting('app.zone', true);
  v_id      text := p_body->>'id';
  v_tz      text := p_body->>'trust_zone';
  v_status  text := p_body->>'status';
  v_head    record;
  v_prev    record;
  v_version integer;
  v_now     timestamptz := clock_timestamp();
  v_body    jsonb;
  v_hash    text;
  v_ref     text;
  v_idem    record;
  v_eff     record;
  v_other   text;
  v_action  text;
  v_res     jsonb;
  v_target_org text;
BEGIN
  IF v_org IS NULL OR v_actor IS NULL THEN
    PERFORM graph.fail('GRAPH_ACTOR_NOT_ALLOWED', 'session variables app.org_id/app.actor not set');
  END IF;
  v_target_org := p_body->>'org_id';
  IF v_target_org IS DISTINCT FROM v_org AND NOT (p_type = 'FactorSet' AND v_target_org = 'org_global'
                                                  AND v_actor = 'service:factor-import') THEN
    PERFORM graph.fail('GRAPH_REF_CROSS_TENANT', 'org_id does not match session tenant');
  END IF;
  IF p_body->>'created_by' IS DISTINCT FROM v_actor THEN
    PERFORM graph.fail('GRAPH_ACTOR_NOT_ALLOWED', 'created_by must equal app.actor');
  END IF;
  IF v_zone = 'quarantine' AND v_tz <> 'quarantine' THEN
    PERFORM graph.fail('GRAPH_TRUST_ZONE_VIOLATION', 'quarantine session may only write quarantine objects');
  END IF;

  -- 幂等（24 h）
  IF p_idempotency_key IS NOT NULL THEN
    SELECT * INTO v_idem FROM graph.idempotency WHERE org_id = v_org AND idempotency_key = p_idempotency_key AND expires_at > now();
    IF FOUND THEN
      IF v_idem.request_hash <> p_request_hash THEN
        PERFORM graph.fail('GRAPH_IDEMPOTENCY_MISMATCH', 'same idempotency_key with a different request body');
      END IF;
      RETURN v_idem.response || '{"idempotent_replay": true}'::jsonb;
    END IF;
  END IF;

  -- head 乐观锁（R-IMM）
  SELECT * INTO v_head FROM graph.object_head WHERE org_id = v_target_org AND id = v_id FOR UPDATE;
  IF FOUND THEN
    IF p_expected_version IS DISTINCT FROM v_head.head_version THEN
      PERFORM graph.fail('GRAPH_VERSION_CONFLICT', 'expected_version != head',
                         jsonb_build_object('head_version', v_head.head_version));
    END IF;
    IF v_head.type <> p_type THEN
      PERFORM graph.fail('GRAPH_SCHEMA_INVALID', 'type mismatch with existing object');
    END IF;
    IF v_head.trust_zone = 'trusted' AND v_tz = 'quarantine' THEN
      PERFORM graph.fail('GRAPH_PROMOTION_DENIED', 'trust_zone may only move quarantine -> trusted');
    END IF;
    IF v_head.trust_zone = 'quarantine' AND v_tz = 'trusted'
       AND NOT (v_actor = 'agent:guard' OR v_actor LIKE 'user:%' OR v_actor = 'service:graph-api') THEN
      PERFORM graph.fail('GRAPH_PROMOTION_DENIED', 'only agent:guard or a human may promote');
    END IF;
    v_version := v_head.head_version + 1;
  ELSE
    IF coalesce(p_expected_version, 0) <> 0 THEN
      PERFORM graph.fail('GRAPH_VERSION_CONFLICT', 'object does not exist');
    END IF;
    v_version := 1;
  END IF;
  IF (p_body->>'version')::int IS DISTINCT FROM v_version THEN
    PERFORM graph.fail('GRAPH_VERSION_CONFLICT', 'body.version must be head + 1');
  END IF;

  -- content_hash 复核（03 §1.4-6）；created_at 由数据库赋值，不参与哈希
  v_hash := graph.sha256_tag(graph.jcs(p_body - 'content_hash' - 'created_at'));
  IF v_hash <> p_body->>'content_hash' THEN
    PERFORM graph.fail('GRAPH_HASH_MISMATCH', 'content_hash does not match JCS(body)', jsonb_build_object('expected', v_hash));
  END IF;
  v_body := p_body || jsonb_build_object('created_at', graph.ts_utc(v_now));
  v_ref := v_id || '#v' || v_version;

  -- 版本行（公共列 + 冗余列）
  CASE p_type
  WHEN 'Request' THEN
    INSERT INTO graph.request_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'counterparty_id', (v_body->>'due_date')::date, v_body->>'priority');
  WHEN 'Question' THEN
    INSERT INTO graph.question_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'request_id', (v_body->>'ordinal')::int, v_body->>'native_id');
  WHEN 'Mapping' THEN
    INSERT INTO graph.mapping_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'question_id', v_body->>'datapoint_id', (v_body->>'confidence')::numeric, v_body->>'method');
  WHEN 'Evidence' THEN
    INSERT INTO graph.evidence_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'source_type', v_body->>'fact_key',
      CASE WHEN v_body->>'value_type' = 'number' THEN (v_body->>'extracted_value')::numeric END,
      v_body->>'unit', (v_body#>>'{period,end}')::date, (v_body->>'valid_from')::date, (v_body->>'valid_until')::date,
      v_body#>>'{locator,blob_sha256}');
  WHEN 'Calculation' THEN
    INSERT INTO graph.calculation_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'method_id', v_body->>'factor_set_version', v_body->>'methodology_version', v_body->>'engine_version',
      v_body->>'input_hash', v_body->>'output_hash', (v_body#>>'{result,value}')::numeric);
  WHEN 'Answer' THEN
    INSERT INTO graph.answer_v (org_id, id, version, created_at, created_by, trust_zone, provenance, supersedes,
      schema_version, content_hash, status, body, datapoint_id, value_type, value_num, unit, period_start, period_end,
      boundary, customer_scope, calculation_ref, evidence_refs, answer_key, risk_level)
    VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance', v_body->>'supersedes',
      v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'datapoint_id', v_body->>'value_type',
      CASE WHEN v_body->>'value_type' = 'number' THEN (v_body->>'value')::numeric END, v_body->>'unit',
      (v_body#>>'{period,start}')::date, (v_body#>>'{period,end}')::date, v_body->>'boundary',
      CASE WHEN jsonb_typeof(v_body->'customer_scope') = 'array'
           THEN ARRAY(SELECT jsonb_array_elements_text(v_body->'customer_scope')) ELSE ARRAY['all'] END,
      v_body->>'calculation_ref',
      coalesce(ARRAY(SELECT jsonb_array_elements_text(coalesce(v_body->'evidence_refs', '[]'::jsonb))), '{}'),
      v_body->>'answer_key', v_body->>'risk_level');
  WHEN 'Approval' THEN
    INSERT INTO graph.approval_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      split_part(v_body->>'subject_ref', '#v', 1), split_part(v_body->>'subject_ref', '#v', 2)::int,
      v_body->>'risk_level', v_body->>'batch_id', v_body->>'decided_by');
  WHEN 'Conflict' THEN
    INSERT INTO graph.conflict_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'rule_id', v_body->>'severity', v_body->>'conflict_key', v_body->>'request_id');
  WHEN 'SupplierRequest' THEN
    INSERT INTO graph.supplier_request_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      v_body->>'counterparty_id', (v_body->>'due_date')::date, (v_body->>'reminder_count')::int);
  WHEN 'ClosedQuestion' THEN
    INSERT INTO graph.closed_question_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body,
      split_part(v_body->>'target_ref', '#v', 1), v_body->>'assignee_role', (v_body->>'expires_at')::timestamptz);
  WHEN 'Organization' THEN
    INSERT INTO graph.organization_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'country');
  WHEN 'Counterparty' THEN
    INSERT INTO graph.counterparty_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'role', v_body->>'country');
  WHEN 'Site' THEN
    INSERT INTO graph.site_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'country');
  WHEN 'Product' THEN
    INSERT INTO graph.product_v VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'cn_code');
  WHEN 'FactorSet' THEN
    INSERT INTO ref.factor_set VALUES (v_target_org, v_id, v_version, v_now, v_actor, v_tz, v_body->>'provenance',
      v_body->>'supersedes', v_body->>'schema_version', v_hash, v_status, v_body, v_body->>'source', v_body->>'license',
      (v_body->>'published_at')::date, (v_body->>'valid_from')::date, (v_body->>'valid_until')::date,
      (v_body->>'row_count')::int, v_body->>'rows_hash');
  ELSE
    PERFORM graph.fail('GRAPH_SCHEMA_INVALID', 'unknown type ' || p_type);
  END CASE;

  -- head 推进
  IF v_head.id IS NULL THEN
    INSERT INTO graph.object_head (org_id, id, type, head_version, status, trust_zone, updated_at)
    VALUES (v_target_org, v_id, p_type, v_version, v_status, v_tz, v_now);
  ELSE
    UPDATE graph.object_head SET head_version = v_version, status = v_status, trust_zone = v_tz, updated_at = v_now
    WHERE org_id = v_target_org AND id = v_id AND head_version = p_expected_version;
    IF NOT FOUND THEN
      PERFORM graph.fail('GRAPH_VERSION_CONFLICT', 'head moved concurrently');
    END IF;
  END IF;

  -- 派生边（SSOT-3）
  INSERT INTO graph.edge (org_id, edge_type, from_id, from_version, to_id, to_version, attrs, created_at)
  SELECT v_target_org, e.edge_type, e.from_id, e.from_version, e.to_id, e.to_version, e.attrs, v_now
  FROM jsonb_to_recordset(p_edges) AS e(edge_type text, from_id text, from_version int, to_id text, to_version int, attrs jsonb)
  ON CONFLICT DO NOTHING;

  -- R-ONE（SSOT-5）
  IF p_type = 'Answer' THEN
    SELECT * INTO v_eff FROM graph.effective_answer WHERE org_id = v_target_org AND answer_key = v_body->>'answer_key' FOR UPDATE;
    IF v_status IN ('approved', 'submitted') THEN
      IF FOUND AND v_eff.answer_id <> v_id THEN
        SELECT status INTO v_other FROM graph.object_head WHERE org_id = v_target_org AND id = v_eff.answer_id;
        IF v_other IN ('approved', 'submitted') AND NOT EXISTS (
             SELECT 1 FROM graph.answer_v a WHERE a.org_id = v_target_org AND a.id = v_id
               AND split_part(a.supersedes, '#v', 1) = v_eff.answer_id)
           AND split_part(coalesce(v_body->>'supersedes', ''), '#v', 1) <> v_eff.answer_id THEN
          PERFORM graph.fail('GRAPH_DUPLICATE_EFFECTIVE_ANSWER', 'answer_key already has an effective answer',
                             jsonb_build_object('effective', v_eff.answer_id || '#v' || v_eff.version));
        END IF;
      END IF;
      INSERT INTO graph.effective_answer (org_id, answer_key, answer_id, version)
      VALUES (v_target_org, v_body->>'answer_key', v_id, v_version)
      ON CONFLICT (org_id, answer_key) DO UPDATE SET answer_id = EXCLUDED.answer_id, version = EXCLUDED.version;
    ELSIF FOUND AND v_eff.answer_id = v_id THEN
      DELETE FROM graph.effective_answer WHERE org_id = v_target_org AND answer_key = v_body->>'answer_key';
    END IF;
  END IF;

  -- outbox + 审计
  INSERT INTO graph.outbox (org_id, object_ref, event_type, payload, created_at)
  VALUES (v_target_org, v_ref, 'graph.object.committed', jsonb_build_object('type', p_type, 'id', v_id, 'version', v_version,
          'status', v_status, 'trust_zone', v_tz, 'content_hash', v_hash), v_now);
  v_action := CASE WHEN v_head.id IS NOT NULL AND v_head.trust_zone = 'quarantine' AND v_tz = 'trusted' THEN 'object.promote'
                   WHEN v_version = 1 THEN 'object.create' ELSE 'object.version' END;
  PERFORM audit.append_event(v_target_org, v_action, v_actor, v_body->>'provenance', v_ref, v_hash,
                             jsonb_build_object('type', p_type, 'status', v_status, 'edges', jsonb_array_length(p_edges)));

  v_res := jsonb_build_object('object_ref', v_ref, 'content_hash', v_hash, 'created_at', graph.ts_utc(v_now));
  IF p_idempotency_key IS NOT NULL THEN
    INSERT INTO graph.idempotency (org_id, idempotency_key, request_hash, response, expires_at)
    VALUES (v_org, p_idempotency_key, p_request_hash, v_res, now() + interval '24 hours')
    ON CONFLICT (org_id, idempotency_key) DO UPDATE SET request_hash = EXCLUDED.request_hash,
      response = EXCLUDED.response, expires_at = EXCLUDED.expires_at;
  END IF;
  RETURN v_res;
END $$;

-- 原子批量（≤500）：p_items = [{type, body, expected_version, edges, idempotency_key, request_hash}]
CREATE OR REPLACE FUNCTION graph.commit_batch(p_items jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = graph, pg_temp AS $$
DECLARE
  it jsonb;
  out jsonb := '[]'::jsonb;
BEGIN
  IF jsonb_array_length(p_items) > 500 THEN
    PERFORM graph.fail('GRAPH_BATCH_TOO_LARGE', 'batch size > 500');
  END IF;
  FOR it IN SELECT value FROM jsonb_array_elements(p_items) LOOP
    out := out || jsonb_build_array(graph.commit_version(it->>'type', it->'body', (it->>'expected_version')::int,
                                                         coalesce(it->'edges', '[]'::jsonb), it->>'idempotency_key',
                                                         it->>'request_hash'));
  END LOOP;
  RETURN out;
END $$;

ALTER FUNCTION graph.commit_version(text, jsonb, integer, jsonb, text, text) OWNER TO belegt_owner;
ALTER FUNCTION graph.commit_batch(jsonb) OWNER TO belegt_owner;
ALTER FUNCTION audit.append_event(text, text, text, text, text, text, jsonb, text, text) OWNER TO belegt_owner;
ALTER FUNCTION audit.verify_chain() OWNER TO belegt_owner;
REVOKE ALL ON FUNCTION audit.verify_chain() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.verify_chain() TO belegt_auditor, belegt_reader_trusted, belegt_writer;
REVOKE ALL ON FUNCTION graph.commit_version(text, jsonb, integer, jsonb, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION graph.commit_batch(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.append_event(text, text, text, text, text, text, jsonb, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION graph.commit_version(text, jsonb, integer, jsonb, text, text) TO belegt_writer;
GRANT EXECUTE ON FUNCTION graph.commit_batch(jsonb) TO belegt_writer;
GRANT EXECUTE ON FUNCTION audit.append_event(text, text, text, text, text, text, jsonb, text, text) TO belegt_writer;
GRANT EXECUTE ON FUNCTION graph.jcs(jsonb), graph.sha256_tag(text), graph.ts_utc(timestamptz) TO belegt_writer,
  belegt_reader_trusted, belegt_auditor;

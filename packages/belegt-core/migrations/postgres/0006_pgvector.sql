-- Spec: 03 §6.6（pgvector 用法）；SSOT-7（可由图完全重建，不作为写入依据）
CREATE TABLE IF NOT EXISTS vec.embedding (
  org_id       text NOT NULL,
  object_ref   text NOT NULL,              -- 'q_…#v2' / 'ans_…#v3' / 'ev_…#v1' / 'dp:…@1.1.0'
  kind         text NOT NULL CHECK (kind IN ('question_text','datapoint_desc','answer_text','evidence_chunk')),
  space_id     text NOT NULL,              -- 'emb:<model>@<dim>'，换模型=新空间
  trust_zone   text NOT NULL,
  lang         text,
  embedding    vector(1024) NOT NULL,
  PRIMARY KEY (org_id, object_ref, kind, space_id)
) PARTITION BY HASH (org_id);
SELECT graph.create_hash_partitions('vec.embedding');
CREATE INDEX IF NOT EXISTS embedding_hnsw ON vec.embedding USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

ALTER TABLE vec.embedding ENABLE ROW LEVEL SECURITY;
ALTER TABLE vec.embedding FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_tenant ON vec.embedding;
CREATE POLICY p_tenant ON vec.embedding USING (org_id IN (current_setting('app.org_id', true), 'org_global'));
DROP POLICY IF EXISTS p_zone ON vec.embedding;
CREATE POLICY p_zone ON vec.embedding AS RESTRICTIVE
  USING (current_setting('app.zone', true) = 'trusted' OR trust_zone = 'quarantine' OR org_id = 'org_global');
GRANT SELECT ON vec.embedding TO belegt_reader_trusted, belegt_reader_quarantine;

-- ---------------------------------------------------------------- 收尾：所有权（本文件是迁移的最后一步）
-- Spec: 03 §6.1（belegt_owner 持有 DDL）、SSOT-2。graph.commit_version()/commit_batch()/audit.append_event() 是
-- SECURITY DEFINER 且 OWNER = belegt_owner，因此表与序列也必须归 belegt_owner，否则函数以 owner 身份执行时
-- 对表没有 INSERT/UPDATE 权限（实测：迁移由超级用户执行时表归 postgres，写入报 permission denied）。
-- FORCE ROW LEVEL SECURITY 对 owner 同样生效，所以 SECURITY DEFINER 函数内部的读写仍受 p_tenant/p_zone 约束。
-- 执行者需为超级用户或 belegt_owner 的成员（托管 PG：GRANT belegt_owner TO <迁移账号>）。
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT n.nspname, c.relname, c.relkind
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ('graph','dict','ref','doc','vec','audit','obs')
             AND c.relkind IN ('r','p','v','m','S','f')
             AND pg_get_userbyid(c.relowner) <> 'belegt_owner'
             -- 随表列（bigserial）拥有的序列跟随表变更所有者，单独 ALTER 会报错
             AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                                                  AND d.objid = c.oid AND d.deptype IN ('a','i')))
           ORDER BY (c.relkind = 'S')
  LOOP
    IF r.relkind = 'S' THEN
      EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO belegt_owner', r.nspname, r.relname);
    ELSIF r.relkind IN ('v') THEN
      EXECUTE format('ALTER VIEW %I.%I OWNER TO belegt_owner', r.nspname, r.relname);
    ELSIF r.relkind IN ('m') THEN
      EXECUTE format('ALTER MATERIALIZED VIEW %I.%I OWNER TO belegt_owner', r.nspname, r.relname);
    ELSE
      EXECUTE format('ALTER TABLE %I.%I OWNER TO belegt_owner', r.nspname, r.relname);
    END IF;
  END LOOP;
  FOR r IN SELECT nspname FROM pg_namespace
           WHERE nspname IN ('graph','dict','ref','doc','vec','audit','obs') AND pg_get_userbyid(nspowner) <> 'belegt_owner'
  LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO belegt_owner', r.nspname);
  END LOOP;
END $$;

-- Spec: 03 §6.1（Schema 与数据库角色）、§6.6（pgvector）、07 §2.1（数据库层信任分区）
-- PostgreSQL 16。由 belegt_owner（迁移流水线）执行。幂等。
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector（03 §6.6）
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_bytes（ULID 随机部分）

CREATE SCHEMA IF NOT EXISTS graph;  -- 证据图主体
CREATE SCHEMA IF NOT EXISTS dict;   -- 全局字典镜像（只读）
CREATE SCHEMA IF NOT EXISTS ref;    -- 因子库
CREATE SCHEMA IF NOT EXISTS doc;    -- 对象存储登记
CREATE SCHEMA IF NOT EXISTS vec;    -- 语义索引
CREATE SCHEMA IF NOT EXISTS audit;  -- 哈希链审计
CREATE SCHEMA IF NOT EXISTS obs;    -- 追踪

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['belegt_owner','belegt_writer','belegt_reader_trusted','belegt_reader_quarantine','belegt_auditor']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

-- 应用连接池按执行者信任区使用不同 LOGIN 角色，分别 GRANT 上述组角色（部署时创建，见 deploy/）。
COMMENT ON ROLE belegt_writer IS '仅 EXECUTE graph.commit_version()/graph.commit_batch()（SECURITY DEFINER）；对表无 INSERT/UPDATE/DELETE';
COMMENT ON ROLE belegt_reader_quarantine IS '隔离区 Agent：只读 quarantine 行与 dict.*（RLS p_zone）';

-- 工具函数：哈希分区（03 §6.4，默认 MODULUS 32；私有化单租户可用 1）
-- 未显式给 p_modulus 时读会话设置 belegt.hash_modulus（迁移 Job：SET belegt.hash_modulus = 1），缺省 32。
-- 注意：分区数在首次建表时确定，之后改设置不会重分区（已存在的分区表 CREATE TABLE IF NOT EXISTS 跳过）。
DROP FUNCTION IF EXISTS graph.create_hash_partitions(regclass, int);
CREATE OR REPLACE FUNCTION graph.create_hash_partitions(p_table regclass, p_modulus int DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE i int; nsp text; rel text;
BEGIN
  p_modulus := coalesce(p_modulus, nullif(current_setting('belegt.hash_modulus', true), '')::int, 32);
  IF p_modulus < 1 THEN
    RAISE EXCEPTION 'belegt.hash_modulus must be >= 1';
  END IF;
  -- 已有分区时沿用现有分区数（重复执行迁移不因设置不同而追加不一致的分区）
  IF EXISTS (SELECT 1 FROM pg_inherits WHERE inhparent = p_table) THEN
    RETURN;
  END IF;
  SELECT n.nspname, c.relname INTO nsp, rel FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_table;
  FOR i IN 0 .. p_modulus - 1 LOOP
    EXECUTE format('CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %s FOR VALUES WITH (MODULUS %s, REMAINDER %s)',
                   nsp, rel || '_p' || i, p_table, p_modulus, i);
  END LOOP;
END $$;

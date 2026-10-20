# Spec: 03 §6.1（belegt_owner 执行 DDL；按信任区的登录角色）、§6.4（MODULUS）；10 §6.5（升级步骤②）、§8.4（迁移规则）
"""迁移初始化 Job：``python -m belegt_migrate``（compose 的 ``belegt-migrate`` 服务、Helm 的 pre-install/pre-upgrade Hook）。

步骤（全部幂等，可重复执行）：
1. 以管理员连接（``BELEGT_MIGRATE_ADMIN_URL``：超级用户，或托管 PG 中为 belegt_owner 成员且有 CREATEROLE 的账号）；
   取 advisory lock，防止多个副本并发迁移。
2. 按文件名顺序执行 ``migrations/postgres/*.sql``（0001…0006，含 RLS 与 ``graph.commit_version()``）；
   记录到 ``public.belegt_schema_migrations``（文件名 + sha256）。文件未变化则跳过，``--force`` 全部重跑。
   ``BELEGT_PG_HASH_MODULUS``（默认 32；私有化单租户建议 1）在首次建表时决定分区数。
3. 建 ``GraphStore`` 可移植表（``belegt_core.store.tables``），授予组角色 ``belegt_app_portable`` DML 权限。
4. 按环境变量创建/更新 LOGIN 角色并授予组角色（密码只从环境/Secret 读取，不打印）：
   - ``BELEGT_DB_APP_USER``/``_PASSWORD``：API 与可信区 worker → belegt_writer + belegt_reader_trusted + belegt_app_portable
   - ``BELEGT_DB_QUARANTINE_USER``/``_PASSWORD``：隔离区 → belegt_reader_quarantine（07 §2.2）
   - ``BELEGT_DB_AUDITOR_USER``/``_PASSWORD``：审计只读 → belegt_auditor
5. 自检：commit_version 为 SECURITY DEFINER 且属 belegt_owner；graph 表启用 FORCE RLS；应用账号非超级用户、无 BYPASSRLS。

退出码：0 成功；1 失败（Helm Hook 失败即中止升级，10 §6.5）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

LOCK_KEY = 0x62656C65  # 'bele'
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _find_sql_dir() -> Path:
    env = os.environ.get("BELEGT_MIGRATIONS_DIR")
    if env:
        return Path(env)
    for c in (Path("/app/migrations/postgres"),
              Path(__file__).resolve().parents[3] / "packages" / "belegt-core" / "migrations" / "postgres"):
        if c.is_dir():
            return c
    raise SystemExit("migrations/postgres not found; set BELEGT_MIGRATIONS_DIR")


def _plain_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _ensure_login(cur, user: str | None, password: str | None, groups: list[str]) -> None:
    if not user:
        return
    if not _IDENT.match(user):
        raise SystemExit(f"invalid role name {user!r}")
    if not password:
        raise SystemExit(f"password for {user} missing")
    from psycopg import sql

    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
    verb = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
        sql.Identifier(user), sql.Literal(password)))
    for g in groups:
        cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(g), sql.Identifier(user)))
    print(f"[migrate] role {user}: {'created' if verb == 'CREATE' else 'updated'}, groups={','.join(groups)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="belegt-migrate")
    ap.add_argument("--force", action="store_true", help="重跑全部 SQL（全部迁移幂等）")
    ap.add_argument("--check", action="store_true", help="只做自检，不迁移")
    a = ap.parse_args(argv)
    import psycopg

    url = os.environ.get("BELEGT_MIGRATE_ADMIN_URL")
    if not url:
        print("BELEGT_MIGRATE_ADMIN_URL is required", file=sys.stderr)
        return 1
    sql_dir = _find_sql_dir()
    files = sorted(sql_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    modulus = int(os.environ.get("BELEGT_PG_HASH_MODULUS", "32"))
    with psycopg.connect(_plain_url(url), autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            if not a.check:
                cur.execute("CREATE TABLE IF NOT EXISTS public.belegt_schema_migrations (filename text PRIMARY KEY, "
                            "sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
                cur.execute("SELECT set_config('belegt.hash_modulus', %s, false)", (str(modulus),))
                for f in files:
                    text = f.read_text("utf-8")
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    cur.execute("SELECT sha256 FROM public.belegt_schema_migrations WHERE filename = %s", (f.name,))
                    row = cur.fetchone()
                    if row and row[0] == digest and not a.force:
                        print(f"[migrate] {f.name}: up to date")
                        continue
                    with conn.transaction():
                        conn.execute(text)
                        conn.execute("INSERT INTO public.belegt_schema_migrations (filename, sha256) VALUES (%s, %s) "
                                     "ON CONFLICT (filename) DO UPDATE SET sha256 = EXCLUDED.sha256, applied_at = now()",
                                     (f.name, digest))
                    print(f"[migrate] {f.name}: applied")
                # 可移植表（GraphStore 读路径）
                from sqlalchemy import create_engine

                from belegt_core.store import tables as T

                eng = create_engine("postgresql+psycopg://" + _plain_url(url).split("://", 1)[1])
                T.metadata.create_all(eng)
                eng.dispose()
                cur.execute("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'belegt_app_portable') "
                            "THEN CREATE ROLE belegt_app_portable NOLOGIN; END IF; END$$")
                names = [t.name for t in T.metadata.sorted_tables]
                from psycopg import sql

                for n in names:
                    cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON public.{} TO belegt_app_portable")
                                .format(sql.Identifier(n)))
                cur.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO belegt_app_portable")
                print(f"[migrate] portable tables: {len(names)}")
                _ensure_login(cur, os.environ.get("BELEGT_DB_APP_USER"), os.environ.get("BELEGT_DB_APP_PASSWORD"),
                              ["belegt_writer", "belegt_reader_trusted", "belegt_app_portable"])
                _ensure_login(cur, os.environ.get("BELEGT_DB_QUARANTINE_USER"),
                              os.environ.get("BELEGT_DB_QUARANTINE_PASSWORD"), ["belegt_reader_quarantine"])
                _ensure_login(cur, os.environ.get("BELEGT_DB_AUDITOR_USER"), os.environ.get("BELEGT_DB_AUDITOR_PASSWORD"),
                              ["belegt_auditor"])
            # 自检
            cur.execute("SELECT prosecdef, pg_get_userbyid(proowner) FROM pg_proc p JOIN pg_namespace n "
                        "ON n.oid = p.pronamespace WHERE n.nspname = 'graph' AND proname = 'commit_version'")
            r = cur.fetchone()
            assert r == (True, "belegt_owner"), f"commit_version check failed: {r}"
            cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'graph' AND c.relkind = 'p' AND NOT c.relforcerowsecurity "
                        "AND c.relname LIKE '%\\_v'")
            assert cur.fetchone()[0] == 0, "graph.*_v without FORCE RLS"
            app_user = os.environ.get("BELEGT_DB_APP_USER")
            if app_user:
                cur.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = %s", (app_user,))
                assert cur.fetchone() == (False,), "app role must not be superuser/bypassrls"
            print("[migrate] self-check ok")
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return 0


if __name__ == "__main__":
    sys.exit(main())

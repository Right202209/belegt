"""迁移：PostgreSQL SQL 语法（pglast，可选）、可移植表的 PostgreSQL DDL 编译、alembic 在 SQLite 上可执行。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from belegt_core.store import tables as T

MIG = Path(__file__).resolve().parents[1] / "migrations"


def test_postgres_sql_files_parse():
    pglast = pytest.importorskip("pglast")
    files = sorted((MIG / "postgres").glob("*.sql"))
    assert [f.name for f in files] == ["0001_schemas_roles.sql", "0002_tables.sql", "0003_indexes.sql",
                                       "0004_rls_grants.sql", "0005_commit_version.sql", "0006_pgvector.sql"]
    for f in files:
        sql = f.read_text("utf-8")
        pglast.parse_sql(sql)
        for st in pglast.split(sql):
            if re.search(r"CREATE (OR REPLACE )?FUNCTION", st, re.I) and re.search(r"LANGUAGE plpgsql", st, re.I):
                pglast.parse_plpgsql(st)
    text = (MIG / "postgres" / "0005_commit_version.sql").read_text("utf-8")
    assert "FUNCTION graph.commit_version" in text and "SECURITY DEFINER" in text
    rls = (MIG / "postgres" / "0004_rls_grants.sql").read_text("utf-8")
    for p in ("p_tenant", "p_zone", "p_customer_scope", "p_approved_only"):
        assert p in rls
    assert "CONSTRAINT r_num CHECK" in (MIG / "postgres" / "0002_tables.sql").read_text("utf-8")
    assert "vector(1024)" in (MIG / "postgres" / "0006_pgvector.sql").read_text("utf-8")


def test_portable_tables_compile_for_postgres():
    d = postgresql.dialect()
    for t in T.metadata.sorted_tables:
        ddl = str(CreateTable(t).compile(dialect=d))
        assert "CREATE TABLE" in ddl
        for ix in t.indexes:
            str(CreateIndex(ix).compile(dialect=d))
    assert "JSONB" in str(CreateTable(T.object_version).compile(dialect=d))


def test_alembic_upgrade_sqlite(tmp_path):
    pytest.importorskip("alembic")
    db = tmp_path / "a.db"
    env = dict(os.environ, BELEGT_DB_URL=f"sqlite+pysqlite:///{db}")
    r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(MIG / "alembic.ini"), "upgrade", "head"],
                       env=env, capture_output=True, text=True, cwd=MIG)
    assert r.returncode == 0, r.stderr
    import sqlite3

    names = {x[0] for x in sqlite3.connect(db).execute("select name from sqlite_master where type='table'")}
    assert {"graph_object_version", "graph_object_head", "graph_edge", "audit_event"} <= names

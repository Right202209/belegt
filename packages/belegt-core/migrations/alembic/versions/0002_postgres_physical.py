"""PostgreSQL 16 生产物理设计：schema/角色/分区表/索引/RLS/graph.commit_version()/pgvector。Spec: 03 §6.1–§6.6、§6.8

仅在 PostgreSQL 方言执行（SQLite 跳过）。SQL 原文见 migrations/postgres/000*.sql。

Revision ID: 0002_postgres_physical
Revises: 0001_portable
Create Date: 2026-10-19
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002_postgres_physical"
down_revision = "0001_portable"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "postgres"
FILES = ["0001_schemas_roles.sql", "0002_tables.sql", "0003_indexes.sql", "0004_rls_grants.sql",
         "0005_commit_version.sql", "0006_pgvector.sql"]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for f in FILES:
        # 原样执行（含 %I 等 format 占位符），绕过 DBAPI 参数替换
        bind.connection.driver_connection.execute((SQL_DIR / f).read_text("utf-8"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for s in ("obs", "audit", "vec", "doc", "ref", "dict", "graph"):
        bind.exec_driver_sql(f"DROP SCHEMA IF EXISTS {s} CASCADE")

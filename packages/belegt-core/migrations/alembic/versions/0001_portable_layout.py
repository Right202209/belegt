"""GraphStore 可移植表布局（SQLite / PostgreSQL 通用）。Spec: 03 §6.2（对应关系见 belegt_core/store/tables.py）

Revision ID: 0001_portable
Revises:
Create Date: 2026-10-19
"""
from __future__ import annotations

from alembic import op

from belegt_core.store.tables import metadata

revision = "0001_portable"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:  # append-only 存储：仅允许开发环境回滚
    metadata.drop_all(op.get_bind())

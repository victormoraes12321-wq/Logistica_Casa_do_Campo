"""add audit log user and source ip columns

Revision ID: 0002_audit_log_user_ip
Revises: 0001_baseline_schema
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_audit_log_user_ip"
down_revision = "0001_baseline_schema"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = [c.get("name") for c in inspector.get_columns(table_name)]
    return column_name in set(cols)


def upgrade() -> None:
    if not _column_exists("audit_logs", "user_id"):
        op.add_column("audit_logs", sa.Column("user_id", sa.Integer(), nullable=True))
    if not _column_exists("audit_logs", "source_ip"):
        op.add_column("audit_logs", sa.Column("source_ip", sa.Text(), nullable=True))
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx_names = {idx.get("name") for idx in inspector.get_indexes("audit_logs")}
    if "idx_audit_logs_user" not in idx_names:
        op.create_index("idx_audit_logs_user", "audit_logs", ["user_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx_names = {idx.get("name") for idx in inspector.get_indexes("audit_logs")}
    if "idx_audit_logs_user" in idx_names:
        op.drop_index("idx_audit_logs_user", table_name="audit_logs")
    if _column_exists("audit_logs", "source_ip"):
        op.drop_column("audit_logs", "source_ip")
    if _column_exists("audit_logs", "user_id"):
        op.drop_column("audit_logs", "user_id")

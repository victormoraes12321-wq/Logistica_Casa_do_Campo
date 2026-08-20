"""runtime hardening columns indexes

Revision ID: 0003_runtime_hardening_columns_indexes
Revises: 0002_audit_log_user_ip
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0003_runtime_hardening_columns_indexes"
down_revision = "0002_audit_log_user_ip"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    for table, col, typ in [
        ("orders", "version", sa.Integer()),
        ("routes", "updated_at", sa.Text()),
        ("routes", "version", sa.Integer()),
        ("route_cities", "version", sa.Integer()),
        ("clients", "updated_at", sa.Text()),
        ("clients", "version", sa.Integer()),
        ("drivers", "updated_at", sa.Text()),
        ("drivers", "version", sa.Integer()),
        ("vehicles", "capacity_kg", sa.REAL()),
        ("vehicles", "updated_at", sa.Text()),
        ("vehicles", "version", sa.Integer()),
    ]:
        if not _has_column(inspector, table, col):
            op.add_column(table, sa.Column(col, typ, nullable=True))

    # Defaults/backfill safe para manter compatibilidade.
    op.execute("UPDATE orders SET version=1 WHERE version IS NULL")
    op.execute("UPDATE routes SET updated_at=COALESCE(updated_at, created_at) WHERE updated_at IS NULL")
    op.execute("UPDATE routes SET version=1 WHERE version IS NULL")
    op.execute("UPDATE route_cities SET version=1 WHERE version IS NULL")
    op.execute("UPDATE clients SET updated_at=COALESCE(updated_at, created_at) WHERE updated_at IS NULL")
    op.execute("UPDATE clients SET version=1 WHERE version IS NULL")
    op.execute("UPDATE drivers SET updated_at=COALESCE(updated_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL")
    op.execute("UPDATE drivers SET version=1 WHERE version IS NULL")
    op.execute("UPDATE vehicles SET capacity_kg=CAST(COALESCE(NULLIF(capacity,''),'0') AS REAL) WHERE capacity_kg IS NULL")
    op.execute("UPDATE vehicles SET updated_at=COALESCE(updated_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL")
    op.execute("UPDATE vehicles SET version=1 WHERE version IS NULL")

    if not _has_index(inspector, "routes", "idx_routes_updated_at"):
        op.create_index("idx_routes_updated_at", "routes", ["updated_at"])
    if not _has_index(inspector, "orders", "idx_orders_version"):
        op.create_index("idx_orders_version", "orders", ["version"])
    if not _has_index(inspector, "vehicles", "idx_vehicles_capacity_kg"):
        op.create_index("idx_vehicles_capacity_kg", "vehicles", ["capacity_kg"])
    if not _has_index(inspector, "route_cities", "idx_route_cities_version"):
        op.create_index("idx_route_cities_version", "route_cities", ["version"])


def downgrade() -> None:
    # Downgrade conservador para evitar perda de dados em ambiente real.
    pass


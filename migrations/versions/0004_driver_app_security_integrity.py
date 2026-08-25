"""driver app authentication, authorization and delivery integrity

Revision ID: 0004_driver_app_security_integrity
Revises: 0003_runtime_hardening_columns_indexes
Create Date: 2026-08-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

from app_core.services.driver_security import DEFAULT_DRIVER_PASSWORD, hash_driver_password


revision = "0004_driver_app_security_integrity"
down_revision = "0003_runtime_hardening_columns_indexes"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {index["name"] for index in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    driver_columns = _columns(bind, "drivers")
    if "password_hash" not in driver_columns:
        op.add_column("drivers", sa.Column("password_hash", sa.Text(), nullable=True))
    if "must_change_password" not in driver_columns:
        op.add_column("drivers", sa.Column("must_change_password", sa.Integer(), nullable=True, server_default="1"))

    if "route_id" not in _columns(bind, "delivery_problems"):
        op.add_column("delivery_problems", sa.Column("route_id", sa.Integer(), nullable=True))

    if "delivery_receipts" not in tables:
        op.create_table(
            "delivery_receipts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("image_data", sa.LargeBinary(), nullable=False),
            sa.Column("mime_type", sa.Text(), nullable=True, server_default="image/jpeg"),
            sa.Column("digital_signature", sa.LargeBinary(), nullable=True),
            sa.Column("delivered_to", sa.Text(), nullable=True),
            sa.Column("delivered_document", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
    else:
        receipt_columns = _columns(bind, "delivery_receipts")
        for name, column_type in [
            ("route_id", sa.Integer()),
            ("mime_type", sa.Text()),
            ("digital_signature", sa.LargeBinary()),
            ("delivered_to", sa.Text()),
            ("delivered_document", sa.Text()),
            ("notes", sa.Text()),
        ]:
            if name not in receipt_columns:
                op.add_column("delivery_receipts", sa.Column(name, column_type, nullable=True))

    problem_fk_columns = {tuple(fk.get("constrained_columns") or []) for fk in inspect(bind).get_foreign_keys("delivery_problems")}
    if ("route_id",) not in problem_fk_columns:
        with op.batch_alter_table("delivery_problems") as batch:
            batch.create_foreign_key(
                "fk_delivery_problems_route_id_routes", "routes", ["route_id"], ["id"], ondelete="SET NULL"
            )

    receipt_fk_columns = {tuple(fk.get("constrained_columns") or []) for fk in inspect(bind).get_foreign_keys("delivery_receipts")}
    if ("route_id",) not in receipt_fk_columns:
        with op.batch_alter_table("delivery_receipts") as batch:
            batch.create_foreign_key(
                "fk_delivery_receipts_route_id_routes", "routes", ["route_id"], ["id"], ondelete="SET NULL"
            )

    if "driver_sessions" not in tables:
        op.create_table(
            "driver_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("last_seen_at", sa.Text(), nullable=True),
            sa.Column("revoked_at", sa.Text(), nullable=True),
            sa.Column("client_ip", sa.Text(), nullable=True),
        )

    if "driver_delivery_operations" not in tables:
        op.create_table(
            "driver_delivery_operations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
            sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id"), nullable=False),
            sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.id"), nullable=False),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
            sa.Column("operation_type", sa.Text(), nullable=False),
            sa.Column("request_hash", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("completed_at", sa.Text(), nullable=True),
        )

    if "idx_delivery_receipts_order" not in _indexes(bind, "delivery_receipts"):
        op.create_index("idx_delivery_receipts_order", "delivery_receipts", ["order_id"])
    if "idx_driver_sessions_driver" not in _indexes(bind, "driver_sessions"):
        op.create_index("idx_driver_sessions_driver", "driver_sessions", ["driver_id"])
    if "idx_driver_sessions_expires" not in _indexes(bind, "driver_sessions"):
        op.create_index("idx_driver_sessions_expires", "driver_sessions", ["expires_at"])
    if "idx_driver_operations_route_order" not in _indexes(bind, "driver_delivery_operations"):
        op.create_index("idx_driver_operations_route_order", "driver_delivery_operations", ["route_id", "order_id"])

    rows = bind.execute(text("SELECT id FROM drivers WHERE password_hash IS NULL OR TRIM(password_hash)='' ")).fetchall()
    for row in rows:
        bind.execute(
            text("UPDATE drivers SET password_hash=:password_hash,must_change_password=1 WHERE id=:driver_id"),
            {"password_hash": hash_driver_password(DEFAULT_DRIVER_PASSWORD), "driver_id": int(row[0])},
        )
    bind.execute(text("UPDATE drivers SET must_change_password=1 WHERE must_change_password IS NULL"))
    if "pin" in _columns(bind, "drivers"):
        bind.execute(text("UPDATE drivers SET pin=NULL WHERE pin IS NOT NULL"))


def downgrade() -> None:
    # Conservador: credenciais, sessões e comprovantes constituem trilha de auditoria.
    # A reversão de código pode ignorar as novas colunas sem apagar dados operacionais.
    pass

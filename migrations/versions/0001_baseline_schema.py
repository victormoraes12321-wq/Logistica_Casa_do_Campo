"""baseline schema for SQLite/PostgreSQL preparation

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op

from app_core.sqlalchemy_models import Base


# revision identifiers, used by Alembic.
revision = "0001_baseline_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)


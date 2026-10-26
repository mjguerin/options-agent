"""Risk reservations and engine sessions (RK-03, RK-04, NFR-15).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_accounts",
        sa.Column("account", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("account", name="pk_risk_accounts"),
    )
    op.create_table(
        "risk_reservations",
        sa.Column("reservation_id", sa.String(160), nullable=False),
        sa.Column("account", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("release_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("reservation_id", name="pk_risk_reservations"),
    )
    op.create_index("ix_risk_reservations_account", "risk_reservations", ["account"])
    op.create_index("ix_risk_reservations_status", "risk_reservations", ["status"])
    op.create_table(
        "engine_sessions",
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(40)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name="pk_engine_sessions"),
    )


def downgrade() -> None:
    op.drop_table("engine_sessions")
    op.drop_table("risk_reservations")
    op.drop_table("risk_accounts")

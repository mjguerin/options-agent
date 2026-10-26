"""Incidents and the alert outbox (BRD 10.5, 11).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("account", sa.String(64)),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("last_alert_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
    )
    op.create_index("ix_incidents_dedupe_key", "incidents", ["dedupe_key"])
    op.create_table(
        "alert_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.BigInteger()),
        sa.Column("event", sa.String(12), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("dropped", sa.String(24)),
        sa.Column("rank", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_outbox"),
    )


def downgrade() -> None:
    op.drop_table("alert_outbox")
    op.drop_index("ix_incidents_dedupe_key", table_name="incidents")
    op.drop_table("incidents")

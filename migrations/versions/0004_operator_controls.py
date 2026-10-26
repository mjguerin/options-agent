"""Operator commands, confirmations and the audit trail (BRD 14).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_commands",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("account", sa.String(64)),
        sa.Column("symbol", sa.String(32)),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.Text()),
        sa.Column("confirm_id", sa.String(32)),
        sa.PrimaryKeyConstraint("id", name="pk_operator_commands"),
    )
    op.create_index("ix_operator_commands_status", "operator_commands", ["status"])
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64)),
        sa.Column("command", sa.String(24)),
        sa.Column("scope", sa.String(8)),
        sa.Column("account", sa.String(64)),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("command_id", sa.String(32)),
        sa.PrimaryKeyConstraint("id", name="pk_operator_actions"),
    )
    op.create_table(
        "control_confirmations",
        sa.Column("confirm_id", sa.String(32), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("account", sa.String(64)),
        sa.Column("symbol", sa.String(32)),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("confirm_id", name="pk_control_confirmations"),
    )


def downgrade() -> None:
    op.drop_table("control_confirmations")
    op.drop_table("operator_actions")
    op.drop_index("ix_operator_commands_status", table_name="operator_commands")
    op.drop_table("operator_commands")

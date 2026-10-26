"""Broker and evaluation ledgers (BRD 11, 12.3).

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False)


def upgrade() -> None:
    op.create_table(
        "ledger_sessions",
        _id(),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("account", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("reconciled", sa.Boolean(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.Column("reconciliation", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_sessions"),
    )
    op.create_index("ix_ledger_sessions_experiment_id", "ledger_sessions", ["experiment_id"])
    op.create_table(
        "ledger_trades",
        _id(),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("strategy_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True)),
        sa.Column("exit_at", sa.DateTime(timezone=True)),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("closed_qty", sa.Integer(), nullable=False),
        sa.Column("broker_entry", sa.Float(), nullable=False),
        sa.Column("broker_exit", sa.Float()),
        sa.Column("eval_entry", sa.Float(), nullable=False),
        sa.Column("eval_exit", sa.Float()),
        sa.Column("broker_gross_usd", sa.Float(), nullable=False),
        sa.Column("eval_gross_usd", sa.Float(), nullable=False),
        sa.Column("fees_usd", sa.Float(), nullable=False),
        sa.Column("net_eval_usd", sa.Float(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_trades"),
    )
    op.create_index("ix_ledger_trades_ledger_id", "ledger_trades", ["ledger_id"])
    op.create_table(
        "ledger_fills",
        _id(),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(8), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_fills"),
    )
    op.create_index("ix_ledger_fills_ledger_id", "ledger_fills", ["ledger_id"])
    op.create_table(
        "ledger_equity",
        _id(),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("realized_usd", sa.Float()),
        sa.Column("unrealized_usd", sa.Float()),
        sa.Column("estimated_usd", sa.Float()),
        sa.Column("risk_usd", sa.Float()),
        sa.Column("exposure_usd", sa.Float()),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_equity"),
    )
    op.create_index("ix_ledger_equity_ledger_id", "ledger_equity", ["ledger_id"])


def downgrade() -> None:
    op.drop_table("ledger_equity")
    op.drop_table("ledger_fills")
    op.drop_table("ledger_trades")
    op.drop_table("ledger_sessions")

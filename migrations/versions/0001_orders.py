"""Order intents and events (OM-02, OM-04, OM-07).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("account_binding", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("strategy_id", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(8), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("qty_intended", sa.Integer(), nullable=False),
        sa.Column("qty_submitted", sa.Integer(), nullable=False),
        sa.Column("qty_filled", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False),
        sa.Column("limit_price", sa.Float()),
        sa.Column("time_in_force", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("replacement", sa.Integer(), nullable=False),
        sa.Column("parent_client_order_id", sa.String(128)),
        sa.Column("broker_order_id", sa.String(64)),
        sa.Column("avg_fill_price", sa.Float()),
        sa.Column("reasons", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("client_order_id", name="pk_orders"),
    )
    op.create_index("ix_orders_account_binding", "orders", ["account_binding"])
    op.create_index("ix_orders_candidate_id", "orders", ["candidate_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_order_events"),
    )
    op.create_index("ix_order_events_client_order_id", "order_events", ["client_order_id"])


def downgrade() -> None:
    op.drop_table("order_events")
    op.drop_table("orders")

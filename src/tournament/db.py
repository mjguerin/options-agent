"""Database connection settings. PostgreSQL is the system of record (BRD 9.1).

The URL holds no password: the engine connects over the local Unix socket with
peer authentication, so there is no database secret to store.
"""

from __future__ import annotations

import os

from sqlalchemy import MetaData

DEFAULT_DATABASE_URL = "postgresql+psycopg:///options_tournament"

# Stable constraint names so Alembic autogenerate produces reviewable migrations.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def database_url() -> str:
    return os.environ.get("TOURNAMENT_DATABASE_URL", DEFAULT_DATABASE_URL)

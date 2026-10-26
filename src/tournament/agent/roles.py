"""The agent's database role and the Postgres access rules that make it read-only (AC-24).

The database used to trust every local connection, which let any account on the machine connect
as the owner's superuser role. The rules below allow the owner's account only as the owner's role
(peer authentication ties an operating-system user to the role of the same name), the agent's
account only as its read-only role, and nothing over TCP. Nothing here runs SQL itself: the
host script feeds these texts to psql after checking them.
"""

from __future__ import annotations

from tournament.agent.config import AgentConfig


def grant_sql(cfg: AgentConfig) -> str:
    h = cfg.host
    r, db, owner = h.db_role, h.database, h.owner_db_role
    return f"""\
DO $do$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{r}') THEN
    CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $do$;
ALTER ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT {h.db_connection_limit};
ALTER ROLE {r} SET default_transaction_read_only = on;
ALTER ROLE {r} SET statement_timeout = '{h.db_statement_timeout_s}s';
REVOKE ALL ON DATABASE {db} FROM PUBLIC;
GRANT CONNECT ON DATABASE {db} TO {r}, {owner};
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {r};
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {r};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {r};
ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public GRANT SELECT ON TABLES TO {r};
"""


def hba_text(cfg: AgentConfig) -> str:
    h = cfg.host
    return f"""\
# Written by tournament agent hba (options-tournament). Peer authentication: the operating-system
# user must match the role. Nothing is reachable over TCP. Edit config/agent/agent-v1.yaml.
local   all             {h.owner_db_role}          peer
local   replication     {h.owner_db_role}          peer
local   {h.database}    {h.db_role}          peer
host    all             all             127.0.0.1/32            reject
host    all             all             ::1/128                 reject
"""

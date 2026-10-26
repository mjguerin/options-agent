#!/bin/bash
# AC-24: prove, as the agent's own account, what it can and cannot do. Safe to run any time:
#     sudo ops/host/verify_agent.sh
# Every check that should be denied is attempted for real; the script reports PASS or FAIL for
# each and exits non-zero if any FAIL. It writes nothing except a temporary file it removes.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV=/opt/homebrew/bin/uv
PGBIN=/opt/homebrew/opt/postgresql@18/bin
cfg() {
    sudo -u "$OWNER" -H env PATH="/opt/homebrew/bin:$PATH" bash -c "cd '$REPO' && '$UV' run python -c \"
from tournament.agent.config import load_agent_config
c = load_agent_config()
print($1)\""
}
AGENT="$(cfg 'c.host.os_user')"; ROLE="$(cfg 'c.host.db_role')"; DB="$(cfg 'c.host.database')"
OWNER_ROLE="$(cfg 'c.host.owner_db_role')"
VIEW="$(cfg 'c.shared("view_dir")')"; OUT="$(cfg 'c.shared("narratives_dir")')"
as_agent() { sudo -u "$AGENT" -H "$@"; }
FAILS=0
expect_ok()   { if "$@" >/dev/null 2>&1; then echo "  PASS  $DESC"; else echo "  FAIL  $DESC"; FAILS=$((FAILS+1)); fi; }
expect_deny() { if "$@" >/dev/null 2>&1; then echo "  FAIL  $DESC"; FAILS=$((FAILS+1)); else echo "  PASS  $DESC"; fi; }
psql_agent() { as_agent "$PGBIN/psql" -d "$DB" -U "$ROLE" -v ON_ERROR_STOP=1 -Atc "$1"; }

echo "Agent '$AGENT' against the trading system:"
DESC="cannot read the owner's .env";                expect_deny as_agent cat "$REPO/.env"
DESC="cannot list the owner's home";                expect_deny as_agent ls "$OWNER_HOME"
DESC="cannot read the repository directly";         expect_deny as_agent ls "$REPO"
DESC="cannot read the data store";                  expect_deny as_agent ls "$REPO/data"
DESC="cannot read the owner's shell history/keys";  expect_deny as_agent ls "$OWNER_HOME/.ssh"
DESC="can read the published view";                 expect_ok   as_agent test -r "$VIEW/VIEW_MANIFEST.json"
DESC="the view holds no .env";                      expect_deny as_agent test -e "$VIEW/.env"
DESC="cannot write into the view";                  expect_deny as_agent touch "$VIEW/x"
DESC="cannot write over the project plan in the view"; expect_deny as_agent sh -c ": >> '$VIEW/README.md'"
DESC="can write a narrative file";                  expect_ok   as_agent sh -c "touch '$OUT/.verify' && rm '$OUT/.verify'"

echo "Agent against PostgreSQL:"
DESC="can SELECT from orders";                      expect_ok   psql_agent "select count(*) from orders"
DESC="can SELECT from ledger_sessions";             expect_ok   psql_agent "select count(*) from ledger_sessions"
DESC="cannot INSERT";                               expect_deny psql_agent "insert into incidents(dedupe_key) values ('x')"
DESC="cannot UPDATE";                               expect_deny psql_agent "update orders set status='x'"
DESC="cannot DELETE";                               expect_deny psql_agent "delete from orders"
DESC="cannot CREATE a table";                       expect_deny psql_agent "create table agent_probe(i int)"
DESC="cannot DROP a table";                         expect_deny psql_agent "drop table orders"
DESC="cannot turn read-only off and write";         expect_deny psql_agent "set default_transaction_read_only=off; set transaction read write; delete from orders"
DESC="cannot connect as the owner's role";          expect_deny as_agent "$PGBIN/psql" -d "$DB" -U "$OWNER_ROLE" -Atc "select 1"
DESC="cannot connect as a superuser role";          expect_deny as_agent "$PGBIN/psql" -d postgres -U postgres -Atc "select 1"
DESC="cannot reach Postgres over TCP";              expect_deny as_agent "$PGBIN/psql" -h 127.0.0.1 -d "$DB" -U "$ROLE" -Atc "select 1"
DESC="the owner's account still connects";          expect_ok   sudo -u "$OWNER" "$PGBIN/psql" -d "$DB" -Atc "select 1"

echo
if [[ $FAILS -eq 0 ]]; then echo "ALL CHECKS PASS"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi

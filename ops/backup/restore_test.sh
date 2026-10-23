#!/bin/bash
# Restore the latest dump into a scratch database and compare row counts with
# the live database (NFR-09: restore is tested, not assumed). Run in the evening
# while the engine is stopped, so the live counts don't move under the test.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

CONF="${BACKUP_CONF:-$HOME/.config/options-tournament/backup.conf}"
# shellcheck source=/dev/null
[[ -f "$CONF" ]] && source "$CONF"

DB="${BACKUP_DB:-options_tournament}"
LOCAL_DIR="${BACKUP_LOCAL_DIR:-$HOME/backups/options-tournament}"
TEST_DB="${DB}_restore_test"
dump="${1:-$(ls -t "$LOCAL_DIR"/pg/"${DB}"_*.dump 2>/dev/null | head -1)}"

[[ -n "$dump" && -f "$dump" ]] || { echo "no dump found in $LOCAL_DIR/pg" >&2; exit 1; }
echo "restoring $dump into $TEST_DB"

dropdb --if-exists "$TEST_DB"
createdb "$TEST_DB"
trap 'dropdb --if-exists "$TEST_DB"' EXIT
pg_restore --no-owner --exit-on-error --dbname="$TEST_DB" "$dump"

counts() {
    psql -X -At -d "$1" -c "
        select format('select %L || ''|'' || count(*) from %I.%I;', table_schema || '.' || table_name,
                      table_schema, table_name)
        from information_schema.tables
        where table_type = 'BASE TABLE' and table_schema not in ('pg_catalog', 'information_schema')
        order by 1" | psql -X -At -d "$1"
}

live="$(counts "$DB")"
restored="$(counts "$TEST_DB")"
if [[ "$live" == "$restored" ]]; then
    echo "OK: $(echo "$restored" | grep -c . || true) tables match"
    echo "$restored" | sed 's/^/  /'
else
    echo "MISMATCH between $DB and restored dump:" >&2
    diff <(echo "$live") <(echo "$restored") >&2 || true
    exit 2
fi

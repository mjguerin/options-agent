#!/bin/bash
# Nightly PostgreSQL dump (NFR-09). Local only by owner decision; set
# BACKUP_OFFSITE to also copy dumps and the Parquet archive elsewhere.
# Exit codes: 0 ok, 1 dump failed.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

CONF="${BACKUP_CONF:-$HOME/.config/options-tournament/backup.conf}"
# shellcheck source=/dev/null
[[ -f "$CONF" ]] && source "$CONF"

DB="${BACKUP_DB:-options_tournament}"
LOCAL_DIR="${BACKUP_LOCAL_DIR:-$HOME/backups/options-tournament}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
OFFSITE="${BACKUP_OFFSITE:-}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

mkdir -p "$LOCAL_DIR/pg"
chmod 700 "$LOCAL_DIR"

out="$LOCAL_DIR/pg/${DB}_$(date -u +%Y%m%dT%H%M%SZ).dump"
log "dumping $DB -> $out"
if ! pg_dump --format=custom --dbname="$DB" --file="$out.partial"; then
    rm -f "$out.partial"
    log "FAILED: pg_dump"
    exit 1
fi
mv "$out.partial" "$out"
# A dump that pg_restore can't list is not a backup.
pg_restore --list "$out" >/dev/null
log "ok $(du -h "$out" | cut -f1)"

find "$LOCAL_DIR/pg" -name "${DB}_*.dump" -mtime +"$KEEP_DAYS" -print -delete |
    sed 's/^/pruned /' || true

if [[ -z "$OFFSITE" ]]; then
    log "local only (no BACKUP_OFFSITE)"
    exit 0
fi

# Never --delete on the far side: a bad night must not erase good copies.
log "copying to $OFFSITE"
rsync -a "$LOCAL_DIR/pg/" "$OFFSITE/pg/"
if [[ -d "$REPO/data" ]]; then
    rsync -a "$REPO/data/" "$OFFSITE/data/"   # daily Parquet archive
fi
log "off-machine copy done"

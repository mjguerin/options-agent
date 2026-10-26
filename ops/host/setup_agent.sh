#!/bin/bash
# Set up the isolated agent (Step 3; BRD 13, AG-07, AG-11, AG-13, AC-24).
# Run on the mini after the close:   sudo ops/host/setup_agent.sh
#
# What it does, all driven by config/agent/agent-v1.yaml:
#   1. checks the agent's macOS account exists and has no admin rights;
#   2. creates the shared folders (the view the agent reads, the narratives it writes);
#   3. creates the agent's read-only PostgreSQL role and tightens the Postgres access rules, so
#      no account but the owner's can connect as the owner's role, and nothing connects over TCP;
#   4. installs the agent's Claude Code permissions and the two daily jobs;
#   5. publishes the first view and runs the isolation checks (ops/host/verify_agent.sh).
# Idempotent. It does not touch the older trading jobs, and it does not read .env.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV=/opt/homebrew/bin/uv
PGBIN=/opt/homebrew/opt/postgresql@18/bin
STAGE="$(mktemp -d /private/tmp/options-tournament-stage.XXXXXX)"
chown "$OWNER" "$STAGE"  # the owner renders into it
step() { printf '\n== %s\n' "$*"; }
as_owner() { sudo -u "$OWNER" -H env PATH="/opt/homebrew/bin:$PGBIN:$PATH" "$@"; }
cfg() {  # cfg <python expression over c>
    as_owner bash -c "cd '$REPO' && '$UV' run python -c 'import sys
from tournament.agent.config import load_agent_config
c = load_agent_config()
print(eval(sys.argv[1]))' \"\$1\"" _ "$1"
}

AGENT="$(cfg 'c.host.os_user')"
AGENT_HOME="/Users/$AGENT"
SHARED="$(cfg 'c.host.shared_root')"
VIEW="$(cfg 'c.shared("view_dir")')"
OUT="$(cfg 'c.shared("narratives_dir")')"
LOGS="$(cfg 'c.shared("agent_logs_dir")')"
DB="$(cfg 'c.host.database')"
ROLE="$(cfg 'c.host.db_role')"

step "The agent's account"
id "$AGENT" >/dev/null || { echo "no macOS user '$AGENT'; create it in System Settings > Users" >&2; exit 1; }
if dseditgroup -o checkmember -m "$AGENT" admin >/dev/null 2>&1; then
    echo "  '$AGENT' is an administrator: make it a Standard user, then rerun" >&2
    exit 1
fi
echo "  $AGENT: standard user, home $AGENT_HOME"

step "Keep $AGENT out of the owner's home"
# The home is mode 750 with group staff, and the agent is in staff, so without this it could
# walk through to the repository, .env and the data. A deny entry for this one account fixes
# that and changes nothing for anyone else.
if ls -lde "$OWNER_HOME" | grep -q "user:$AGENT deny"; then
    echo "  already denied"
else
    chmod +a "user:$AGENT deny list,search,read,readattr,readextattr,readsecurity" "$OWNER_HOME"
    echo "  $AGENT denied on $OWNER_HOME"
fi

step "Shared folders"
install -d -o "$OWNER" -g staff -m 755 "$SHARED"
install -d -o "$AGENT" -g staff -m 755 "$OUT" "$LOGS"
as_owner mkdir -p "$OWNER_HOME/Library/Logs/options-tournament"
echo "  $SHARED (owner writes the view; the agent writes narratives and logs)"

step "Render the agent's files from the config"
as_owner bash -c "cd '$REPO' && '$UV' run tournament agent render \
    --owner '$OWNER' --home '$OWNER_HOME' --repo '$REPO' --uv '$UV' --out '$STAGE'"
plutil -lint "$STAGE/agent-narrative.plist" "$STAGE/agent-view.plist" >/dev/null

step "PostgreSQL: the read-only role"
as_owner "$PGBIN/psql" -d "$DB" -v ON_ERROR_STOP=1 -q -f "$STAGE/agent-grants.sql"
echo "  role $ROLE: SELECT only, read-only transactions"

step "PostgreSQL: access rules (peer authentication, nothing over TCP)"
HBA="$(as_owner "$PGBIN/psql" -d "$DB" -Atc 'show hba_file')"
if cmp -s "$STAGE/pg_hba.conf" "$HBA"; then
    echo "  already in place"
else
    BACKUP="$HBA.before-agent.$(date +%Y%m%d%H%M%S)"
    cp -p "$HBA" "$BACKUP"
    install -o "$OWNER" -g staff -m 600 "$STAGE/pg_hba.conf" "$HBA"
    if ! as_owner "$PGBIN/psql" -d "$DB" -Atc 'select pg_reload_conf()' >/dev/null \
        || ! as_owner "$PGBIN/psql" -d "$DB" -Atc 'select 1' >/dev/null; then
        cp -p "$BACKUP" "$HBA"
        as_owner "$PGBIN/pg_ctl" reload -D "$(dirname "$HBA")" || true
        echo "  the new rules failed; the old ones are back ($BACKUP)" >&2
        exit 1
    fi
    sleep 1
    BAD="$(as_owner "$PGBIN/psql" -d "$DB" -Atc 'select count(*) from pg_hba_file_rules where error is not null')"
    [[ "$BAD" == "0" ]] || { cp -p "$BACKUP" "$HBA"; as_owner "$PGBIN/psql" -d "$DB" -Atc 'select pg_reload_conf()' >/dev/null; echo "  rule errors; reverted" >&2; exit 1; }
    echo "  applied; the previous file is $BACKUP"
fi

step "Claude Code permissions for $AGENT"
install -d -o "$AGENT" -g staff -m 700 "$AGENT_HOME/.claude"
install -o "$AGENT" -g staff -m 600 "$STAGE/claude-settings.json" "$AGENT_HOME/.claude/settings.json"
if [[ -x "$(cfg 'c.host.claude_bin')" ]]; then
    echo "  claude is installed for $AGENT"
else
    echo "  claude is NOT installed for $AGENT yet. Install it and sign in (see the end)."
fi

step "Publish the first view"
as_owner bash -c "cd '$REPO' && '$UV' run tournament agent publish"

step "Install the two daily jobs"
for job in agent-view agent-narrative; do
    label="com.options-tournament.$job"
    install -o root -g wheel -m 644 "$STAGE/$job.plist" "/Library/LaunchDaemons/$label.plist"
    launchctl bootout "system/$label" 2>/dev/null || true
    launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
    echo "  loaded $label"
done

step "Isolation checks (AC-24)"
bash "$REPO/ops/host/verify_agent.sh" || { echo "ISOLATION CHECKS FAILED" >&2; rm -rf "$STAGE"; exit 1; }
rm -rf "$STAGE"

cat <<MSG

Next, you do these once, in a Terminal on the mini (or over SSH):
  sudo -u $AGENT -H bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
  sudo -u $AGENT -H $(cfg 'c.host.claude_bin')        # then /login with your Pro plan
For Remote Control, run it inside tmux as $AGENT (see the plan).
MSG

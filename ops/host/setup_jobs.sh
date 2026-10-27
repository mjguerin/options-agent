#!/bin/bash
# Install the daily jobs (Step 3; BRD 9.3, 10.2): the tournament's own Supervisor, the engine
# start at the pre-market time, and the nightly download. Run on the mini after the close:
#     sudo ops/host/setup_jobs.sh
#
# Idempotent. It renders everything from config/app/app-v1.yaml, so a change of time is an edit
# to that file and a rerun of this script. It touches only com.options-tournament.* jobs: not
# Homebrew's Supervisor, not /opt/homebrew/etc/supervisor.d, and none of the older trading jobs.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV=/opt/homebrew/bin/uv
STAGE="$(mktemp -d /private/tmp/options-tournament-stage.XXXXXX)"
chown "$OWNER" "$STAGE"  # the owner renders into it
step() { printf '\n== %s\n' "$*"; }
reload_daemon() {  # reload_daemon <label> <plist>: bootout can return before the job is gone
    local label="$1" plist="$2" i
    launchctl bootout "system/$label" 2>/dev/null || true
    for i in $(seq 1 30); do
        launchctl print "system/$label" >/dev/null 2>&1 || break
        sleep 0.5
    done
    for i in 1 2 3 4 5 6; do
        launchctl bootstrap system "$plist" && return 0
        sleep 2
    done
    echo "could not load $label" >&2
    return 1
}


step "Time zone: launchd fires on the host's local time"
echo "  host zone: $(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
grep -q 'timezone: America/New_York' "$REPO/config/app/app-v1.yaml" \
    && echo "  config expects America/New_York: keep the host on it" || true

step "Render the job files from the app config"
sudo -u "$OWNER" -H bash -c "cd '$REPO' && '$UV' run tournament ops render \
    --owner '$OWNER' --home '$OWNER_HOME' --repo '$REPO' --uv '$UV' --out '$STAGE'"
sudo -u "$OWNER" mkdir -p "$REPO/data/run" "$OWNER_HOME/Library/Logs/options-tournament"
sudo -u "$OWNER" cp "$STAGE/supervisord.conf" "$REPO/data/run/supervisord.conf"

step "Install the LaunchDaemons (root-owned, 644)"
for job in supervisor engine-start nightly; do
    label="com.options-tournament.$job"
    plutil -lint "$STAGE/$job.plist" >/dev/null
    install -o root -g wheel -m 644 "$STAGE/$job.plist" "/Library/LaunchDaemons/$label.plist"
    reload_daemon "$label" "/Library/LaunchDaemons/$label.plist"
    echo "  loaded $label"
done

step "Check"
sleep 3
sudo -u "$OWNER" /opt/homebrew/bin/supervisorctl -c "$REPO/data/run/supervisord.conf" status || true
launchctl list | grep options-tournament || true
rm -rf "$STAGE"

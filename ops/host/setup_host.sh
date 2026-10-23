#!/bin/bash
# One-time host setup for the Mac mini (Step 1; BRD 9.2, NFR-13, NFR-14).
# Run on the mini, after the market close:  sudo ops/host/setup_host.sh
#
# Idempotent: safe to run again. It does not touch anything else on the machine
# (other launchd jobs, Docker, Tailscale).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
OWNER_UID="$(id -u "$OWNER")"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
step() { printf '\n== %s\n' "$*"; }

render() {  # render <template> <dest>: fill placeholders, install root-owned 644
    sed -e "s|__USER__|$OWNER|g" -e "s|__HOME__|$OWNER_HOME|g" -e "s|__REPO__|$REPO|g" "$1" >"$2"
    chown root:wheel "$2"
    chmod 644 "$2"
    plutil -lint "$2" >/dev/null
}

load_daemon() {  # (re)load a LaunchDaemon from its plist
    launchctl bootout "system/$1" 2>/dev/null || true
    launchctl bootstrap system "/Library/LaunchDaemons/$1.plist"
}

step "Power: no sleep, restart after power failure, wake on network"
pmset -a sleep 0 disksleep 0 standby 0 autorestart 1 womp 1
pmset -g | grep -E '^ (sleep|disksleep|standby|autorestart|womp) '

step "Network time"
systemsetup -setnetworktimeserver time.apple.com >/dev/null 2>&1 || true
systemsetup -setusingnetworktime on 2>&1 || echo "  (systemsetup refused; check System Settings > General > Date & Time)"
sntp -t 2 time.apple.com | tail -1

step "SSH: key-only login"
if [[ ! -s "$OWNER_HOME/.ssh/authorized_keys" ]]; then
    echo "  SKIPPED: $OWNER has no authorized_keys; key-only login would lock you out"
else
    conf=/etc/ssh/sshd_config.d/000-options-tournament.conf
    install -m 644 -o root -g wheel "$REPO/ops/sshd/000-options-tournament.conf" "$conf"
    if ! sshd -t; then
        rm -f "$conf"
        echo "  sshd config test failed; change reverted" >&2
        exit 1
    fi
    # macOS starts sshd per connection, so new logins pick this up; open sessions stay up.
    sshd -T -C "user=$OWNER,host=localhost,addr=127.0.0.1" |
        grep -E '^(passwordauthentication|kbdinteractiveauthentication|authenticationmethods|permitrootlogin) '
fi

step "PostgreSQL: start at boot without a login"
agent="$OWNER_HOME/Library/LaunchAgents/homebrew.mxcl.postgresql@18.plist"
if [[ -f "$agent" ]]; then
    launchctl bootout "gui/$OWNER_UID/homebrew.mxcl.postgresql@18" 2>/dev/null ||
        launchctl bootout "user/$OWNER_UID/homebrew.mxcl.postgresql@18" 2>/dev/null || true
    mkdir -p "$OWNER_HOME/Library/LaunchAgents/disabled"
    mv "$agent" "$OWNER_HOME/Library/LaunchAgents/disabled/"
    chown -R "$OWNER" "$OWNER_HOME/Library/LaunchAgents/disabled"
    echo "  moved Homebrew's per-login agent to LaunchAgents/disabled/"
fi
for _ in {1..30}; do
    sudo -u "$OWNER" /opt/homebrew/bin/pg_ctl status -D /opt/homebrew/var/postgresql@18 >/dev/null 2>&1 || break
    sleep 1
done
render "$REPO/ops/launchd/com.options-tournament.postgresql.plist" \
    /Library/LaunchDaemons/com.options-tournament.postgresql.plist
load_daemon com.options-tournament.postgresql
for _ in {1..30}; do
    /opt/homebrew/bin/pg_isready -q -h localhost && break
    sleep 1
done
/opt/homebrew/bin/pg_isready -h localhost

step "Nightly backup at 20:00"
sudo -u "$OWNER" mkdir -p "$OWNER_HOME/Library/Logs/options-tournament" "$OWNER_HOME/.config/options-tournament"
if [[ ! -f "$OWNER_HOME/.config/options-tournament/backup.conf" ]]; then
    sudo -u "$OWNER" cp "$REPO/ops/backup/backup.conf.example" "$OWNER_HOME/.config/options-tournament/backup.conf"
    echo "  created ~/.config/options-tournament/backup.conf"
fi
render "$REPO/ops/launchd/com.options-tournament.backup.plist" \
    /Library/LaunchDaemons/com.options-tournament.backup.plist
load_daemon com.options-tournament.backup
launchctl print system/com.options-tournament.backup | grep -E '^\s*state'

step "Done"
echo "Still manual: connect the UPS (it should then show in 'pmset -g ps'),"
echo "and run the reboot test: sudo shutdown -r now, then 'ssh mini' and check pg_isready."

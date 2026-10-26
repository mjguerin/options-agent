#!/bin/bash
# AC-13: controlled negative tests of the agent's tools, run as the agent's own account with the
# narrative job's settings. It asks the model to run a command, write a file, edit the risk limits,
# fetch a web page and read the broker credentials, and checks that each was refused, left no side
# effect and was audited. Needs Claude Code signed in as the agent and uses a few model calls:
#     sudo ops/host/verify_agent_tools.sh
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV=/opt/homebrew/bin/uv
cfg() {
    sudo -u "$OWNER" -H env PATH="/opt/homebrew/bin:$PATH" bash -c "cd '$REPO' && '$UV' run python -c 'import sys
from tournament.agent.config import load_agent_config
c = load_agent_config()
print(eval(sys.argv[1]))' \"\$1\"" _ "$1"
}
AGENT="$(cfg 'c.host.os_user')"
VIEW="$(cfg 'c.shared("view_dir")')"
cd /tmp
sudo -u "$AGENT" -H "$(cfg 'c.host.python')" "$VIEW/ops/agent/verify_denials.py" \
    --view "$VIEW" --claude "$(cfg 'c.host.claude_bin')" --model "$(cfg 'c.narrative.model')" \
    --timeout "$(cfg 'c.narrative.timeout_s')" --audit "$(cfg 'c.audit_path')" \
    --owner-env "$REPO/.env" \
    --allowed $(cfg '" ".join(c.narrative.allowed_tools)') \
    --disallowed $(cfg '" ".join(c.narrative.disallowed_tools)')

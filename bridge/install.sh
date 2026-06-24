#!/usr/bin/env bash
# One-shot setup for the Claude Code → buddy bridge.
# Idempotent: safe to re-run. Creates the venv, installs bleak, and prepares
# the runtime dir. The repo's project hooks (.claude/settings.json) are already
# committed, so after this you only need to restart Claude Code.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: '$PY' not found. Install Python 3, or set PYTHON=/path/to/python3." >&2
  exit 1
fi

echo "[buddy] creating venv → $(pwd)/.venv"
"$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
echo "[buddy] installing dependencies (bleak)…"
./.venv/bin/python -m pip install --quiet -r requirements.txt

mkdir -p "$HOME/.claude-buddy"

# sanity-check the daemon imports cleanly with bleak available
./.venv/bin/python -c "import buddy_bridge" >/dev/null
echo "[buddy] daemon imports OK."

cat <<'EOF'

[buddy] setup complete.

  1. Restart Claude Code (VS Code) so the project hooks load + are trusted.
  2. Wake the stick and make sure the desktop app's Hardware Buddy is
     DISCONNECTED — BLE allows only one central at a time.
  3. First connect pops a macOS pairing dialog: enter the 6-digit passkey
     shown on the stick. Reconnects reuse the stored key.

Optional:
  • Drive the buddy from EVERY project on this machine:
        python3 wire_hooks.py user        (undo: --remove)
  • Set your display name / scan filter:
        echo '{"owner":"YOURNAME"}' > ~/.claude-buddy/config.json
  • Watch the bridge:
        tail -f ~/.claude-buddy/bridge.log
  • Run it in the foreground for first-time pairing:
        ./.venv/bin/python buddy_bridge.py --foreground
EOF

#!/usr/bin/env bash
# One-shot setup for the Claude Code → buddy bridge.
# Idempotent: safe to re-run. Creates the venv, installs bleak, prepares the
# runtime dir, and wires the hooks GLOBALLY (~/.claude/settings.json) so the
# buddy reacts to Claude Code in every project — not just this repo.
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

# Wire hooks into the user settings (all projects). Absolute path to this clone.
"$PY" wire_hooks.py user

cat <<'EOF'

[buddy] setup complete. Hooks are installed globally (all projects).

  1. Restart Claude Code (VS Code) so the hooks load + are trusted.
  2. Wake the stick and make sure the desktop app's Hardware Buddy is
     DISCONNECTED — BLE allows only one central at a time.
  3. First connect pops a macOS pairing dialog: enter the 6-digit passkey
     shown on the stick. Reconnects reuse the stored key.

Notes:
  • The global hooks point at THIS folder's buddy_hook.py — keep the repo here
    (or re-run this script after moving it). Undo: python3 wire_hooks.py user --remove
  • Repo-only instead of global: python3 wire_hooks.py project
  • Set your display name:  echo '{"owner":"YOURNAME"}' > ~/.claude-buddy/config.json
  • Watch the bridge:       tail -f ~/.claude-buddy/bridge.log
  • Foreground (pairing):   ./.venv/bin/python buddy_bridge.py --foreground
EOF

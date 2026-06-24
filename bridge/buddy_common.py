"""Shared paths + config for the Claude Code → buddy bridge.

Pure stdlib so both the hook client (system python3) and the daemon (venv
python with bleak) can import it. All machine-specific locations are derived
at runtime — nothing is hardcoded — so the repo works unchanged on any clone.
"""
import json
import os

# Per-user runtime dir. Deterministic across processes (no $TMPDIR ambiguity),
# short enough for an AF_UNIX path on macOS (<104 chars), and the same value
# whether computed by the hook or the daemon.
BUDDY_DIR = os.path.join(os.path.expanduser("~"), ".claude-buddy")
SOCK_PATH = os.path.join(BUDDY_DIR, "buddy.sock")
LOG_PATH = os.path.join(BUDDY_DIR, "bridge.log")
CONFIG_PATH = os.path.join(BUDDY_DIR, "config.json")

DEFAULTS = {
    # BLE advertised-name prefix to scan for (firmware advertises "Claude…").
    "name_prefix": "Claude",
    # Owner first name shown on the device. "" → leave the device's stored name.
    "owner": "",
    # Safety-net only: a turn is busy from UserPromptSubmit until Stop, and Stop
    # fires reliably, so this just clears a stuck "busy" if a Stop is ever
    # missed. It must be LONGER than the longest gap *within* a turn (Claude can
    # generate text / think for tens of seconds with no hook), or the buddy
    # falls back to idle mid-work. See NOTES.md.
    "idle_timeout": 600.0,
    # Seconds a PreToolUse may sit without its PostToolUse before we treat the
    # tool as blocked on the approval prompt → attention. The Pre→Post gap is
    # how we detect "waiting for approval" without the Notification hook.
    "approve_wait": 1.5,
    # Resend the snapshot at least this often even if nothing changed, so the
    # firmware's ~30s dead-link timeout never trips.
    "keepalive": 10.0,
    # The firmware only shows the "busy" (sweating) animation at
    # sessionsRunning >= 3 (see NOTES.md); a single active session reads as
    # idle. Set true to report running=3 whenever ANY session is generating, so
    # the buddy looks awake while you work. Off = faithful to the desktop app.
    "busy_boost": False,
    # Pending approval entries older than this are assumed resolved (a *denied*
    # tool never emits PostToolUse, so it would otherwise stick on attention).
    "pending_ttl": 180.0,
}


def ensure_dir():
    os.makedirs(BUDDY_DIR, exist_ok=True)


def load_config():
    """DEFAULTS, overlaid by ~/.claude-buddy/config.json, overlaid by env."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        pass  # malformed config must never break the bridge
    if os.environ.get("CLAUDE_BUDDY_NAME"):
        cfg["name_prefix"] = os.environ["CLAUDE_BUDDY_NAME"]
    if os.environ.get("CLAUDE_BUDDY_OWNER"):
        cfg["owner"] = os.environ["CLAUDE_BUDDY_OWNER"]
    return cfg

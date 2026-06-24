#!/usr/bin/env python3
"""Wire (or remove) the buddy bridge hooks in a Claude Code settings.json.

The project copy (bridge-local .claude/settings.json) is committed and uses
$CLAUDE_PROJECT_DIR, so it travels with the repo. Use this script to also drive
the buddy from EVERY project on a machine, by merging the hooks into the user
settings (~/.claude/settings.json) with an absolute path computed here:

    python3 wire_hooks.py user            # buddy reacts to all Claude Code work
    python3 wire_hooks.py user --remove   # undo
    python3 wire_hooks.py project          # (re)generate the repo's .claude/settings.json

Idempotent: it only ever touches hook entries whose command references our own
scripts (buddy_hook.py / log_event.py); your other hooks are left untouched.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Events we listen on. (matcher=True → needs a tool matcher field.)
EVENTS = [
    ("SessionStart", False),
    ("SessionEnd", False),
    ("UserPromptSubmit", False),
    ("PreToolUse", True),
    ("PostToolUse", True),
    ("Notification", False),  # no-op in the VS Code extension, useful in the CLI
    ("Stop", False),
    ("SubagentStop", False),
]

MANAGED = ("buddy_hook.py", "log_event.py")

# PreToolUse may block on a device button (button approval). Claude Code kills
# hooks at 60s by default, so give it room beyond approve_timeout (300s).
PRETOOLUSE_TIMEOUT = 360


def cmd_for(target):
    if target == "project":
        path = '"$CLAUDE_PROJECT_DIR/bridge/buddy_hook.py"'
    else:
        path = json.dumps(os.path.join(HERE, "buddy_hook.py"))
    return f"python3 {path}"


def settings_path(target):
    if target == "project":
        return os.path.join(REPO, ".claude", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        sys.exit(f"refusing to edit malformed {path}: {e}")


def strip_managed(groups):
    """Drop hook groups that only run our managed scripts; keep the rest."""
    kept = []
    for g in groups:
        hooks = [h for h in g.get("hooks", [])
                 if not any(m in (h.get("command") or "") for m in MANAGED)]
        if hooks:
            kept.append({**g, "hooks": hooks})
        elif not g.get("hooks"):
            kept.append(g)
    return kept


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "project"
    if target not in ("project", "user"):
        sys.exit("usage: wire_hooks.py [project|user] [--remove]")
    remove = "--remove" in sys.argv

    path = settings_path(target)
    data = load(path)
    hooks = data.get("hooks", {})
    cmd = cmd_for(target)

    for event, needs_matcher in EVENTS:
        groups = strip_managed(hooks.get(event, []))
        if not remove:
            hook_cmd = {"type": "command", "command": cmd}
            if event == "PreToolUse":
                hook_cmd["timeout"] = PRETOOLUSE_TIMEOUT
            entry = {"hooks": [hook_cmd]}
            if needs_matcher:
                entry = {"matcher": "*", **entry}
            groups.append(entry)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)

    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    verb = "removed from" if remove else "wired into"
    print(f"[buddy] hooks {verb} {path}")
    if not remove:
        print("[buddy] restart Claude Code so the hooks load.")


if __name__ == "__main__":
    main()

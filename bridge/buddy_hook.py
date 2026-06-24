#!/usr/bin/env python3
"""Claude Code hook → buddy bridge (fire-and-forget event reporter).

Wired to Claude Code hook events. Reads the hook JSON on stdin, forwards a
compact event to the bridge daemon over a unix socket, and exits fast. If the
daemon isn't running it spawns it (detached, via the bundled venv) and moves on.

Hard rule: this must never block or fail Claude Code. Any error → silent exit 0,
no stdout. Locations are resolved from __file__, so it works on any clone.

FUTURE — button approval: a `--approve` mode will keep the socket open on
PreToolUse, wait for the device's button decision, and print Claude Code's
permissionDecision JSON to stdout. The daemon already reads the device TX
channel; only this client mode and a request/reply on the socket are missing.
See NOTES.md.
"""
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import buddy_common as bc  # noqa: E402

VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
DAEMON = os.path.join(HERE, "buddy_bridge.py")


def read_hook():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def build_event(h):
    ev = {
        "event": h.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else ""),
        "session_id": h.get("session_id") or "default",
    }
    if h.get("tool_name"):
        ev["tool"] = h["tool_name"]
    if h.get("source"):
        ev["source"] = h["source"]
    if h.get("reason"):
        ev["reason"] = h["reason"]
    # tool_use_id correlates PreToolUse↔PostToolUse for the approval-gap heuristic.
    if h.get("tool_use_id"):
        ev["uid"] = h["tool_use_id"]
    # A short hint for the tiny display (command / file / url / pattern / message).
    ti = h.get("tool_input") or {}
    hint = (ti.get("command") or ti.get("file_path") or ti.get("path")
            or ti.get("url") or ti.get("pattern") or h.get("message"))
    if isinstance(hint, str) and hint:
        ev["hint"] = hint[:120]
    return ev


def send(ev, timeout=0.4):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(bc.SOCK_PATH)
        s.sendall((json.dumps(ev) + "\n").encode())
    finally:
        s.close()


def spawn_daemon():
    if not os.path.exists(VENV_PY):
        return  # install.sh hasn't run; nothing we can do silently
    try:
        subprocess.Popen(
            [VENV_PY, DAEMON],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=HERE,
        )
    except Exception:
        pass


def request_approval(ev, cfg):
    """Block on the device for a decision. Returns "allow" | "deny" | "ask".

    "ask" means defer to the normal VS Code permission prompt — returned when the
    device is disconnected/busy, the daemon is down, or it times out, so Claude
    Code never deadlocks waiting on a button.
    """
    timeout = float(cfg.get("approve_timeout", 30.0)) + 5.0
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(bc.SOCK_PATH)
        req = dict(ev)
        req["approve_req"] = True
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        decision = json.loads(buf.split(b"\n", 1)[0].decode()).get("decision", "ask")
        return decision if decision in ("allow", "deny", "ask") else "ask"
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError, ValueError):
        spawn_daemon()  # bring it up for next time; defer this one
        return "ask"
    finally:
        try:
            s.close()
        except Exception:
            pass


def emit_decision(decision):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": "buddy: " + ("approved" if decision == "allow" else "denied") + " on device",
    }}))


def main():
    ev = build_event(read_hook())
    cfg = bc.load_config()

    # Button approval: route a gated PreToolUse to the device and block for A/B.
    if (ev.get("event") == "PreToolUse" and cfg.get("button_approval")
            and ev.get("tool") in set(cfg.get("button_approval_tools") or [])):
        decision = request_approval(ev, cfg)
        if decision in ("allow", "deny"):
            emit_decision(decision)
        # "ask" → emit nothing → fall through to the normal VS Code prompt
        sys.exit(0)

    # Display-only path: fire-and-forget event report.
    try:
        send(ev)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        spawn_daemon()
        time.sleep(0.3)
        try:
            send(ev)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()

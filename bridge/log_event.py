#!/usr/bin/env python3
"""Diagnostic hook logger.

Wired to every Claude Code hook event. Appends one line per invocation to
LOGFILE recording the event name and the full hook payload, so we can see
which hook events actually fire inside the VS Code extension (vs the CLI).

Pure stdlib; exits 0 with no stdout so it never alters Claude's behavior.
"""
import json, os, sys, tempfile, time

LOGFILE = os.path.join(tempfile.gettempdir(), "claude-hook-events.log")


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {"_unparsed": raw[:500]}

    event = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "?")
    # Monotonic-ish wall clock is fine here; this is a diagnostic.
    ts = time.strftime("%H:%M:%S")
    # Keep payloads readable but compact; drop huge fields.
    slim = {k: v for k, v in data.items() if k not in ("transcript_path",)}
    line = f"{ts}  {event:<18}  {json.dumps(slim, ensure_ascii=False)}\n"
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

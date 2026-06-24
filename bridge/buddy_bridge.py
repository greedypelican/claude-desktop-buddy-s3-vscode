#!/usr/bin/env python3
"""Claude Code → M5StickS3 buddy bridge daemon.

Plays the BLE central role the macOS/Windows desktop app plays — but driven by
Claude Code hook events instead of the desktop app's session manager. It:

  1. Connects to the buddy over BLE Nordic UART Service (firmware unchanged).
  2. Receives hook events from buddy_hook.py over a unix socket.
  3. Maps them to the Hardware Buddy wire protocol (REFERENCE.md) and pushes
     heartbeat snapshots so the device shows sleep / idle / busy / attention.

Display-only today: it reports state, it does not gate permissions. The device
TX channel is already parsed (acks / future permission replies) so the blocking
button-approval round-trip can be added without re-plumbing — see NOTES.md.

Run detached by buddy_hook.py, or in the foreground for first-run pairing:
    ./.venv/bin/python buddy_bridge.py --foreground
"""
import asyncio
import fcntl
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import buddy_common as bc  # noqa: E402

from bleak import BleakClient, BleakScanner  # noqa: E402

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # central → device (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → central (notify)

CFG = bc.load_config()
FOREGROUND = "--foreground" in sys.argv or "-f" in sys.argv


def log(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}\n"
    try:
        with open(bc.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    if FOREGROUND:
        sys.stderr.write(line)
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# State model
#
# sessions: sid -> {"busy": bool, "last": monotonic_ts}
# pending:  tool_use_id -> {"sid","tool","hint","ts"}  (PreToolUse awaiting Post)
# A pending entry older than approve_wait means the tool is blocked on the
# approval prompt → attention. Cleared on its PostToolUse, on Stop / new prompt
# for the session, or after pending_ttl (a denied tool never emits PostToolUse).
# ---------------------------------------------------------------------------
def new_state():
    return {"sessions": {}, "pending": {}, "lock": asyncio.Lock(), "stop": False}


def clear_pending(state, sid):
    for uid in [u for u, p in state["pending"].items() if p["sid"] == sid]:
        state["pending"].pop(uid, None)


def apply_event(state, ev):
    name = ev.get("event")
    sid = ev.get("session_id") or "default"
    now = time.monotonic()
    S = state["sessions"]

    if name == "SessionStart":
        S.setdefault(sid, {})["busy"] = False
        S[sid]["last"] = now
    elif name == "SessionEnd":
        S.pop(sid, None)
        clear_pending(state, sid)
    elif name == "UserPromptSubmit":
        s = S.setdefault(sid, {})
        s["busy"] = True
        s["last"] = now
        clear_pending(state, sid)
    elif name == "PreToolUse":
        s = S.setdefault(sid, {})
        s["busy"] = True
        s["last"] = now
        uid = ev.get("uid")
        if uid:
            state["pending"][uid] = {
                "sid": sid,
                "tool": ev.get("tool", ""),
                "hint": ev.get("hint", ""),
                "ts": now,
            }
    elif name == "PostToolUse":
        s = S.setdefault(sid, {})
        s["busy"] = True
        s["last"] = now
        uid = ev.get("uid")
        if uid:
            state["pending"].pop(uid, None)
    elif name == "Stop":
        # Turn finished — back to idle.
        s = S.setdefault(sid, {})
        s["busy"] = False
        s["last"] = now
        clear_pending(state, sid)
    elif name == "SubagentStop":
        # A subagent finished but the main turn is still running — stay busy.
        s = S.setdefault(sid, {})
        s["busy"] = True
        s["last"] = now
    elif name == "Notification":
        # Doesn't fire in the VS Code extension, but does in the CLI — treat it
        # as a generic "needs you" attention nudge when present.
        s = S.setdefault(sid, {})
        s["last"] = now
        state["pending"].setdefault(
            "notif:" + sid,
            {"sid": sid, "tool": "", "hint": ev.get("hint", ""), "ts": now},
        )
    # unknown events: ignore


def compute_snapshot(state):
    now = time.monotonic()
    S = state["sessions"]

    # expire stale pending + idle-out busy sessions
    for uid in [u for u, p in state["pending"].items()
                if now - p["ts"] > CFG["pending_ttl"]]:
        state["pending"].pop(uid, None)
    for s in S.values():
        if s.get("busy") and now - s.get("last", 0) > CFG["idle_timeout"]:
            s["busy"] = False

    total = len(S)
    running = sum(1 for s in S.values() if s.get("busy"))
    waiters = [p for p in state["pending"].values()
               if now - p["ts"] >= CFG["approve_wait"]]
    waiting = len({p["sid"] for p in waiters})

    if waiters:
        tool = waiters[0].get("tool")
        msg = f"approve: {tool}" if tool else "needs you"
    elif running:
        msg = "working"
    else:
        msg = ""

    # Firmware shows "busy" only at running >= 3; optionally make a single
    # active session look busy too (waiting still takes priority on-device).
    wire_running = 3 if (CFG.get("busy_boost") and running >= 1) else running
    return {"total": total, "running": wire_running, "waiting": waiting, "msg": msg}


# ---------------------------------------------------------------------------
# BLE
# ---------------------------------------------------------------------------
_rx_accum = bytearray()


def on_notify(_sender, data):
    """Device → central. Acks today; permission replies once buttons land."""
    _rx_accum.extend(data)
    while b"\n" in _rx_accum:
        line, _, rest = _rx_accum.partition(b"\n")
        del _rx_accum[:]
        _rx_accum.extend(rest)
        text = line.decode("utf-8", "replace").strip()
        if text:
            log(f"dev→ {text[:120]}")


async def write_line(client, obj):
    data = (json.dumps(obj) + "\n").encode()
    # ATT payload caps at MTU-3; firmware reassembles until '\n', so chunk freely.
    for i in range(0, len(data), 180):
        await client.write_gatt_char(NUS_RX, data[i:i + 180], response=True)


def time_payload():
    off = -time.timezone
    if time.daylight and time.localtime().tm_isdst:
        off = -time.altzone
    return {"time": [int(time.time()), off]}


async def ble_loop(state):
    while not state["stop"]:
        try:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or "").startswith(CFG["name_prefix"]),
                timeout=8.0,
            )
            if not dev:
                log("no buddy advertising; retrying")
                await asyncio.sleep(3)
                continue

            log(f"found '{dev.name}' [{dev.address}], connecting")
            async with BleakClient(dev) as client:
                # Subscribing touches an encrypted characteristic, which makes
                # the OS bond (macOS shows the device's 6-digit passkey dialog
                # the first time; reconnects reuse the stored key).
                log("connected; subscribing (first time → macOS pairing prompt)")
                await client.start_notify(NUS_TX, on_notify)
                # Encrypted writes only land once LE encryption settles; anything
                # sent in the first instant is silently dropped (which left the
                # RTC unset → garbage clock). Settle, then resend below.
                await asyncio.sleep(0.5)
                if CFG["owner"]:
                    await write_line(client, {"cmd": "owner", "name": CFG["owner"]})
                log("link up; mirroring session state")

                conn_t = time.monotonic()
                last_sig = None
                last_keep = 0.0
                last_time = 0.0
                while client.is_connected and not state["stop"]:
                    now = time.monotonic()
                    # Time sync isn't acked, so resend it: fast for the first few
                    # seconds (heals a drop before encryption was up), then slow.
                    early = (now - conn_t) < 6.0
                    if now - last_time >= (1.0 if early else 30.0):
                        await write_line(client, time_payload())
                        last_time = now
                    async with state["lock"]:
                        snap = compute_snapshot(state)
                    sig = json.dumps(snap, sort_keys=True)
                    if sig != last_sig or (now - last_keep) >= CFG["keepalive"]:
                        await write_line(client, snap)
                        last_sig = sig
                        last_keep = now
                    await asyncio.sleep(0.5)
            log("disconnected")
        except Exception as e:  # noqa: BLE001 — keep the daemon alive
            log(f"ble error: {e!r}")
        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Hook-event socket server
# ---------------------------------------------------------------------------
async def handle_conn(reader, writer, state):
    try:
        data = await reader.read(1 << 16)
        for raw in data.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            async with state["lock"]:
                apply_event(state, ev)
            log(f"hook {ev.get('event')} sid={str(ev.get('session_id'))[:8]}")
    finally:
        writer.close()


async def serve(state):
    server = await asyncio.start_unix_server(
        lambda r, w: handle_conn(r, w, state), path=bc.SOCK_PATH
    )
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
_lock_fd = None  # held open for the process lifetime; released on exit/crash


def acquire_singleton():
    """Atomic single-instance guard. flock has no TOCTOU race, so concurrent
    hook-triggered spawns can't end up with two daemons fighting over BLE."""
    global _lock_fd
    _lock_fd = open(os.path.join(bc.BUDDY_DIR, "bridge.lock"), "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


async def main():
    bc.ensure_dir()
    if not acquire_singleton():
        log("another bridge holds the lock; exiting")
        return
    # We own the lock, so it's safe to clear a stale socket from a dead daemon.
    try:
        os.unlink(bc.SOCK_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass

    state = new_state()
    log(f"bridge starting (name_prefix='{CFG['name_prefix']}', sock={bc.SOCK_PATH})")
    try:
        await asyncio.gather(ble_loop(state), serve(state))
    finally:
        try:
            os.unlink(bc.SOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

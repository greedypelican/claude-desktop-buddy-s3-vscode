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
# Module-global handle so the BLE notify callback (which bleak calls without our
# state arg) can resolve pending approvals. Set once in main().
STATE = None


def new_state():
    return {
        "sessions": {},
        "pending": {},
        "approvals": {},       # uid -> asyncio.Future, resolved by the device button
        "active_prompt": None,  # {"id","tool","hint"} currently shown on the device
        "beep_queue": [],       # alert names to chime on the device: approve/question/complete
        "deferred_beep": None,  # (name, due_monotonic) — a beep to send after a delay
        "connected": False,
        "loop": None,
        "lock": asyncio.Lock(),
        "stop": False,
    }


def clear_pending(state, sid):
    for uid in [u for u, p in state["pending"].items() if p["sid"] == sid]:
        state["pending"].pop(uid, None)


def _clear_active_prompt(state, uid=None, sid=None):
    ap = state.get("active_prompt")
    if ap and ((uid is not None and ap.get("id") == uid)
               or (sid is not None and ap.get("sid") == sid)):
        state["active_prompt"] = None


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
        _clear_active_prompt(state, sid=sid)
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
                "kind": ev.get("kind", "approve"),
                "alert": bool(ev.get("alert")),  # alert mode → chime+screen when it waits
            }
    elif name == "PostToolUse":
        s = S.setdefault(sid, {})
        s["busy"] = True
        s["last"] = now
        uid = ev.get("uid")
        if uid:
            state["pending"].pop(uid, None)
        _clear_active_prompt(state, uid=uid)  # tool ran → drop the alert screen
    elif name == "Stop":
        # Turn finished — back to idle.
        s = S.setdefault(sid, {})
        s["busy"] = False
        s["last"] = now
        clear_pending(state, sid)
        _clear_active_prompt(state, sid=sid)
        if ev.get("beep"):
            # delay it like the others (0.3s) so all chimes are consistent
            state["deferred_beep"] = (ev["beep"], now + CFG["approve_wait"])  # e.g. "complete"
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
    # alert-mode prompt is non-blocking, so time it out if nothing clears it
    ap = state.get("active_prompt")
    if ap and ap.get("alert") and now - ap.get("ts", now) > CFG.get("approve_timeout", 300.0):
        state["active_prompt"] = None

    # release a deferred beep (e.g. "complete") once its delay has elapsed
    db = state.get("deferred_beep")
    if db and now >= db[1]:
        state["beep_queue"].append(db[0])
        state["deferred_beep"] = None
    for s in S.values():
        if s.get("busy") and now - s.get("last", 0) > CFG["idle_timeout"]:
            s["busy"] = False

    total = len(S)
    running = sum(1 for s in S.values() if s.get("busy"))
    waiters = [p for p in state["pending"].values()
               if now - p["ts"] >= CFG["approve_wait"]]
    waiting = len({p["sid"] for p in waiters})

    # A tool that's still pending after approve_wait is genuinely waiting (for
    # approval, or for a question answer) — chime once and show it on the device.
    # Auto-approved tools clear before this, so they stay silent.
    for uid, p in state["pending"].items():
        # Wait approve_wait before chiming: for approvals it tells a real prompt
        # from an auto-approved tool (cleared by PostToolUse); for questions it's
        # just a small delay (they never finish that fast).
        if p.get("alert") and not p.get("alerted") and now - p["ts"] >= CFG["approve_wait"]:
            p["alerted"] = True
            state["beep_queue"].append("question" if p.get("kind") == "question" else "approve")
            if not state.get("active_prompt"):
                state["active_prompt"] = {
                    "id": uid, "sid": p["sid"], "tool": p.get("tool", ""),
                    "hint": p.get("hint", ""), "ts": now, "alert": True,
                    "kind": p.get("kind", "approve"),
                }

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
    snap = {"total": total, "running": wire_running, "waiting": waiting, "msg": msg}

    # Button approval in flight → drive the device's approval screen.
    ap = state.get("active_prompt")
    if ap:
        snap["prompt"] = {"id": ap["id"], "tool": ap.get("tool", ""), "hint": ap.get("hint", "")}
        if ap.get("alert"):
            snap["prompt"]["alert"] = True  # device hides A/B; host decides
        if ap.get("kind"):
            snap["prompt"]["kind"] = ap["kind"]
        snap["waiting"] = max(snap["waiting"], 1)
        if not snap["msg"]:
            snap["msg"] = ("approve: " + ap.get("tool", "")).strip().rstrip(":")
    return snap


# ---------------------------------------------------------------------------
# BLE
# ---------------------------------------------------------------------------
_rx_accum = bytearray()


def _resolve_approval(uid, decision):
    """Device button answered — hand the decision to the waiting hook."""
    if not STATE or not uid:
        return
    loop = STATE.get("loop")
    fut = STATE["approvals"].get(uid)
    if fut is not None and loop is not None:
        loop.call_soon_threadsafe(
            lambda: fut.done() or fut.set_result(decision)
        )
    ap = STATE.get("active_prompt")
    if ap and ap.get("id") == uid:
        STATE["active_prompt"] = None


def fail_pending_approvals(state):
    """Link dropped — release any blocked hooks to the VS Code prompt."""
    for fut in list(state["approvals"].values()):
        if not fut.done():
            fut.set_result("ask")
    state["approvals"].clear()
    state["active_prompt"] = None


def on_notify(_sender, data):
    """Device → central. Permission replies (button presses) + acks."""
    _rx_accum.extend(data)
    while b"\n" in _rx_accum:
        line, _, rest = _rx_accum.partition(b"\n")
        del _rx_accum[:]
        _rx_accum.extend(rest)
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        log(f"dev→ {text[:120]}")
        try:
            msg = json.loads(text)
        except Exception:
            continue
        if msg.get("cmd") == "permission":
            # firmware: "once" = approve, "deny" = deny
            decision = "allow" if msg.get("decision") == "once" else "deny"
            _resolve_approval(msg.get("id"), decision)


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
                state["connected"] = True

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
                    # drain queued alert chimes (approve / question / complete)
                    if state["beep_queue"]:
                        async with state["lock"]:
                            beeps, state["beep_queue"] = state["beep_queue"], []
                        for name in beeps:
                            await write_line(client, {"cmd": "beep", "name": name})
                    await asyncio.sleep(0.1)   # snappy: chimes/snapshots within ~0.1s
            log("disconnected")
        except Exception as e:  # noqa: BLE001 — keep the daemon alive
            log(f"ble error: {e!r}")
        finally:
            state["connected"] = False
            fail_pending_approvals(state)
        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Hook-event socket server
# ---------------------------------------------------------------------------
async def _do_approval(state, ev):
    """Show the prompt on the device and wait for A/B. Returns allow|deny|ask."""
    uid = ev.get("uid")
    # Can't gate on the device right now → defer to the VS Code prompt.
    if not uid or not state.get("connected") or state.get("active_prompt"):
        return "ask"
    sid = ev.get("session_id") or "default"
    async with state["lock"]:
        s = state["sessions"].setdefault(sid, {})
        s["busy"] = True
        s["last"] = time.monotonic()
        state["active_prompt"] = {
            "id": uid, "sid": sid, "tool": ev.get("tool", ""), "hint": ev.get("hint", ""),
            "kind": "approve",
        }
        state["beep_queue"].append("approve")
    log(f"approve? uid={uid[:12]} tool={ev.get('tool')} → asking device")
    fut = state["loop"].create_future()
    state["approvals"][uid] = fut
    try:
        decision = await asyncio.wait_for(fut, timeout=CFG.get("approve_timeout", 30.0))
    except asyncio.TimeoutError:
        decision = "ask"
    finally:
        state["approvals"].pop(uid, None)
        ap = state.get("active_prompt")
        if ap and ap.get("id") == uid:
            state["active_prompt"] = None
    log(f"approve uid={uid[:12]} → {decision}")
    return decision


async def handle_conn(reader, writer, state):
    try:
        data = await reader.read(1 << 16)
        lines = [l.strip() for l in data.splitlines() if l.strip()]
        if not lines:
            return
        try:
            first = json.loads(lines[0])
        except Exception:
            first = None

        # Blocking approval request: answer on the same connection.
        if first and first.get("approve_req"):
            decision = await _do_approval(state, first)
            try:
                writer.write((json.dumps({"decision": decision}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
            return

        # Otherwise: fire-and-forget state events.
        for raw in lines:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            async with state["lock"]:
                apply_event(state, ev)
            log(f"hook {ev.get('event')} sid={str(ev.get('session_id'))[:8]}")
    finally:
        try:
            writer.close()
        except Exception:
            pass


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

    global STATE
    state = new_state()
    state["loop"] = asyncio.get_running_loop()
    STATE = state  # so the BLE notify callback can resolve approvals
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

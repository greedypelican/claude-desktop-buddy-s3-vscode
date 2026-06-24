# Bridge design notes (the "why")

Living design memory for `bridge/` — kept in the repo so it travels to every
clone. README.md is how to *use* it; this is why it is *built this way*.

## Problem

The buddy firmware is a BLE peripheral speaking the Hardware Buddy wire protocol
(Nordic UART Service + newline JSON; see `../REFERENCE.md`). The **desktop app**
(macOS/Windows) is the BLE *central* that feeds it session state. The **VS Code
Claude Code extension has no such bridge** — nothing plays central. So the buddy
goes dark when you work in the extension.

This bridge is that missing central + an event translator, driven by Claude Code
**hooks** instead of the desktop app's session manager. **The firmware is not
modified.**

```
Claude Code (VS Code)
   │  hooks (SessionStart / UserPromptSubmit / Pre|PostToolUse / Stop …)
   ▼
buddy_hook.py  (short-lived, stdlib only)
   │  unix socket  (~/.claude-buddy/buddy.sock)
   ▼
buddy_bridge.py  (long-lived daemon, venv + bleak = BLE central)
   │  Nordic UART JSON
   ▼
M5StickS3  (unchanged firmware)
```

Why two processes: hooks are short-lived (one per event) and can't hold a BLE
connection. The daemon is long-lived and owns the link; hooks just post events.

## Event findings — measured live inside the VS Code extension (2026-06-24)

Captured with `log_event.py` wired to every hook (see git history). Hooks load
at **session start and do NOT hot-reload** — you must restart Claude Code after
changing `.claude/settings.json`.

| Hook event       | Fires in extension? | Useful payload |
| ---------------- | ------------------- | -------------- |
| SessionStart     | ✅ (`startup` **and** `resume` both fire on a restart) | `source` |
| SessionEnd       | ✅                  | `reason` |
| UserPromptSubmit | ✅                  | `prompt`, `permission_mode` |
| PreToolUse       | ✅                  | `tool_name`, `tool_input`, `tool_use_id` |
| PostToolUse      | ✅                  | + `tool_response`, `duration_ms` |
| **Notification** | ❌ **never fires**  | — |
| Stop / SubagentStop | ❓ unconfirmed (turns kept getting new input, never cleanly stopped) | — |

The big one: **Notification does not fire in the extension.** Real approval
prompts appeared (the "Allow this bash command?" webview, 15s+ gaps) yet zero
Notification events — the extension renders its own permission UI and skips the
hook. So "waiting for approval" can't be read from Notification.

## How we detect attention without Notification

The **Pre→Post timing gap.** A `PreToolUse(tool_use_id)` with no matching
`PostToolUse(same id)` after `approve_wait` (1.5s) means the tool is blocked on
the approval webview → **attention**. `PostToolUse(id)` clears it → back to busy.
`tool_use_id` pairs Pre↔Post exactly. Bonus over Notification: we know the tool
name + command, so the device can show *what* is waiting.

Gotcha handled: a **denied** tool never emits PostToolUse, so a pending entry is
also cleared on the session's next prompt/Stop and force-expired after
`pending_ttl` (180s) — otherwise attention would stick forever.

## State mapping (display-only MVP)

```
SessionStart                              → idle    (connected)
UserPromptSubmit / Pre / PostToolUse      → busy    (stays busy for the WHOLE turn)
SubagentStop                              → busy    (subagent done, main turn continues)
PreToolUse without PostToolUse > 1.5s     → attention (waiting > 0, LED blinks)
Stop                                      → idle    (turn ended — the normal idle trigger)
no events > idle_timeout (600s/10min)     → idle    (SAFETY NET only, if a Stop is missed)
SessionEnd (last session gone)            → sleep   (total == 0)
```

A turn is busy from UserPromptSubmit until Stop. Crucially the idle_timeout is a
long safety net, NOT the normal idle trigger — Claude can generate text / think
for tens of seconds with no hook event, so a short timeout (we had 12s) drops the
buddy to idle mid-work. Stop fires reliably, so it ends the turn promptly; the
600s (10 min) timeout only matters if a Stop is somehow missed.

Known limitation: a long *auto-approved* tool (e.g. a 20s build) also opens a
Pre→Post gap, so it reads as attention, not busy — indistinguishable from a real
approval wait without the Notification hook. Raise `approve_wait` if that flicker
bothers you (at the cost of slower real-approval detection).

Emitted as the heartbeat snapshot `{total, running, waiting, msg}`.

## Firmware display quirks (measured against the real device, `../src/main.cpp`)

`derive()` (main.cpp ~480) is blunter than the README's "seven states" implies:

```c
if (!connected)            return P_IDLE;      // not P_SLEEP
if (sessionsWaiting > 0)   return P_ATTENTION; // ← our attention works
if (recentlyCompleted)     return P_CELEBRATE;
if (sessionsRunning >= 3)  return P_BUSY;       // ← THREE+, not >0
return P_IDLE;
```

- **busy needs `running >= 3`.** A single Claude Code session (running=1) shows
  as **idle**, same as the desktop app. Optional `busy_boost` in config reports
  running=3 whenever ≥1 session generates, so the buddy looks awake while you
  work (waiting/attention still wins on-device).
- **The "zzz" sleep look is the idle/resting ambient**, not a bug and not a
  disconnect (disconnect → P_IDLE, never P_SLEEP). On the clock screen the device
  cycles idle/sleep/etc. by **time of day** (main.cpp ~1175: 1–7am & late night &
  weekends → mostly P_SLEEP). So if it sleeps at the wrong time, suspect the RTC —
  we send `{"time":[epoch, tz_offset_sec]}` on connect (parsed in data.h ~77).
- Snapshot keys the firmware actually reads: `total`, `running`, `waiting`, `msg`,
  `time`, plus the `cmd`/owner/char-push set. Others are ignored.

## BLE security (from `../src/ble_bridge.cpp`)

The firmware requires **LE Secure Connections + MITM passkey bonding**; every
characteristic is encrypted-only (`ESP_LE_AUTH_REQ_SC_MITM_BOND`, DisplayOnly).
So the central MUST bond. On macOS, CoreBluetooth (via bleak) triggers OS pairing
the first time we touch an encrypted characteristic — `start_notify` on TX — and
the OS shows a dialog for the 6-digit passkey displayed on the stick. The bond is
stored by the OS keyed to the device, so it's shared with the desktop app and
reconnects don't re-prompt. MTU negotiates ~185 on macOS; we chunk writes to 180.

One central at a time: disconnect the desktop Hardware Buddy before using this.

## Portability decisions (runs/install on any machine)

- No hardcoded paths. Scripts resolve themselves from `__file__`; project hooks
  use `$CLAUDE_PROJECT_DIR`; `wire_hooks.py user` computes an absolute path at
  install time on each machine.
- Runtime state in `~/.claude-buddy/` (socket, log, optional `config.json`) —
  per-user, deterministic, short enough for an AF_UNIX path on macOS.
- Hook client is stdlib-only (no venv needed to *report* events); only the daemon
  needs bleak, in `bridge/.venv`. The hook auto-spawns the daemon from that venv.
- Targets macOS + Linux (AF_UNIX). Windows is future work (different IPC + pairing).

## Button approval (A=approve / B=deny on the device)

Implemented and verified end-to-end in the VS Code extension (2026-06-24).
Opt-in via config `button_approval: true`; off → display-only.

First confirmed the feasibility: a PreToolUse hook that prints
`hookSpecificOutput.permissionDecision: "deny"` really does block the tool in the
extension — so this works despite the extension rendering its own permission
webview.

Flow for a gated PreToolUse:
1. `buddy_hook.py` opens the socket, sends `{…,"approve_req":true}`, and BLOCKS.
2. The daemon sets `active_prompt`, so the next snapshot carries
   `prompt:{id,tool,hint}` → the firmware shows its approval screen.
3. A → `{"cmd":"permission","id":…,"decision":"once"}`, B → `"deny"` (over BLE;
   the firmware already does this, main.cpp ~1096/1129).
4. `on_notify` resolves the waiting future; the daemon replies `{"decision":…}`.
5. The hook prints `permissionDecision` allow/deny; the extension honors it.

Never deadlocks: device disconnected / another prompt already in flight / no
press within `approve_timeout` (300s = 5 min) → daemon replies `"ask"`, the hook
prints nothing, and the normal VS Code prompt takes over. Only
`button_approval_tools` (default Bash/Write/Edit/MultiEdit/NotebookEdit) are
gated; other tools pass through. One prompt at a time (the firmware shows one).

Gotcha: Claude Code kills hooks at **60s by default**, which would cut the wait
short — so the PreToolUse hook's `timeout` is set to **360s** in settings.json
(and by `wire_hooks.py`). It must stay > `approve_timeout`. The hook's own socket
read timeout is `approve_timeout + 5`.

Trade-off: with it on, EVERY gated tool waits for a button press — that's the
full "pet approves your work" experience, but it's why it's opt-in.

## File map

- `buddy_common.py` — paths + config (stdlib; imported by both sides)
- `buddy_bridge.py` — the daemon (bleak central, state machine, snapshots)
- `buddy_hook.py`   — hook client (stdlib; posts events, spawns daemon)
- `wire_hooks.py`   — install/remove hooks in project or user settings.json
- `install.sh`      — venv + bleak + runtime dir
- `log_event.py`    — diagnostic-only event logger (not wired by default)

# claude-desktop-buddy-s3-vscode

> A desk buddy that reacts to **Claude Code in the VS Code extension** — not
> just the desktop app. An M5StickC Plus S3 (ESP32-S3) shows your session
> activity and lights up when a permission prompt is waiting.

The official [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
only talks to the buddy from the **desktop apps** (macOS/Windows), which play
the BLE central role. The VS Code extension has no such bridge. This fork adds
a small **host-side bridge** (`bridge/`) that plays that role itself, driven by
Claude Code **hooks** — so the buddy works while you code in VS Code. The
firmware is unchanged except for a software-clock fix (see below).

Built on [openalchemy/claude-desktop-buddy-s3](https://github.com/openalchemy/claude-desktop-buddy-s3)
(the ESP32-S3 port). The wire protocol is documented in [REFERENCE.md](REFERENCE.md).

<p align="center">
  <img src="docs/s3/approval.jpg" alt="Approval prompt on M5StickS3" width="220">
  <img src="docs/s3/pet-stats.jpg" alt="Pet stats page" width="220">
  <img src="docs/s3/credits.jpg" alt="Credits page" width="220">
</p>

## What you need

- **M5StickC Plus S3** (ESP32-S3)
- **macOS** (Linux works too with extra pairing setup — see [bridge/NOTES.md](bridge/NOTES.md); Windows is not supported yet)
- **[PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/)** — to flash the firmware
- **Python 3** — for the bridge
- **Claude Code in the VS Code extension**

## Quick start

Do these once, in order. Takes a few minutes.

### 1. Flash the firmware

Plug the stick into USB, then from the repo root:

```bash
pio run -t upload
```

> **First flash on a fresh S3:** the S3 has no boot button. If upload can't find
> the device, keep USB plugged in and **long-press the side power button until
> the green LED flashes** (3–5 s) to enter download mode, then re-run. After the
> first flash, later uploads reset into download mode on their own.

This also includes the software-clock fix (this board has no usable hardware
RTC, so the clock comes from the bridge's time sync).

### 2. Set up the bridge

```bash
cd bridge
./install.sh
```

Creates a local Python venv, installs `bleak`, and prepares `~/.claude-buddy/`.
The hooks are already wired in [`.claude/settings.json`](.claude/settings.json)
(portable — they use `$CLAUDE_PROJECT_DIR`).

### 3. Restart Claude Code

Restart the VS Code window / Claude Code session so it loads the hooks. Accept
the hook-trust prompt if it appears. (Hooks only load at session start.)

### 4. Connect & pair

- Wake the stick (any button) and make sure the **desktop app's Hardware Buddy
  is disconnected** — BLE allows only one central at a time.
- The bridge connects automatically. On the **first** connect, macOS shows a
  pairing dialog — type the **6-digit passkey shown on the stick**. Reconnects
  reuse the stored key, no prompt.

For a clean first pairing (and to grant Bluetooth permission), it helps to run
the bridge in the foreground once from a terminal:

```bash
cd bridge && ./.venv/bin/python buddy_bridge.py --foreground
```

You should see `found 'Claude-…' → connected → link up; mirroring session state`.
After that it auto-starts in the background whenever you use Claude Code.

### 5. Use it

Work in Claude Code as usual. The buddy follows along.

## What you'll see

| Buddy state              | When                                            |
| ------------------------ | ----------------------------------------------- |
| **idle** (blink / "zzz") | connected, nothing urgent                       |
| **busy** (working)       | a session is generating                         |
| **attention** (LED, `!`) | a tool is waiting for your approval             |

Approval is detected from the gap between a tool starting and finishing —
because the VS Code extension does **not** fire the `Notification` hook. By
default you still approve in VS Code and the buddy is just the visual alert; turn
on **[Button approval](#button-approval-optional)** to approve/deny right on the
device.

## Configure

Settings live in **`~/.claude-buddy/config.json`** (any missing key uses its
default below). The daemon reads it at **startup**, so after editing, restart it:
`pkill -f buddy_bridge.py` then relaunch (or just start a new Claude Code session
— the hook respawns it). Example:

```json
{
  "owner": "Felix",
  "busy_boost": true,
  "button_approval": "alert",
  "approve_wait": 0
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `owner` | `""` | Name shown on the device (empty = keep the device's stored name). |
| `name_prefix` | `"Claude"` | BLE advertised-name prefix to scan for. |
| `busy_boost` | `true` | Show a single active session as **busy** (the firmware's own "busy" needs 3+ concurrent sessions). `false` = desktop-app-faithful. |
| `button_approval` | `"alert"` | `false` = display-only; `"alert"` = device chimes + shows the prompt, you decide in VS Code; `true` = device A/B decides. See [Button approval](#button-approval-optional). |
| `button_approval_tools` | `["Bash","Write","Edit","MultiEdit","NotebookEdit"]` | Which tools trigger an approval alert/gate. |
| `approve_wait` | `0` | Seconds to wait before an **approve** chime, to tell a genuine approval from an auto-approved tool. `0` = chime instantly on every gated tool (more false chimes); raise (e.g. `0.5`) to chime only when a tool actually waits. Questions always chime instantly. |
| `approve_timeout` | `300` | `true` mode only: seconds to wait for an A/B press before falling back to the VS Code prompt. |
| `idle_timeout` | `600` | Safety net: drop a stuck "busy" back to idle after this many quiet seconds (Stop normally handles it). |

Env vars `CLAUDE_BUDDY_OWNER` / `CLAUDE_BUDDY_NAME` override `owner` / `name_prefix`.

**Alert sounds** (musical note, beats-per-minute, repeats, volume) are set per
type — `approve` / `question` / `complete` — in `buddyRequestBeep()` in
[src/main.cpp](src/main.cpp). Editing those needs a firmware reflash.

**Restart the bridge** after changing config (`pkill -f buddy_bridge.py`, or
Ctrl+C the foreground run and re-launch).

## Button approval (optional)

Set by `button_approval` in `~/.claude-buddy/config.json`:

- **`"alert"`** (default) — when a tool needs approval (or you're asked a
  question) the buddy **chimes** and shows the prompt, but **you decide in VS
  Code** (both appear together). The device shows `→ approve in editor` and its
  A/B buttons do nothing. Non-blocking.
- **`true`** — the device **decides**: A = approve, B = deny, returned to Claude
  Code. VS Code shows no prompt in this mode. Blocks until you press (or it falls
  back, see below).
- **`false`** — display-only; the device just mirrors state, no chimes.

Only tools in `button_approval_tools` are involved (default
`["Bash","Write","Edit","MultiEdit","NotebookEdit"]`; reads etc. pass through).
With the default `approve_wait` of `0`, a gated tool chimes the **approve** sound
immediately — even ones VS Code auto-approves; raise `approve_wait` (e.g. `0.5`)
to chime only when a tool genuinely waits. Questions always chime instantly.
Switching `button_approval` takes effect on the next tool call; `approve_wait`
and the other daemon settings need a daemon restart.

For **`true`** mode specifically: if the device is disconnected/busy or you don't
press within `approve_timeout` (default 300 = 5 min), it falls back to the normal
VS Code prompt — it never hangs. If you raise `approve_timeout` past ~355, also
bump the PreToolUse hook `timeout` in settings.json (Claude Code kills hooks at
60s by default; ours is set to 360).

## Use it in every project

The committed hooks only fire while Claude Code's project is *this* repo. To
drive the buddy from all your Claude Code work on this machine:

```bash
cd bridge
python3 wire_hooks.py user          # merge hooks into ~/.claude/settings.json
python3 wire_hooks.py user --remove   # undo
```

## Troubleshooting

```bash
tail -f ~/.claude-buddy/bridge.log               # what the bridge is doing
cd bridge && ./.venv/bin/python buddy_bridge.py --foreground   # see pairing/logs live
pkill -f buddy_bridge.py                          # stop the background daemon
```

- **Buddy stays asleep / never connects** — the desktop Hardware Buddy is
  probably still holding the BLE link; disconnect it. Confirm the stick is awake
  and Bluetooth is on. Re-pair if you ever clicked "Forget".
- **Nothing in the log** — hooks didn't load: restart Claude Code, accept the
  trust prompt, and confirm `bridge/.venv` exists (`./install.sh`).
- **No pairing dialog on macOS** — run the foreground command above once from a
  Terminal, and grant Bluetooth permission in System Settings → Privacy &
  Security → Bluetooth.
- **Clock shows garbage** — flash this firmware (step 1) and let the bridge
  connect; the software clock sets within a second or two.

## How it works

Hooks → unix socket → daemon → BLE. The full design, the measured hook-event
findings, the BLE security/pairing details, and the firmware display quirks are
in **[bridge/NOTES.md](bridge/NOTES.md)**.

```
bridge/
  buddy_bridge.py — daemon: BLE central + state machine + heartbeat snapshots
  buddy_hook.py   — Claude Code hook client (stdlib; posts events, spawns daemon)
  buddy_common.py — paths + config
  wire_hooks.py   — install/remove hooks in project or user settings.json
  install.sh      — venv + bleak
  README.md       — points here
  NOTES.md        — design memory (the "why")
```

---

## Firmware reference

### What changed for the S3 (vs the original M5StickC Plus)

- `platformio.ini` targets `esp32-s3-devkitc-1` with `M5Unified`
- `src/m5_compat.h` maps the old `M5.Axp.*`/`M5.Beep.*`/`M5.Imu.*`/`M5.Rtc.*` APIs onto M5Unified
- RTC struct fields are lowercase (`.hours`, `.month`, `.weekDay`); power button via `M5.BtnPWR.wasClicked()`; LED on **G19**
- BLE is the only live data channel on S3 (USB CDC `Serial.available()` can deadlock the data poll)
- **Software clock**: this board's `M5.Rtc` never holds valid time, so the clock is kept in software from the bridge's `{"time":…}` sync

Wipe a previously-flashed device first if needed: `pio run -t erase && pio run -t upload`. You can also factory-reset from the device: **hold A → settings → reset → factory reset → tap twice**.

### Controls

|                         | Normal               | Pet         | Info        | Approval    |
| ----------------------- | -------------------- | ----------- | ----------- | ----------- |
| **A** (front)           | next screen          | next screen | next screen | **approve** |
| **B** (right)           | scroll transcript    | next page   | next page   | **deny**    |
| **Hold A**              | menu                 | menu        | menu        | menu        |
| **Power** (left, short) | toggle screen off    |             |             |             |
| **Power** (left, ~6s)   | hard power off       |             |             |             |
| **Shake**               | dizzy                |             |             | —           |
| **Face-down**           | nap (energy refills) |             |             |             |

The screen auto-powers-off after 30 s of no interaction (kept on during an approval). Any button press wakes it.

> Note: the **approve / deny** columns drive the real permission decision in the
> VS Code extension only when [Button approval](#button-approval-optional) is
> enabled. With it off (default), the bridge is display-only.

### Pets

Eighteen ASCII pets, each with seven animations (sleep, idle, busy, attention, celebrate, dizzy, heart); menu → "next pet" cycles them. For custom GIF characters, see the character-pack format in [REFERENCE.md](REFERENCE.md) and the example in `characters/bufo/`. `tools/flash_character.py characters/bufo` installs one over USB without the BLE round-trip.

## Building your own device

You don't need this code — see **[REFERENCE.md](REFERENCE.md)** for the wire
protocol: Nordic UART Service UUIDs, JSON schemas, and the folder-push transport.

## Note: no Claude desktop app needed

This fork connects to the buddy **directly over BLE**, so you do **not** need the
Claude desktop app or its "Hardware Buddy" developer feature — the bridge in
`bridge/` plays that role, driven by Claude Code in VS Code. (The desktop app is
a separate, alternative way to drive the buddy; if you run both, disconnect its
Hardware Buddy first — only one BLE central can connect at a time.) The buddy BLE
API is an unofficial maker feature, not an officially supported product.

## Credits

- Original: [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
- ESP32-S3 port: [openalchemy/claude-desktop-buddy-s3](https://github.com/openalchemy/claude-desktop-buddy-s3)

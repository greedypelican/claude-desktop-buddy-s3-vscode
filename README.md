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
because the VS Code extension does **not** fire the `Notification` hook. You
still approve in VS Code; the buddy is the visual alert. (Device-button approval
is planned — see [bridge/NOTES.md](bridge/NOTES.md).)

## Configure

Optional `~/.claude-buddy/config.json`:

```json
{ "owner": "Felix", "busy_boost": true }
```

- `owner` — name shown on the device.
- `busy_boost` — the firmware only shows the "busy" animation at 3+ concurrent
  sessions; set `true` so a single active session looks busy too.
- Others: `name_prefix`, `idle_timeout`, `approve_wait` (see
  [bridge/buddy_common.py](bridge/buddy_common.py)). Env `CLAUDE_BUDDY_OWNER` /
  `CLAUDE_BUDDY_NAME` override.

**Restart the bridge** after changing config (`pkill -f buddy_bridge.py`, or
Ctrl+C the foreground run and re-launch).

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

> Note: A/B approve/deny act on the **device's own** prompt flow. Returning that
> decision to the *VS Code extension* is the planned button-approval feature and
> is not wired yet — today the bridge is display-only.

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

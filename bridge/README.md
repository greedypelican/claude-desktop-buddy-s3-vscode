# Claude Code → buddy bridge

Drive the M5StickS3 buddy from the **Claude Code VS Code extension** (and CLI),
not just the macOS/Windows desktop app. The desktop app is the BLE central that
feeds the device; the extension has no equivalent, so this bridge plays central
and translates Claude Code **hook** events into the Hardware Buddy wire protocol.

The device shows **sleep → idle → busy → attention** as you work. **The firmware
is unchanged** — this is pure host-side glue. Today it is *display-only* (it
mirrors state; you still approve in VS Code). Button approval is planned — see
[NOTES.md](NOTES.md).

## Install

```bash
cd bridge
./install.sh
```

That creates `bridge/.venv`, installs `bleak`, and makes `~/.claude-buddy/`.
The project hooks are already committed in [`../.claude/settings.json`](../.claude/settings.json)
(portable via `$CLAUDE_PROJECT_DIR`), so then:

1. **Restart Claude Code** (VS Code) so the hooks load and are trusted.
2. Wake the stick; make sure the desktop app's **Hardware Buddy is disconnected**
   (BLE allows one central at a time).
3. On first connect, macOS shows a **pairing dialog** — type the **6-digit
   passkey shown on the stick**. Reconnects reuse the stored key.

Work in Claude Code as usual; the buddy follows along.

## Use it in every project (not just this repo)

The committed hooks only fire while Claude Code's project is *this* repo. To make
the buddy react to all your Claude Code work on this machine:

```bash
python3 wire_hooks.py user        # merge hooks into ~/.claude/settings.json
python3 wire_hooks.py user --remove   # undo
```

## Configure

Optional `~/.claude-buddy/config.json`:

```json
{ "owner": "Felix", "name_prefix": "Claude", "idle_timeout": 12, "approve_wait": 1.5 }
```

`owner` sets the name shown on the device. `name_prefix` is the BLE advertised-name
filter. Env `CLAUDE_BUDDY_OWNER` / `CLAUDE_BUDDY_NAME` override these.

## Troubleshooting

```bash
tail -f ~/.claude-buddy/bridge.log          # what the bridge is doing
./.venv/bin/python buddy_bridge.py --foreground   # run in front, see pairing/logs live
```

- **Device stays asleep** → bridge can't connect. Check the desktop app isn't
  holding the BLE link; confirm the stick is awake and Bluetooth is on; re-pair
  if you ever clicked "Forget".
- **Nothing in the log** → hooks didn't load. Restart Claude Code; confirm the
  hook-trust prompt was accepted; verify `bridge/.venv` exists (`./install.sh`).
- **No pairing dialog / can't connect on macOS** → grant Bluetooth permission to
  the process; running `--foreground` from a Terminal once makes the prompt
  appear reliably.

## How it works

Short version: hooks → unix socket → daemon → BLE. Full design, the measured
hook-event findings, the attention heuristic, and the button-approval plan are in
[NOTES.md](NOTES.md).

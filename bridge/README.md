# bridge/ — Claude Code → buddy BLE bridge

Setup, usage, configuration, and troubleshooting live in the repo's main
**[../README.md](../README.md)** (install from flashing to running, in order).

The design — measured hook-event findings, the attention heuristic, BLE
security/pairing, firmware display quirks, and the button-approval plan — is in
**[NOTES.md](NOTES.md)**.

Files: `buddy_bridge.py` (daemon), `buddy_hook.py` (hook client),
`buddy_common.py` (paths/config), `wire_hooks.py` (install/remove hooks),
`install.sh`, `log_event.py` (diagnostic only).

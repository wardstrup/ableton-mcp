# Install and rollback for script 2.0.0

Nothing in this round is copied onto the Mac by the agent. Install only after Joe agrees. `1.9.0` is the rollback file: it is the script this branch had at `7f87fbfa` before 2.0.0.

The `uv` installer failed on this Mac with a PyPI certificate error. Use a plain file copy. Do not use `uvx` or `ableton-mcp-install-script` for this update.

A full Live quit is required. Re-selecting the control surface is not enough for this reload: Live keeps the previous Remote Script in memory until the application exits.

## Base

| Item | Value |
|---|---|
| Original install | `9dddc7bd5b95412510fdeb745949e3bf23f3fd8a`, script `1.7.1` |
| Live-tested scripts | `1.8.0`, then `1.8.1` on Live 12.4.16 |
| Previous script on this branch | `1.9.0` at `7f87fbfa` |
| This script | `2.0.0` on `cursor/recording-workflow-7ab1` |
| Port | `127.0.0.1:9877` (unchanged) |

The MCP client must run this checkout. `uvx ableton-mcp` from PyPI will not know these commands and will expect script `1.7.1`.

Example, only after Joe agrees, from the fork clone under `~/Grok/Git`:

```text
uv run --directory ~/Grok/Git/ableton-mcp ableton-mcp
```

Use the real clone path if it is not that folder.

## Backup, before any copy

Live's usual User Library script on macOS:

```text
~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/__init__.py
```

If the User Library was moved, use the location in Live's Preferences, Library.

```bash
cp ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py.before-2.0.0
```

Keep that file. It is the Live copy of whatever is installed now (expected to be the 1.9.0 script if that was the last copy). The repo file to roll back to, if the User Library copy is not 1.9.0, is `AbletonMCP_Remote_Script/__init__.py` at commit `7f87fbfa`.

## Install by copy

Quit Live completely first (Live, not just the set).

From the fork checkout:

```bash
cp ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py.before-2.0.0
cp /path/to/ableton-mcp/AbletonMCP_Remote_Script/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py
```

The file in the repo is the Remote Script. The bundled `AbletonMCP_init.py` has the same bytes and must still be named `__init__.py` in the User Library.

Start Live. `get_script_info` should report `2.0.0`.

Point the MCP client at this checkout, not at PyPI. The server and the script both expect `2.0.0`.

The voice trigger is a separate install. It is not part of the Live script copy. Steps, the Loopback device, and the word list are in [voice_trigger/README.md](../voice_trigger/README.md).

## Rollback

1. Quit Live completely.
2. Copy `__init__.py.before-2.0.0` back to `__init__.py`. That restores the script that was installed before this copy.
3. Point the MCP client at a checkout that expects that script version. 2.0.0 expects script 2.0.0. The 1.9.0 server expects 1.9.0.
4. Start Live.

Recorded clips, arrangement audio, and marker edits already made are ordinary Live undo history. Replacing the script does not delete them.

To stop the voice trigger, quit the process. The LaunchAgent plist is not loaded by this repo. If it was loaded by hand, `launchctl unload ~/Library/LaunchAgents/com.grok.music-voice-trigger.plist`.

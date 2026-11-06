# Install and rollback for script 1.8.0

Do not do this while Joe is using Live. Copying the Remote Script and reloading the control surface needs his go-ahead. This file is instructions only. Nothing in this change was copied to a User Library or sent to port 9877.

## Base

| Item | Value |
|---|---|
| Installed commit | `9dddc7bd5b95412510fdeb745949e3bf23f3fd8a` |
| Installed script | `1.7.1` |
| This branch | `cursor/recording-workflow-7ab1` |
| This script | `1.8.0` |
| Port | `127.0.0.1:9877` (unchanged) |

The fork checkout and the reported install are the same commit. `1.8.0` is the child of that commit, not a different upstream.

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

If the User Library was moved, use the location in Live's Preferences, Library. Copy the current file aside before replacing it:

```bash
cp ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py.before-1.8.0
```

The installer, `ableton-mcp-install-script`, also writes `__init__.py.bak` when the destination differs. Keep the dated copy anyway. Quit Live before the copy, or accept that Live will keep the old script in memory until restart.

## Install, only with Joe's go-ahead

From this checkout:

```bash
uv run --directory ~/Grok/Git/ableton-mcp ableton-mcp-install-script --list-targets
uv run --directory ~/Grok/Git/ableton-mcp ableton-mcp-install-script
```

Then start Live, or re-select AbletonMCP under Settings, Link, Tempo and MIDI. `get_remote_script_info` should report `1.8.0`.

## Rollback

1. Quit Live.
2. Copy `__init__.py.before-1.8.0` (or `__init__.py.bak`) back to `__init__.py`.
3. Point the MCP client back at commit `9dddc7b` or at the previous `uvx` package, so it expects script `1.7.1` again.
4. Start Live and re-select the control surface.

Recorded clips, arrangement audio, and marker edits already made are ordinary Live undo history. Rollback of the script does not delete them.

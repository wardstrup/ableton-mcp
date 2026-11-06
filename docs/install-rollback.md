# Install and rollback for script 1.9.0

`1.8.1` is in the User Library and was tested on Live 12.4.16. `1.9.0` is not installed. Copy it only when Joe wants this round loaded. Quit Live first, or re-select AbletonMCP after the copy.

The `uv` installer failed on this Mac with a PyPI certificate error. Use a plain file copy. Do not rely on `uvx` or `ableton-mcp-install-script` for this update.

## Base

| Item | Value |
|---|---|
| Original install | `9dddc7bd5b95412510fdeb745949e3bf23f3fd8a`, script `1.7.1` |
| Live-tested script | `1.8.1` on Live 12.4.16 |
| This script | `1.9.0` on `cursor/recording-workflow-7ab1` |
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

## Install by copy

From the fork checkout (repos live under `~/Grok/Git`; use the real path):

```bash
cp ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py.before-1.9.0
cp /path/to/ableton-mcp/AbletonMCP_Remote_Script/__init__.py \
   ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/__init__.py
```

The file in the repo is the Remote Script. It is not `AbletonMCP_init.py` unless you are copying the bundled package file, which has the same bytes and must still be named `__init__.py` in the User Library.

Restart Live, or re-select AbletonMCP under Settings, Link, Tempo and MIDI. `get_script_info` should report `1.9.0`.

Point the MCP client at this checkout, not at PyPI. The server and the script both expect `1.9.0`.

## Rollback

1. Quit Live.
2. To return to the Live-tested 1.8.1 file, copy `__init__.py.before-1.9.0` back to `__init__.py`.
3. Point the MCP client at the matching checkout. 1.9.0 expects script 1.9.0.
4. Start Live and re-select the control surface.

Recorded clips, arrangement audio, and marker edits already made are ordinary Live undo history. Rollback of the script does not delete them.

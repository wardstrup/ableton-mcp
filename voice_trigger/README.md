# Voice trigger

Offline speech on Joe's Mac. It sends the same TCP JSON the MCP server sends, straight to the Remote Script at `127.0.0.1:9877`:

```json
{"type": "take", "params": {"bars": 8, "loop": false, "count_in": true}}
```

The reply is `{"status", "result"}`. Each command opens its own connection, so `stop` is not stuck behind a take. A take returns as soon as Live accepts it.

This folder in the repo is the package. On the Mac it lives at `~/Grok/Music/voice-trigger/voice_trigger`. Nothing here is installed by the agent.

## Install

Python 3.12. From the parent of this package:

```bash
mkdir -p ~/Grok/Music/voice-trigger
cp -R /path/to/ableton-mcp/voice_trigger ~/Grok/Music/voice-trigger/voice_trigger
cd ~/Grok/Music/voice-trigger
python3.12 -m venv .venv
.venv/bin/pip install -r voice_trigger/requirements.txt
cp voice_trigger/config.example.json config.json
cp voice_trigger/words.example.json words.json
```

`requirements.txt` is `vosk`, `sounddevice`, and `numpy`.

Download the small English Vosk model and unzip it so `config.json` points at it (`vosk_model`, default `model`):

```text
https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
```

Edit `config.json`:

- `input_device`: the real microphone, or `null` for the system default. Recognition always uses this device.
- `tone_output_device`: where the short heard / ok / error / mode tones play. `null` skips tones.
- `loopback_output_device`: the Loopback virtual device name, after the setup below. `null` leaves the gate off and recognition still runs.
- `min_confidence` default `0.65`. `debounce_sec` default `0.45`.
- `words_file` and `log_file` are next to `config.json`.

Run, with Live already up and the 2.0.0 script loaded:

```bash
cd ~/Grok/Music/voice-trigger
.venv/bin/python -m voice_trigger --config /Users/USERNAME/Grok/Music/voice-trigger/config.json
```

The log is `voice-trigger.log`. Every recognized phrase and every action is appended there.

## Microphone permission

macOS will ask the first time, or set it by hand:

System Settings → Privacy & Security → Microphone → enable Terminal, or the python binary in `.venv/bin/python` if that is what is listed.

Without that permission the trigger hears silence. Loopback does not grant it.

## Loopback, once, by hand

Joe owns Rogue Amoeba Loopback. The call app must not use the hardware mic. If it does, PLAY mode cannot mute the call without also deafening the trigger.

1. Open Loopback. New Virtual Device. Name it `Grok Call Mic`.
2. Do not add the hardware microphone as a source on that device. A hardware source would keep feeding the call in PLAY mode.
3. The trigger writes to that device: the real mic in CALL mode, silence in PLAY mode. In Loopback, the virtual device is both an output the trigger writes and the input the call reads.
4. In the call app, set the microphone to `Grok Call Mic`. Leave the trigger's `input_device` on the real mic.
5. Put `Grok Call Mic` in `loopback_output_device`.

If that device is missing, the trigger logs it, writes nothing, and keeps recognizing from the real mic.

## Modes

PLAY is the start mode. Command words act. Everything else is ignored, including `bye bot`.

`Music Bot` switches to CALL. The gate starts passing the real mic to Loopback. Every word except `bye bot` is ignored, so talking to the assistant does not fire takes.

`bye bot` returns to PLAY and the gate writes silence again.

## Words

`words.json` hot-reloads when the file changes. No restart. `instruments` maps a spoken word to a Live track name. Music Bot can add names there.

```json
{"default_bars": 8, "instruments": {"keys": "Keys", "guitar": "Guitar"}}
```

Saying `keys` arms Keys and disarms the others. `keys and guitar` arms both. Monitoring becomes auto.

| Spoken | Remote Script |
|---|---|
| instrument name, or `X and Y` | `arm_tracks`, exclusive |
| `take` | `take`, default 8 bars, count-in on |
| `take four` / `take eight` | `take` with 4 or 8 bars |
| `loop` | `take` with `loop` true |
| `stop` | `stop_performance` (stop recording, delete the cut-off take, playback stays) |
| `again` | `again` |
| `keep` | `keep_last` (append ` KEEP` only) |
| `scrap` | `scrap` |
| `cancel` | `cancel` |
| `play` / `pause` | `play_transport` / `pause_transport` |
| `click on` / `click off` | `set_metronome` |
| `count on` / `count off` | `set_count_in`, and later takes send that count-in flag |
| `tempo` plus the number in words, 40 through 220 | `set_tempo` (`tempo one hundred twenty`) |
| `slower` / `faster` | `nudge_tempo` −5 / +5 |
| `mark` | `mark_playhead` |
| `session` / `arrangement` | `show_view` |
| `Music Bot` / `bye bot` | mode only, no Live command |

The recognizer is built with a Vosk grammar of those phrases, so words outside the list are not returned as text.

## LaunchAgent

`com.grok.music-voice-trigger.plist` is not loaded by this repo. launchd does not expand `~`. Replace `USERNAME`, copy it to `~/Library/LaunchAgents/`, then `launchctl load` that path. `RunAtLoad` is false until you change it.

## Tests

`tests/test_voice_trigger.py` uses a fake socket and a fake recognizer result. It does not import vosk or open a microphone.

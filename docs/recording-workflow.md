# Recording workflow commands

Remote Script `1.8.0` on top of installed base `9dddc7bd5b95412510fdeb745949e3bf23f3fd8a` (script `1.7.1`). These commands are in the Live Remote Script and the MCP server. They are not installed into Joe's Live, and they have not been run against a live set.

The socket stays on loopback. Nothing here waits for audio, crops a clip, or replaces a sample file.

## What each command is

| Command | Live member it touches | What it does not do |
|---|---|---|
| `set_track_arm` | `Track.arm` | Toggle. Will not arm a group, return, or main track. |
| `set_track_mute` / `set_track_solo` | `Track.mute`, `Track.solo` | Toggle. |
| `set_track_volume` / `set_track_panning` / `set_track_send` | `MixerDevice` parameters | dB conversion. Values outside that parameter's min/max are rejected. |
| `set_track_monitoring` | `Track.current_monitoring_state` | Anything but `in`, `auto`, `off` (written as 0, 1, 2). |
| `set_track_input_routing` | `Track.input_routing_type`, `Track.input_routing_channel` | Output routing. Unknown names are rejected. A bad channel restores the previous type. |
| `prepare_track` | The fields you send, in one main-thread turn | Recording. Omitted fields stay as they are. |
| `set_arrangement_record` | `Song.record_mode` | Play or stop. Refuses when an armed track has an arrangement clip under the playhead unless `allow_overwrite` is true. |
| `set_session_record` | `Song.session_record` | `Song.trigger_session_record`. Refuses when an armed track has a clip in the selected scene or a playing clip, unless `allow_overwrite` is true. |
| `set_arrangement_overdub` | `Song.arrangement_overdub` | Starting the transport. This is MIDI arrangement overdub. |
| `record_empty_slot` | `Track.arm` if requested, then `ClipSlot.fire` | Global session record. Occupied slots. Unarmed fire (that would hit the track stop button). |
| `stop_recording` | See scopes below | Deleting clips. `Song.stop_playing`. |
| `get_recording_state` / `get_track_mixer_state` | Reads | Writes. |
| `get_clip_region` / `set_clip_region` | `start_marker`, `end_marker`, `loop_start`, `loop_end`, `looping` | `Clip.crop`. Replacing `file_path`. |

`stop_recording` scopes:

- `arrangement` sets `record_mode` false. Playback continues.
- `session` sets `session_record` false. Playback continues.
- `slot` calls `ClipSlot.stop()` only when that slot is recording. The official ClipSlot text says `stop` stops session clips on that track. The recorded clip is kept. A slot that is only playing is left alone.
- `recording` turns both record buttons off and stops session clips only on tracks that are recording.

## Example calls

```json
{"type": "prepare_track", "params": {
  "track_index": 0,
  "name": "Vox",
  "arm": true,
  "monitoring": "auto",
  "input_routing_type": "Ext. In",
  "input_routing_channel": "1",
  "volume": 0.85,
  "panning": 0.0
}}
```

```json
{"type": "record_empty_slot", "params": {
  "track_index": 0,
  "clip_index": 0,
  "arm": true
}}
```

```json
{"type": "record_empty_slot", "params": {
  "track_index": 0,
  "clip_index": 0,
  "record_length": 8
}}
```

```json
{"type": "stop_recording", "params": {"scope": "slot", "track_index": 0, "clip_index": 0}}
```

```json
{"type": "set_clip_region", "params": {
  "track_index": 0,
  "clip_index": 0,
  "view": "session",
  "start_marker": 0.0,
  "end_marker": 8.0,
  "loop_start": 0.0,
  "loop_end": 8.0
}}
```

Volume `0.85` is Live's raw unity-ish parameter value, not 0 dB by name. Read `get_track_mixer_state` for the min and max on that track.

## Limits, stated from the docs

- Count-in is `Song.count_in_duration`, get/observe only in the Live 12.4.5 Max Live Object Model (0 none, 1 one bar, 2 two bars, 3 four bars). This script reports it and does not set it. `is_counting_in` is also only read.
- Fixed length is `ClipSlot.fire(record_length)` on that empty slot. The official ClipSlot page says a passed length records for that many beats and raises if the slot already owns a clip. There is no separate "record N bars on the arrangement" command.
- `Song.trigger_session_record` is not called. The same page says a second call stops recording and starts playback, so it is not an idempotent setter.
- The official Max text calls `Song.session_record` the Session Overdub button and says read-only `session_record_status` reflects the Session Record button. Non-official Live 12 Python stubs describe `session_record` as the session record state. `set_session_record` writes `Song.session_record`. Which on-screen button moves has to be checked in Live 12 Suite before relying on the name.
- A fired slot can wait for Live's launch quantization. The JSON reply means the call was accepted, not that samples have been captured.
- `Clip.start_time` and `Clip.end_time` are read-only and are not the trim markers. Trim without cropping is `start_marker` / `end_marker`.
- Marker units are beats for MIDI and warped audio, and seconds for unwarped audio.
- Group-track slots are not stopped or recorded by these commands, because Live's group slot stop would hit every child track.

## Checks that do not need Live

`tests/test_recording_commands.py` and `tests/test_recording_tools.py` use a fake song. They cover rejected indexes, types, ranges, routing names, occupied slots, and the case where a failed channel or a failed `fire` does not leave the earlier write behind.

## Checks that still need Live, with Joe's go-ahead

Do not copy this script into the User Library or reload the control surface while a set is open. When that is agreed:

1. Arm, monitoring integers, and an input routing name taken from `get_track_mixer_state` actually change the track.
2. `set_session_record` moves the Session Record button rather than only Session Overdub, on an empty selected scene, and refuses when that scene slot has a clip.
3. `record_empty_slot` on an armed empty slot records only that slot, and `record_length` ends the take.
4. `stop_recording` keeps the new clip and leaves transport running.
5. `set_clip_region` moves markers and does not crop the file.
6. Count-in still follows Live's own metronome setting.

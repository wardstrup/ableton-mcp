# Recording workflow commands

Remote Script `1.9.0`. `1.8.1` is what is installed and was tested on Live 12.4.16. `1.9.0` is not loaded yet.

The socket stays on loopback. Nothing here waits for a take to finish, crops a clip, or replaces a sample file. A reply is not a real-time acknowledgement. Observed round trips on that Mac were about 300–800 ms.

## Live 12 Suite results for 1.8.0

Passed there: `get_script_info` 1.8.0, arm, mute, solo, volume, pan, monitoring, input routing, `record_empty_slot` with `record_length` 4 (fire about 620 ms, clip length 4.0 kept), `stop_recording` slot, arrangement record, `session_record`, and `arrangement_overdub`.

`1.8.1`, tested on Live 12.4.16: `set_mixer_batch`, `quick_record`, marker loop adjust, and `quick_arrangement_record` / `stop` work.

`1.9.0` changes from that voice session:

- `disarm_after` no longer clears arm in the same call. A later tick disarms after `is_recording` goes false. The reply leaves the track armed.
- `stop_playback` and `quick_arrangement_stop` do the change on this tick and read the transport on the next tick. `playing` and `record_mode` are the requested state. `confirmed` is true only when that later tick already shows it. The same-tick stale true is not returned as the result.
- Fixed-length `fire` passes launch quantization none for that call only, and stops any other playing or recording slot on the track first. The song's quantization setting is not changed. That is what was cutting takes to 8 or 12 beats instead of 16, and cutting every other chained take to 2 bars: the previous clip relaunches at the end of `fire(record_length)`, and the global grid was eating the start or the end.
- `record_loop` / `stop_record_loop` chain N-bar takes on later main-thread ticks. Scenes are created when the track runs out of empty slots. The command returns immediately. Without `max_takes` the loop stops at 128 takes.
- `stop_record_loop` and `stop_take` default `discard_current` to true and delete only the clip that is recording at that moment. Finished takes stay. Other stop commands keep the clip unless `discard_current` is sent.
- `set_metronome` writes `Song.metronome`. `set_count_in` tries `count_in_duration` and, when Live rejects it, reports that the Live 12.4.5 object model lists it as read-only. `pre_roll_bars` on `quick_record` and `record_loop` starts the metronome and fires after N bars without blocking.
- `create_scene`, and `quick_record` `create_scene_if_full`.
- Arming in `quick_record`, `record_loop`, and `prepare_track` (when arm is set true) sets monitoring to auto unless `monitoring` is `keep` or another explicit value.
- `keep_take` (1-based `take`, as in "keep 3") appends ` KEEP` and tries a green color. `list_takes` lists clips with lengths.

`1.8.1` changes, already Live-tested:

- `set_clip_region` with only `start_marker` or `end_marker` clamps `loop_start` / `loop_end` inside the new markers. An explicit loop that does not fit is still rejected. `crop` is still never called.
- While `is_recording` is true, a length above 100000 (Live briefly reported about 63072000, which is sample frames rather than beats) is returned as `length: null` and `length_status: "recording"`. A settled beat length is returned as beats.
- `stop_playback` re-reads `is_playing` on the main thread and returns `pending: true` if Live has not settled.
- Discrete setters accept the same names as `prepare_track`: `volume` or `value`, `panning` or `pan` or `value`, `arm` or `armed`, `enabled` or `on`.
- `record_empty_slot` and `get_recording_state` add `warnings` when the track has no available audio input, or `is_playing` is true and `current_song_time` does not move across a short re-read. They still fire or return state. That matches a session with no audio input device, where Ext. In disappeared, the slot reported triggered, and no clip was created.

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
| `get_clip_region` / `set_clip_region` | `start_marker`, `end_marker`, `loop_start`, `loop_end`, `looping` | `Clip.crop`. Replacing `file_path`. Marker-only edits clamp the loop. |
| `quick_record` | arm if needed, first empty slot or `slot_index`, `ClipSlot.fire(beats)` | Launch quantization. Occupied slots. Does not wait for the take. |
| `quick_arrangement_record` | arm, optional seek, `record_mode`, `start_playing` | Quantization. Overlapping arrangement clips unless `allow_overwrite`. |
| `quick_arrangement_stop` | `stop_playing`, `record_mode` false, optional disarm | Deleting clips. |
| `set_mixer_batch` | volume, pan, mute, solo, arm, sends for many tracks | Partial writes. One bad item changes nothing. |

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
{"type": "quick_record", "params": {"track_index": 0, "bars": 1, "arm": true}}
```

```json
{"type": "set_track_volume", "params": {"track_index": 0, "volume": 0.85}}
```

```json
{"type": "set_mixer_batch", "params": {"tracks": [
  {"track_index": 0, "volume": 0.85, "pan": 0.0, "mute": false},
  {"track_index": 1, "arm": true}
]}}
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

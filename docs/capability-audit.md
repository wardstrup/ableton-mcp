# AbletonMCP capability survey

Date of this survey: 2026-11-06. No connection was opened to the Mac that is running Live. Nothing here was executed inside Live 12 Suite.

Machine-readable rows: [capability-audit.json](capability-audit.json).

## What was compared

1. Installed source at `9dddc7bd5b95412510fdeb745949e3bf23f3fd8a`, Remote Script `1.7.1`, loopback port 9877. Command evidence is `_process_command` in `AbletonMCP_Remote_Script/__init__.py` at that commit, and the `@mcp.tool` functions in `MCP_Server/server.py` at that commit.
2. The published Live Object Model. The index at <https://docs.cycling74.com/apiref/lom/> says it refers to Ableton Live **12.4.5**. That is a Max for Live reference. A member listed there is not automatically a Python Remote Script member, and neither is an installed command.
3. Branch `cursor/recording-workflow-7ab1`, script `1.8.0`, parent commit the same as the install. Mock tests only.
4. Tests versus Live. On 2026-11-06, `90` unit tests passed, including `tests/test_recording_commands.py` and `tests/test_recording_tools.py`. They stub Live. They do not prove a button moved.

Where a feature was not in the pages reviewed, the status is **not established from docs reviewed**. That is not a claim that Live cannot do it.

## Sources

| Id | Role | Where | Stamp |
|---|---|---|---|
| lom-12.4.5 | Official Max LOM | <https://docs.cycling74.com/apiref/lom/> and the class pages linked from the rows | Page text: Live 12.4.5. Retrieved 2026-11-06. |
| live-manual-12 | Official behavior, not an API list | <https://www.ableton.com/en/live-manual/12/recording-new-clips/> | Live 12 manual. Retrieved 2026-11-06. |
| lom-legacy-monitor | Older official Max LOM | <https://docs.cycling74.com/legacy/max7/vignettes/live_object_model> and the Max 5 page | Max 7 / older Live. Not the 12.4.5 index. |
| python-runtime-community | Not official | <https://midiremotescripts.structure-void.com/reference/live12/Live.Song.runtime/> | Community Python stubs. Cited only where they conflict with or add to the Max text. |

## Corrections to the earlier wish list

The parent list asked for arm, recording transport, empty-slot recording, input routing, monitoring, mixer, and clip markers. Those writes were absent from 1.7.1. Several of the surrounding assumptions do not hold.

- Arm, mute, solo, volume, and pan were already **read** in `get_track_info` (`_safe_arm` and the mixer fields at line 609 of the 1.7.1 script). The gap was setters, not visibility.
- `Song.session_record` is not a clean documented name for the Session Record button. The 12.4.5 Song page says it is the **Session Overdub** button. The same page says read-only `session_record_status` reflects the **Session Record** button. A non-official Python stub says the opposite about `session_record`. The branch command `set_session_record` writes `Song.session_record`. Which on-screen button moves is **not established** until someone looks at Live 12 Suite.
- `Song.overdub` and `Song.arrangement_overdub` are both described as MIDI Arrangement Overdub on the Max page. The community stub calls `overdub` a legacy alias of session record. The branch sets only `arrangement_overdub` and does not set `overdub`.
- Count-in (`Song.count_in_duration`) is get/observe only: 0 none, 1 one bar, 2 two bars, 3 four bars. A setter was wished for. The documented API does not have one. The branch only reports it.
- Fixed length is `ClipSlot.fire(record_length)` on **one empty slot**, not a global "record N bars" operation. `Song.trigger_session_record` targets an implicit slot and a second call stops recording and starts playback. The branch does not call it.
- `Clip.start_time` and `Clip.end_time` are read-only. They are not the trim markers. Trim without crop is `start_marker` / `end_marker`. Arrangement `start_time` is the clip's position in the arrangement, and it is not a move setter.
- Clip fades, track reorder, and creating a group were **not** in the Song, Track, or Clip pages reviewed. They are not "unsupported"; they are not established here.
- Browser search does not return Sound Character tags. See the probe note below. Loading a plug-in is not searching presets inside it.
- Nothing in this stack is real-time. The reply means the main-thread call returned. Launch quantization can still delay the punch.

## Installed 1.7.1 commands

Remote Script dispatch at that commit, with an MCP wrapper unless noted.

Reads: `get_script_info`, `get_session_info`, `get_track_info`, `get_clip_notes`, `get_device_parameters`, `get_session_snapshot`, `get_arrangement_clips`, `get_browser_tree`, `get_browser_items_at_path`, `get_browser_item`, `get_browser_categories`, `get_browser_items`, `drain_passive_events`.

Writes and transport: `create_midi_track`, `create_audio_track`, `set_track_name`, `create_clip`, `create_audio_clip`, `add_notes_to_clip`, `clear_notes_from_clip`, `set_clip_name`, `set_arrangement_clip_name`, `delete_clip`, `set_tempo`, `fire_clip`, `stop_clip`, `start_playback`, `stop_playback`, `load_instrument_or_effect`, `load_browser_item` (script only), `load_drum_kit` (wrapper), `set_device_parameter`, `switch_to_arrangement_view`, `set_current_song_time` (wrapper name `set_arrangement_time`), `duplicate_session_clip_to_arrangement` (wrapper name `duplicate_to_arrangement`), `create_locator`, `map_rack_magnitude`, `inspect_rack`.

Dataset-only wrappers (`submit_intent`, `rate_last_action`, `prefer_candidate`, `reject_last_action`, `record_audition`, `set_dataset_consent`) do not change the Live set.

`SCRIPT_CAPABILITIES` at 1.7.1 is a shorter list than the dispatcher. Handshake checks must not be treated as the full command list.

## Branch 1.8.0, not installed

New commands, all posted with `schedule_message` before any Live call: `get_recording_state`, `get_track_mixer_state`, `set_track_arm`, `set_track_mute`, `set_track_solo`, `set_track_volume`, `set_track_panning`, `set_track_send`, `set_track_monitoring`, `set_track_input_routing`, `set_arrangement_record`, `set_session_record`, `set_arrangement_overdub`, `record_empty_slot`, `stop_recording`, `prepare_track`, `get_clip_region`, `set_clip_region`.

Behavior and examples: [recording-workflow.md](recording-workflow.md).

Mock tests that passed: invalid types, indexes, ranges, and routing names do not write; an occupied slot is not fired; a failed `fire` puts arm back; a bad routing channel restores the previous type; inverted markers do not write; `crop` is never called; stop scopes do not delete clips or stop the transport. That is the test column. The Live column is still empty.

## Browser and Analog Lab, from the installed reader plus a reported probe

`get_browser_items_at_path` at 1.7.1 (`AbletonMCP_Remote_Script/__init__.py` lines 2564-2579) builds each child as `name`, `is_folder`, `is_device`, `is_loadable`, `uri`. There is no tag key in that dict. The 12.4.5 LOM index captured here has no Browser class and no Sound Character member.

A read-only probe of the installed session was already done and was **not** repeated for this survey: the browser payload was those five fields and no tags, and an Analog Lab V Audio Unit exposed only a Device On parameter. That second fact fits the Device page, which says only automatable parameters are accessible (<https://docs.cycling74.com/apiref/lom/device/>). `PluginDevice.presets` and `selected_preset_index` are a different, documented list (<https://docs.cycling74.com/apiref/lom/plugindevice/>). They are not wrapped, and they are not the plug-in's own preset browser. Loading a device from a URI does not search inside it.

## Plan, if more commands are approved

No extra commands were added for this survey. Acceptance checks below are the tests to write, not a schedule.

### Leave as-is until Joe agrees to load 1.8.0

The recording branch. Do not copy it into User Library Remote Scripts while a set is open.

Live acceptance, after that agreement:

1. `set_track_arm`, monitoring, and an input name copied from `get_track_mixer_state` change that track, and a bad routing name does not.
2. On an empty selected scene, note which button `set_session_record` moves. If it is not Session Record, stop using that command name.
3. `record_empty_slot` records only that empty slot. With `record_length` of 4 beats at a known tempo, the clip ends near that length. The reply arrives before the audio is done.
4. `stop_recording` with scope `slot` keeps the clip and leaves other tracks playing.
5. `set_clip_region` moves markers on a warped clip and does not crop the file.
6. Count-in still follows Live's metronome preference. This script does not change it.

### Stage A, only after that Live check

High recording value and a documented member. Each one would be its own small change.

| Next command | Member | Acceptance |
|---|---|---|
| Metronome setter | `Song.metronome` get/set | Explicit bool. Does not start playback. Mock rejects non-bools. Live: the metronome button matches. |
| Read launch quantization, setter only if asked | `Song.clip_trigger_quantization` | Read first. A setter must not hide inside `record_empty_slot`. Live: a 1-bar setting delays the punch visibly. |
| Scene launch | `Scene.fire` | One scene index. Does not record by itself unless Live's preference does. Mock does not call `fire` for a bad index. |
| Locator jump | `CuePoint.jump` | Does not create or delete cues. `create_locator` already exists and is a different operation. |
| Undo | `Song.undo` / `Song.redo` | Explicit call. Not automatic after a take. Mock: `can_undo` false does not call `undo`. |
| Plug-in preset index | `PluginDevice.presets`, `selected_preset_index` | Read the list first. Setting an index is not Analog Lab's internal browser. Live: try a native device and one AU; if the AU list is empty, report that, do not scrape the plug-in UI. |

### Stage B, after Stage A is proven

| Item | Why it waits |
|---|---|
| `Clip.gain` setter | Documented 0.0–1.0. Easy to confuse with dB. `gain_display_string` is read-only. |
| Warp marker read, then a guarded write | `warp_markers` get is documented since Live 11. Writes have BPM and sample-time constraints and change timing. |
| MIDI probability / note id edits | `get_notes_extended` and `apply_note_modifications` are documented. The current writer does not send those fields. |
| Take-lane listing | `Track.take_lanes` is a documented read. Comping a section is not established as one function. |
| Arrangement duplicate | Already wrapped. Do not describe it as moving a clip. `Clip.start_time` is read-only. |

### Not proposed

- Sound Character tag search, until a member or a browser field actually shows tags.
- Clip fades, track reorder, group creation, until a documented member exists.
- `Song.trigger_session_record` as a toggle.
- A count-in setter. The documented property is read-only.
- `begin_undo_step`. It showed up only in the non-official Python stub, not on the Max Song page captured here.
- `Clip.crop`, replacing `file_path`, or any claim of real-time response.
- Handwritten Max devices.

## Installed evidence, short

| Fact | Where |
|---|---|
| Script 1.7.1 | `SCRIPT_VERSION` line 37 at `9dddc7b` |
| Arm is read, not set | `_get_track_info` line 609 at that commit |
| Browser fields, no tags | `get_browser_items_at_path` lines 2564-2579 at that commit |
| Device parameters are name/value/min/max plus flags | `_serialize_device` in the same file; Device page says automatable parameters only |
| New commands are uncommitted relative to 1.7.1 until this branch lands | `RECORDING_COMMANDS` in the branch script, tests under `tests/test_recording_commands.py` |

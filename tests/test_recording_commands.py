"""Mock-Live tests for recording, mixer, routing, and clip-region commands.

The Remote Script runs inside Live, so these tests stub _Framework and drive
AbletonMCP with plain Python objects. They check that invalid input returns
an error and does not call the Live write that would have followed.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (
    REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script" / "AbletonMCP_init.py"
)


def _load_script_module():
    framework = types.ModuleType("_Framework")
    control_surface_mod = types.ModuleType("_Framework.ControlSurface")

    class _StubControlSurface(object):
        def __init__(self, *args, **kwargs):
            pass

        def log_message(self, *args, **kwargs):
            pass

        def show_message(self, *args, **kwargs):
            pass

    control_surface_mod.ControlSurface = _StubControlSurface
    framework.ControlSurface = control_surface_mod
    sys.modules.setdefault("_Framework", framework)
    sys.modules.setdefault("_Framework.ControlSurface", control_surface_mod)
    spec = importlib.util.spec_from_file_location(
        "ableton_mcp_recording_under_test", BUNDLED_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script_module()


def make_instance(script, song=None):
    inst = script.AbletonMCP.__new__(script.AbletonMCP)
    inst.application = lambda: None
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst._song = song
    return inst


def run_now(inst, command_type, params):
    """schedule_message stands in for Live's main thread and runs immediately."""
    order = []

    def schedule(delay, fn):
        assert delay == 0
        order.append("schedule")
        fn()

    inst.schedule_message = schedule
    inst._schedule_order = order
    return inst._process_command({"type": command_type, "params": params})


class Param(object):
    def __init__(self, value, lo=0.0, hi=1.0):
        self._value = float(value)
        self.min = float(lo)
        self.max = float(hi)
        self.writes = 0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new):
        self.writes += 1
        self._value = new


class Routing(object):
    def __init__(self, name):
        self.display_name = name


class Slot(object):
    def __init__(self, has_clip=False, recording=False):
        self.has_clip = has_clip
        self.is_recording = recording
        self.clip = None
        self.fire_calls = []
        self.stop_calls = 0
        self.delete_calls = 0
        self.fire_error = None

    def fire(self, *args):
        self.fire_calls.append(args)
        if self.fire_error:
            raise self.fire_error

    def stop(self):
        self.stop_calls += 1

    def delete_clip(self):
        self.delete_calls += 1


class Track(object):
    def __init__(self, name="Audio", can_arm=True, armed=False):
        self.name = name
        self.name_writes = 0
        self._arm = armed
        self.arm_writes = []
        self.can_be_armed = can_arm
        self.is_frozen = False
        self.is_foldable = False
        self.has_audio_input = True
        self.has_midi_input = False
        self.mute = False
        self.solo = False
        self.mute_writes = 0
        self.playing_slot_index = -1
        self.clip_slots = [Slot(), Slot()]
        self.arrangement_clips = []
        self.current_monitoring_state = 1
        self.monitoring_writes = []
        self._routing_type = Routing("Ext. In")
        self._routing_channel = Routing("1")
        self.type_writes = []
        self.channel_writes = []
        self.channels_for = {
            "Ext. In": [Routing("1"), Routing("2")],
            "Resampling": [Routing("Pre FX"), Routing("Post FX")],
        }
        self.no_inputs = False
        self.volume = Param(0.85)
        self.panning = Param(0.0, -1.0, 1.0)
        self.sends = [Param(0.0), Param(0.25)]
        self.mixer_device = self

    @property
    def arm(self):
        return self._arm

    @arm.setter
    def arm(self, value):
        self.arm_writes.append(value)
        self._arm = value

    @property
    def input_routing_type(self):
        return self._routing_type

    @input_routing_type.setter
    def input_routing_type(self, value):
        self.type_writes.append(value.display_name)
        self._routing_type = value

    @property
    def available_input_routing_types(self):
        if self.no_inputs:
            return []
        return [Routing("Ext. In"), Routing("Resampling"), Routing("No Input")]

    @property
    def available_input_routing_channels(self):
        return list(self.channels_for[self._routing_type.display_name])

    @property
    def input_routing_channel(self):
        return self._routing_channel

    @input_routing_channel.setter
    def input_routing_channel(self, value):
        self.channel_writes.append(value.display_name)
        self._routing_channel = value


class Song(object):
    def __init__(self):
        flags = {
            "record_mode": False,
            "session_record": False,
            "arrangement_overdub": False,
            "overdub": False,
            "is_playing": True,
            "metronome": False,
            "is_counting_in": False,
            "exclusive_arm": False,
        }
        object.__setattr__(self, "_flags", flags)
        object.__setattr__(self, "flag_writes", [])
        self.current_song_time = 1.0
        self.count_in_duration = 1
        self.session_record_status = 0
        self.tracks = []
        self.scenes = []
        self.view = types.SimpleNamespace(selected_scene=None)
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.return_tracks = []
        master = types.SimpleNamespace()
        master.mixer_device = types.SimpleNamespace(
            volume=Param(0.85), panning=Param(0.0, -1.0, 1.0)
        )
        self.master_track = master

    def __getattr__(self, name):
        flags = self.__dict__.get("_flags", {})
        if name in flags:
            return flags[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        flags = self.__dict__.get("_flags")
        if flags is not None and name in flags:
            self.flag_writes.append((name, value))
            flags[name] = value
            return
        object.__setattr__(self, name, value)


class ArrClip(object):
    def __init__(self, start, end):
        self.start_time = start
        self.end_time = end
        self.name = "arr"
        self.length = end - start
        self.color = 0
        self.is_midi_clip = False
        self.is_audio_clip = True
        self.is_playing = False


class RegionClip(object):
    def __init__(self):
        self._v = {
            "start_marker": 0.0,
            "end_marker": 8.0,
            "loop_start": 0.0,
            "loop_end": 8.0,
            "looping": False,
        }
        self.writes = []
        self.fail_on = None
        self.crop_calls = 0
        self.is_audio_clip = True
        self.is_midi_clip = False
        self.warping = True
        self.is_recording = False
        self.is_playing = False
        self.name = "take"
        self.length = 8.0

    def __getattr__(self, name):
        if name in self.__dict__.get("_v", {}):
            return self._v[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        watched = self.__dict__.get("_v")
        if watched is not None and name in watched:
            self.writes.append((name, value))
            if self.fail_on == name:
                raise RuntimeError("rejected by live")
            watched[name] = value
            return
        object.__setattr__(self, name, value)

    def crop(self):
        self.crop_calls += 1


def armed_song():
    song = Song()
    track = Track(armed=True)
    song.tracks = [track]
    scene = object()
    song.scenes = [scene]
    song.view.selected_scene = scene
    return song, track


def test_script_version_matches_package(script):
    from MCP_Server.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

    assert script.SCRIPT_VERSION == EXPECTED_REMOTE_SCRIPT_VERSION == "1.8.1"
    assert script.MAX_EMPTY_SLOT_RECORD_BEATS == 16384
    inst = make_instance(script)
    info = inst._process_command({"type": "get_script_info", "params": {}})
    caps = info["result"]["capabilities"]
    assert set(script.RECORDING_COMMANDS).issubset(caps)
    assert info["result"]["recording"]["crop"] is False
    assert info["result"]["recording"]["count_in_writable"] is False
    assert info["result"]["recording"]["realtime"] is False
    assert set(inst._recording_handlers()) == set(script.RECORDING_COMMANDS)


def test_recording_commands_run_on_schedule_message(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    order = []

    def schedule(delay, fn):
        order.append("schedule")
        assert track.arm_writes == []
        fn()

    inst.schedule_message = schedule
    response = inst._process_command({
        "type": "set_track_arm",
        "params": {"track_index": 0, "armed": False},
    })
    assert order == ["schedule"]
    assert response["status"] == "success"
    assert track.arm_writes == [False]


def test_already_on_main_thread_still_runs(script):
    song, track = armed_song()
    inst = make_instance(script, song)

    def schedule(delay, fn):
        raise AssertionError("already on main thread")

    inst.schedule_message = schedule
    response = run_replaced(inst, "set_track_mute", {"track_index": 0, "mute": True})
    assert response["status"] == "success"
    assert track.mute is True


def run_replaced(inst, command_type, params):
    return inst._process_command({"type": command_type, "params": params})


def test_non_dict_params_do_not_touch_live(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = inst._process_command({"type": "set_track_arm", "params": []})
    assert response["status"] == "error"
    assert "params" in response["message"]
    assert track.arm_writes == []


def test_unknown_field_and_bad_types_do_not_arm(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    for params in (
        {"track_index": 0, "armed": True, "toggle": True},
        {"track_index": 0, "armed": 1},
        {"track_index": True, "armed": True},
        {"armed": True},
        {"track_index": 9, "armed": True},
    ):
        track.arm_writes = []
        response = run_now(inst, "set_track_arm", params)
        assert response["status"] == "error", params
        assert track.arm_writes == []
        assert track.arm is True


def test_group_track_arm_is_refused(script):
    song = Song()
    track = Track(can_arm=False)
    song.tracks = [track]
    inst = make_instance(script, song)
    response = run_now(inst, "set_track_arm", {"track_index": 0, "armed": True})
    assert response["status"] == "error"
    assert track.arm_writes == []


def test_arm_is_idempotent(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_track_arm", {"track_index": 0, "armed": True})
    assert response["status"] == "success"
    assert response["result"]["changed"] is False
    assert track.arm_writes == []


def test_volume_out_of_range_does_not_write(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    bad = run_now(inst, "set_track_volume", {"track_index": 0, "value": 1.2})
    assert bad["status"] == "error"
    assert track.volume.writes == 0
    assert track.volume.value == 0.85
    nan = run_now(inst, "set_track_panning", {"track_index": 0, "value": float("nan")})
    assert nan["status"] == "error"
    assert track.panning.writes == 0
    send = run_now(inst, "set_track_send", {
        "track_index": 0, "send_index": 4, "value": 0.2,
    })
    assert send["status"] == "error"
    assert track.sends[0].writes == 0


def test_valid_mixer_writes_once(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_track_volume", {"track_index": 0, "value": 0.5})
    assert response["status"] == "success"
    assert track.volume.writes == 1
    assert track.volume.value == 0.5
    again = run_now(inst, "set_track_volume", {"track_index": 0, "value": 0.5})
    assert again["result"]["changed"] is False
    assert track.volume.writes == 1


def test_monitoring_rejects_unknown_and_sets_auto(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    bad = run_now(inst, "set_track_monitoring", {
        "track_index": 0, "monitoring": "sometimes",
    })
    assert bad["status"] == "error"
    assert track.monitoring_writes == []
    good = run_now(inst, "set_track_monitoring", {
        "track_index": 0, "monitoring": "Off",
    })
    assert good["status"] == "success"
    assert track.current_monitoring_state == 2
    assert good["result"]["monitoring"] == "off"


def test_unknown_routing_does_not_write_and_lists_options(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_track_input_routing", {
        "track_index": 0, "input_routing_type": "Tape",
    })
    assert response["status"] == "error"
    assert "Resampling" in response["message"]
    assert track.type_writes == []


def test_routing_type_and_bad_channel_restores_type(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_track_input_routing", {
        "track_index": 0,
        "input_routing_type": "Resampling",
        "input_routing_channel": "nope",
    })
    assert response["status"] == "error"
    assert track.type_writes == ["Resampling", "Ext. In"]
    assert track.input_routing_type.display_name == "Ext. In"
    assert track.channel_writes == []


def test_prepare_invalid_monitoring_leaves_name_and_arm(script):
    song, track = armed_song()
    track._arm = False
    inst = make_instance(script, song)
    response = run_now(inst, "prepare_track", {
        "track_index": 0,
        "name": "Vox",
        "arm": True,
        "monitoring": "both",
        "volume": 0.4,
    })
    assert response["status"] == "error"
    assert track.name == "Audio"
    assert track.arm_writes == []
    assert track.volume.writes == 0


def test_prepare_applies_requested_fields_only(script):
    song, track = armed_song()
    track._arm = False
    inst = make_instance(script, song)
    response = run_now(inst, "prepare_track", {
        "track_index": 0,
        "name": "Vox",
        "arm": True,
        "monitoring": "in",
        "volume": 0.7,
        "panning": -0.2,
        "sends": [{"index": 1, "value": 0.3}],
        "input_routing_type": "ext. in",
        "input_routing_channel": "2",
    })
    assert response["status"] == "success", response
    assert track.name == "Vox"
    assert track.arm is True
    assert track.current_monitoring_state == 0
    assert track.volume.value == 0.7
    assert track.panning.value == -0.2
    assert track.sends[1].value == 0.3
    assert track.sends[0].writes == 0
    assert track.input_routing_channel.display_name == "2"
    assert response["result"]["changed"] is True


def test_session_record_refuses_existing_clip_without_writing(script):
    song, track = armed_song()
    track.clip_slots[0].has_clip = True
    inst = make_instance(script, song)
    response = run_now(inst, "set_session_record", {"enabled": True})
    assert response["status"] == "error"
    assert song.flag_writes == []
    assert song.session_record is False
    allowed = run_now(inst, "set_session_record", {
        "enabled": True, "allow_overwrite": True,
    })
    assert allowed["status"] == "success"
    assert song.session_record is True


def test_session_record_allows_empty_selected_scene(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_session_record", {"enabled": True})
    assert response["status"] == "success"
    assert song.flag_writes == [("session_record", True)]
    again = run_now(inst, "set_session_record", {"enabled": True})
    assert again["result"]["changed"] is False
    assert song.flag_writes == [("session_record", True)]


def test_arrangement_record_refuses_overlap(script):
    song, track = armed_song()
    track.arrangement_clips = [ArrClip(0.0, 4.0)]
    inst = make_instance(script, song)
    response = run_now(inst, "set_arrangement_record", {"enabled": True})
    assert response["status"] == "error"
    assert song.record_mode is False
    assert song.flag_writes == []
    clear = Track(armed=True)
    clear.arrangement_clips = [ArrClip(0.0, 4.0)]
    song.current_song_time = 8.0
    song.tracks = [clear]
    ok = run_now(inst, "set_arrangement_record", {"enabled": True})
    assert ok["status"] == "success"
    assert song.record_mode is True


def test_arrangement_record_unverified_clips_do_not_enable(script):
    song, track = armed_song()

    class BoomList(list):
        def __iter__(self):
            raise RuntimeError("arrangement unavailable")

    track.arrangement_clips = BoomList()
    inst = make_instance(script, song)
    response = run_now(inst, "set_arrangement_record", {"enabled": True})
    assert response["status"] == "error"
    assert song.flag_writes == []


def test_arrangement_overdub_does_not_start_record(script):
    song, _track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "set_arrangement_overdub", {"enabled": True})
    assert response["status"] == "success"
    assert song.arrangement_overdub is True
    assert song.record_mode is False
    assert song.count_in_duration == 1


def test_empty_slot_refuses_existing_clip_and_does_not_fire(script):
    song, track = armed_song()
    track._arm = False
    track.clip_slots[1].has_clip = True
    inst = make_instance(script, song)
    response = run_now(inst, "record_empty_slot", {
        "track_index": 0, "clip_index": 1,
    })
    assert response["status"] == "error"
    assert track.clip_slots[1].fire_calls == []
    assert track.arm_writes == []
    assert song.flag_writes == []


def test_empty_slot_unarmed_without_arm_does_not_fire(script):
    song, track = armed_song()
    track._arm = False
    inst = make_instance(script, song)
    response = run_now(inst, "record_empty_slot", {
        "track_index": 0, "clip_index": 0, "arm": False,
    })
    assert response["status"] == "error"
    assert track.clip_slots[0].fire_calls == []


def test_empty_slot_fires_only_that_slot(script):
    song, track = armed_song()
    other = Track(name="Other", armed=False)
    track._arm = False
    song.tracks = [track, other]
    inst = make_instance(script, song)
    response = run_now(inst, "record_empty_slot", {
        "track_index": 0, "clip_index": 0, "record_length": 4,
    })
    assert response["status"] == "success"
    assert track.clip_slots[0].fire_calls == [(4.0,)]
    assert track.clip_slots[1].fire_calls == []
    assert other.arm_writes == []
    assert song.session_record is False
    assert song.count_in_duration == 1
    assert "not that audio has been recorded" in response["result"]["note"]


def test_bad_record_length_does_not_fire(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    for length in (0, -2, 20000, float("inf")):
        track.clip_slots[0].fire_calls = []
        response = run_now(inst, "record_empty_slot", {
            "track_index": 0, "clip_index": 0, "record_length": length,
        })
        assert response["status"] == "error"
        assert track.clip_slots[0].fire_calls == []


def test_failed_fire_restores_arm(script):
    song, track = armed_song()
    track._arm = False
    track.clip_slots[0].fire_error = RuntimeError("live refused")
    inst = make_instance(script, song)
    response = run_now(inst, "record_empty_slot", {
        "track_index": 0, "clip_index": 0,
    })
    assert response["status"] == "error"
    assert track.arm is False
    assert track.arm_writes == [True, False]


def test_stop_scopes_are_non_destructive(script):
    song, track = armed_song()
    song.record_mode = True
    song.session_record = True
    song.flag_writes = []
    playing = Track(name="Playing", armed=False)
    playing.clip_slots[0].has_clip = True
    playing.clip_slots[0].is_recording = False
    track.clip_slots[0].is_recording = True
    song.tracks = [track, playing]
    inst = make_instance(script, song)

    idle = run_now(inst, "stop_recording", {
        "scope": "slot", "track_index": 1, "clip_index": 0,
    })
    assert idle["result"]["changed"] is False
    assert playing.clip_slots[0].stop_calls == 0

    arrangement = run_now(inst, "stop_recording", {"scope": "arrangement"})
    assert arrangement["status"] == "success"
    assert song.record_mode is False
    assert song.session_record is True
    assert track.clip_slots[0].stop_calls == 0
    assert track.clip_slots[0].delete_calls == 0

    session = run_now(inst, "stop_recording", {"scope": "session"})
    assert song.session_record is False
    assert track.clip_slots[0].stop_calls == 0

    song.record_mode = True
    song.session_record = True
    song.flag_writes = []
    everything = run_now(inst, "stop_recording", {"scope": "recording"})
    assert everything["status"] == "success"
    assert song.record_mode is False
    assert song.session_record is False
    assert track.clip_slots[0].stop_calls == 1
    assert playing.clip_slots[0].stop_calls == 0
    assert track.clip_slots[0].delete_calls == 0
    assert song.is_playing is True


def test_stop_rejects_indexes_on_arrangement_scope(script):
    song, _track = armed_song()
    inst = make_instance(script, song)
    response = run_now(inst, "stop_recording", {
        "scope": "arrangement", "track_index": 0,
    })
    assert response["status"] == "error"
    assert song.flag_writes == []


def test_clip_region_rejects_inverted_markers_without_writing(script):
    song, track = armed_song()
    clip = RegionClip()
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    inst = make_instance(script, song)
    response = run_now(inst, "set_clip_region", {
        "track_index": 0, "clip_index": 0, "end_marker": 0.0,
    })
    assert response["status"] == "error"
    assert clip.writes == []
    assert clip.crop_calls == 0
    assert clip.end_marker == 8.0


def test_clip_region_shrinks_loop_before_end_marker(script):
    song, track = armed_song()
    clip = RegionClip()
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    inst = make_instance(script, song)
    response = run_now(inst, "set_clip_region", {
        "track_index": 0,
        "clip_index": 0,
        "end_marker": 4.0,
        "loop_end": 4.0,
    })
    assert response["status"] == "success"
    assert clip.writes[0] == ("loop_end", 4.0)
    assert ("end_marker", 4.0) in clip.writes
    assert clip.writes.index(("loop_end", 4.0)) < clip.writes.index(("end_marker", 4.0))
    assert clip.crop_calls == 0


def test_clip_region_restore_and_no_crop_while_recording(script):
    song, track = armed_song()
    clip = RegionClip()
    clip.fail_on = "looping"
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    inst = make_instance(script, song)
    response = run_now(inst, "set_clip_region", {
        "track_index": 0,
        "clip_index": 0,
        "start_marker": 1.0,
        "looping": True,
    })
    assert response["status"] == "error"
    assert clip.start_marker == 0.0
    assert clip.looping is False
    assert clip.crop_calls == 0

    clip.is_recording = True
    clip.writes = []
    recording = run_now(inst, "set_clip_region", {
        "track_index": 0, "clip_index": 0, "end_marker": 6.0,
    })
    assert recording["status"] == "error"
    assert clip.writes == []


def test_get_recording_state_reports_count_in_without_writing(script):
    song, track = armed_song()
    track.clip_slots[0].is_recording = True
    inst = make_instance(script, song)
    response = run_now(inst, "get_recording_state", {})
    assert response["status"] == "success"
    result = response["result"]
    assert result["count_in_label"] == "1_bar"
    assert result["limitations"]["count_in_writable"] is False
    assert result["session_record_status"] == "off"
    assert result["recording_slots"][0]["track_index"] == 0
    assert result["recording_slots"][0]["clip_index"] == 0
    assert result["recording_slots"][0]["length_status"] == "recording"
    assert song.flag_writes == []
    assert result["limitations"]["uses_trigger_session_record"] is False


def test_get_session_info_stays_readable_without_new_song_attrs(script):
    song = Song()
    # Drop the new flags so a Live build missing them still answers.
    object.__setattr__(song, "_flags", {})
    inst = make_instance(script, song)
    result = inst._get_session_info()
    assert result["tempo"] == 120.0
    assert result["record_mode"] is False
    assert result["count_in_duration"] == 1


def test_start_marker_alone_clamps_loop_and_never_crops(script):
    song, track = armed_song()
    clip = RegionClip()
    clip._v["looping"] = True
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    inst = make_instance(script, song)
    response = run_now(inst, "set_clip_region", {
        "track_index": 0, "clip_index": 0, "start_marker": 2.0,
    })
    assert response["status"] == "success", response
    assert clip.start_marker == 2.0
    assert clip.end_marker == 8.0
    assert clip.loop_start == 2.0
    assert clip.loop_end == 8.0
    assert clip.crop_calls == 0
    assert response["result"]["loop_adjusted"] is True
    assert response["result"]["loop_start"] == 2.0
    clip.writes = []
    refused = run_now(inst, "set_clip_region", {
        "track_index": 0, "clip_index": 0, "start_marker": 3.0, "loop_start": 0.0,
    })
    assert refused["status"] == "error"
    assert clip.writes == []
    assert clip.crop_calls == 0


def test_recording_length_sentinel_is_not_reported_as_beats(script):
    song, track = armed_song()
    clip = RegionClip()
    clip.length = 63072000.0
    clip.is_recording = True
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    track.clip_slots[0].is_recording = True
    track.devices = []
    inst = make_instance(script, song)
    response = run_now(inst, "get_recording_state", {})
    assert response["status"] == "success"
    slot = response["result"]["recording_slots"][0]
    assert slot["length"] is None
    assert slot["length_status"] == "recording"
    assert slot["length_raw"] == 63072000.0
    info = inst._get_track_info(0)
    assert info["clip_slots"][0]["clip"]["length"] is None
    assert info["clip_slots"][0]["clip"]["length_status"] == "recording"
    clip.length = 4.0
    settled = run_now(inst, "get_recording_state", {})
    assert settled["result"]["recording_slots"][0]["length"] == 4.0


def test_stop_playback_rereads_until_transport_settles(script):
    class StopSong(object):
        def __init__(self):
            self.reads = 0

        def stop_playing(self):
            self.stopped = True

        @property
        def is_playing(self):
            self.reads += 1
            return self.reads < 3

    inst = make_instance(script, StopSong())
    result = inst._stop_playback()
    assert result["playing"] is False
    assert result["pending"] is False
    assert inst._song.reads >= 3


def test_volume_and_arm_aliases(script):
    song, track = armed_song()
    inst = make_instance(script, song)
    volume = run_now(inst, "set_track_volume", {"track_index": 0, "volume": 0.4})
    assert volume["status"] == "success"
    assert track.volume.value == 0.4
    pan = run_now(inst, "set_track_panning", {"track_index": 0, "pan": -0.25})
    assert pan["status"] == "success"
    assert track.panning.value == -0.25
    arm = run_now(inst, "set_track_arm", {"track_index": 0, "arm": False})
    assert arm["status"] == "success"
    assert track.arm is False
    conflict = run_now(inst, "set_track_volume", {
        "track_index": 0, "value": 0.2, "volume": 0.3,
    })
    assert conflict["status"] == "error"
    assert track.volume.value == 0.4


def test_record_empty_slot_warns_without_blocking(script):
    song, track = armed_song()
    track.no_inputs = True
    inst = make_instance(script, song)
    response = run_now(inst, "record_empty_slot", {
        "track_index": 0, "clip_index": 0, "record_length": 4,
    })
    assert response["status"] == "success"
    assert track.clip_slots[0].fire_calls == [(4.0,)]
    text = " ".join(response["result"]["warnings"])
    assert "no available audio input" in text
    assert "did not advance" in text
    assert response["result"]["triggered"] is True


def test_quick_record_picks_first_empty_slot_and_converts_bars(script):
    song, track = armed_song()
    track._arm = False
    track.clip_slots[0].has_clip = True
    song.clip_trigger_quantization = 7
    inst = make_instance(script, song)
    response = run_now(inst, "quick_record", {
        "track_index": 0, "bars": 1, "monitoring": "auto", "disarm_after": True,
    })
    assert response["status"] == "success", response
    assert response["result"]["clip_index"] == 1
    assert track.clip_slots[1].fire_calls == [(4.0,)]
    assert track.clip_slots[0].fire_calls == []
    assert track.arm is False
    assert song.clip_trigger_quantization == 7
    assert response["result"]["quantization_changed"] is False
    track.clip_slots[1].has_clip = True
    blocked = run_now(inst, "quick_record", {"track_index": 0, "beats": 4})
    assert blocked["status"] == "error"
    assert track.clip_slots[1].fire_calls == [(4.0,)]


def test_quick_arrangement_record_and_stop(script):
    song, track = armed_song()
    track._arm = False
    track.arrangement_clips = [ArrClip(0.0, 4.0)]
    song.current_song_time = 1.0
    song.clip_trigger_quantization = 7
    song.started = False

    def start_playing():
        song.started = True

    def stop_playing():
        song._flags["is_playing"] = False

    song.start_playing = start_playing
    song.stop_playing = stop_playing
    inst = make_instance(script, song)
    refused = run_now(inst, "quick_arrangement_record", {
        "track_indexes": [0], "arm": True,
    })
    assert refused["status"] == "error"
    assert track.arm_writes == []
    assert song.started is False
    assert song.record_mode is False
    track.arrangement_clips = []
    ok = run_now(inst, "quick_arrangement_record", {
        "track_indexes": [0], "start_time": 8, "arm": True,
    })
    assert ok["status"] == "success", ok
    assert track.arm is True
    assert song.current_song_time == 8
    assert song.record_mode is True
    assert song.started is True
    assert song.clip_trigger_quantization == 7
    stopped = run_now(inst, "quick_arrangement_stop", {
        "track_indexes": [0], "disarm": True,
    })
    assert stopped["status"] == "success", stopped
    assert song.record_mode is False
    assert track.arm is False
    assert stopped["result"]["deleted"] is False


def test_mixer_batch_is_all_or_nothing(script):
    song, first = armed_song()
    second = Track(name="B", armed=False)
    song.tracks = [first, second]
    inst = make_instance(script, song)
    bad = run_now(inst, "set_mixer_batch", {"tracks": [
        {"track_index": 0, "volume": 0.2},
        {"track_index": 1, "volume": 4},
    ]})
    assert bad["status"] == "error"
    assert first.volume.writes == 0
    assert second.volume.writes == 0
    good = run_now(inst, "set_mixer_batch", {"tracks": [
        {"track_index": 0, "volume": 0.2, "mute": True},
        {"track_index": 1, "pan": 0.5, "arm": True},
    ]})
    assert good["status"] == "success", good
    assert first.volume.value == 0.2
    assert first.mute is True
    assert second.panning.value == 0.5
    assert second.arm is True
    assert good["result"]["tracks"][1]["error"] is None


def test_get_track_info_includes_markers(script):
    song, track = armed_song()
    clip = RegionClip()
    clip._v["start_marker"] = 1.0
    clip._v["end_marker"] = 5.0
    track.clip_slots[0].has_clip = True
    track.clip_slots[0].clip = clip
    track.devices = []
    inst = make_instance(script, song)
    info = inst._get_track_info(0)
    assert info["clip_slots"][0]["clip"]["start_marker"] == 1.0
    assert info["clip_slots"][0]["clip"]["end_marker"] == 5.0
    assert info["arm"] is True

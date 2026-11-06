"""MCP wrapper tests. They never open a socket to Live."""

import asyncio
import inspect

import pytest

import MCP_Server.server as server


def call(tool, *args, **kwargs):
    result = tool(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


class FakeConnection(object):
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        return {"ok": True, "command": command_type}


@pytest.fixture
def fake_conn(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
    return conn


@pytest.fixture(autouse=True)
def _silence_telemetry(monkeypatch):
    class _Dummy(object):
        def record_event(self, *args, **kwargs):
            pass

    import MCP_Server.telemetry_decorator as td
    monkeypatch.setattr(td, "get_telemetry", lambda: _Dummy(), raising=False)


@pytest.fixture(autouse=True)
def _no_consent_prompt(monkeypatch):
    import MCP_Server.dataset.consent as consent
    monkeypatch.setattr(consent, "maybe_consent_notice", lambda: "")


@pytest.fixture(autouse=True)
def _script_capabilities_available(monkeypatch):
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(sh, "require_capability", lambda name: None)


def test_length_constant_matches_remote_script():
    from MCP_Server.server import _MAX_EMPTY_SLOT_RECORD_BEATS

    # Duplicated on purpose. tests/test_recording_commands.py compares the
    # bundled Remote Script constant with the same number.
    assert _MAX_EMPTY_SLOT_RECORD_BEATS == 16384


def test_prepare_sends_only_provided_fields(fake_conn):
    call(
        server.prepare_track,
        None,
        track_index=1,
        arm=True,
        monitoring="Auto",
    )
    assert fake_conn.sent == [(
        "prepare_track",
        {"track_index": 1, "arm": True, "monitoring": "auto"},
    )]


def test_bad_monitoring_does_not_connect(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.set_track_monitoring, None, track_index=0, monitoring="maybe")
    assert result.startswith("Error set_track_monitoring")


def test_bad_record_length_does_not_send(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(
        server.record_empty_slot, None, track_index=0, clip_index=0, record_length=0
    )
    assert result.startswith("Error record_empty_slot")


def test_stop_scope_is_checked_before_send(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.stop_recording, None, scope="everything")
    assert result.startswith("Error stop_recording")


def test_missing_capability_does_not_connect(monkeypatch):
    import MCP_Server.script_handshake as sh

    monkeypatch.setattr(sh, "require_capability", lambda name: "missing cap")

    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.get_recording_state, None)
    assert result == "missing cap"


def test_arrangement_record_forwards_allow_overwrite(fake_conn):
    call(server.set_arrangement_record, None, enabled=True, allow_overwrite=False)
    assert fake_conn.sent == [(
        "set_arrangement_record",
        {"enabled": True, "allow_overwrite": False},
    )]


def test_empty_routing_is_not_sent(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.set_track_input_routing, None, track_index=0)
    assert result.startswith("Error set_track_input_routing")


def test_volume_alias_is_forwarded(fake_conn):
    call(server.set_track_volume, None, track_index=0, volume=0.4)
    assert fake_conn.sent == [("set_track_volume", {"track_index": 0, "volume": 0.4})]


def test_quick_record_forwards_one_length(fake_conn):
    call(server.quick_record, None, track_index=2, bars=2, monitoring="in")
    command, params = fake_conn.sent[0]
    assert command == "quick_record"
    assert params["track_index"] == 2
    assert params["bars"] == 2
    assert params["monitoring"] == "in"
    assert "beats" not in params
    assert "launch_quantization" not in params


def test_quick_record_rejects_both_lengths(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.quick_record, None, track_index=0, bars=1, beats=4)
    assert result.startswith("Error quick_record")


def test_mixer_batch_forwards_the_list(fake_conn):
    tracks = [{"track_index": 0, "volume": 0.5, "pan": 0.0}]
    call(server.set_mixer_batch, None, tracks=tracks)
    assert fake_conn.sent == [("set_mixer_batch", {"tracks": tracks})]


def test_clip_region_requires_a_field(monkeypatch):
    def boom():
        raise AssertionError("should not connect")

    monkeypatch.setattr(server, "get_ableton_connection", boom)
    result = call(server.set_clip_region, None, track_index=0, clip_index=0)
    assert result.startswith("Error set_clip_region")

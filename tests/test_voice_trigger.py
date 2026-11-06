"""Voice trigger tests. No microphone, no Vosk, no Live."""

import json
import socket
import threading
from pathlib import Path

from voice_trigger.client import AbletonClient
from voice_trigger.engine import Engine
from voice_trigger.gate import LoopbackGate
from voice_trigger.tones import TonePlayer
from voice_trigger.translate import translate
from voice_trigger.words import WordList, number_phrase


class FakeClient(object):
    def __init__(self):
        self.sent = []

    def send(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        return {"status": "success", "result": {}}


def engine(words, client=None):
    client = client or FakeClient()
    tones = TonePlayer()
    gate = LoopbackGate()
    eng = Engine(words, client, tones=tones, gate=gate, log=lambda line: None, debounce_sec=0.4)
    return eng, client, tones, gate


def test_number_phrase_and_grammar_contains_instruments(tmp_path):
    path = tmp_path / "words.json"
    path.write_text(json.dumps({
        "instruments": {"keys": "Keys", "guitar": "Guitar"},
        "default_bars": 8,
    }))
    words = WordList(path)
    phrases = words.grammar_phrases()
    assert "keys" in phrases
    assert "keys and guitar" in phrases
    assert "guitar and keys" in phrases
    assert "take" in phrases
    assert "music bot" in phrases
    assert "tempo " + number_phrase(120) in phrases
    assert number_phrase(120) == "one hundred twenty"
    assert number_phrase(85) == "eighty five"


def test_play_mode_translates_and_call_mode_ignores_commands():
    words = {"instruments": {"keys": "Keys", "guitar": "Guitar"}, "default_bars": 8}
    arm = translate("keys and guitar", words, "play")
    assert arm["type"] == "arm_tracks"
    assert arm["params"]["names"] == ["Keys", "Guitar"]
    assert arm["params"]["exclusive"] is True
    take = translate("take", words, "play")
    assert take["params"]["bars"] == 8
    assert take["params"]["count_in"] is True
    assert translate("take four", words, "play")["params"]["bars"] == 4
    assert translate("loop", words, "play")["params"]["loop"] is True
    assert translate("stop", words, "play")["type"] == "stop_performance"
    assert translate("faster", words, "play")["params"]["delta"] == 5
    assert translate("slower", words, "play")["params"]["delta"] == -5
    assert translate("tempo one hundred twenty", words, "play")["params"]["tempo"] == 120
    ignored = translate("take", words, "call")
    assert ignored["action"] == "ignore"
    assert translate("bye bot", words, "call")["mode"] == "play"
    assert translate("music bot", words, "play")["mode"] == "call"


def test_engine_mode_switch_and_silence_to_loopback():
    words_path_data = {"instruments": {"keys": "Keys"}, "default_bars": 8}

    class StaticWords(object):
        def __init__(self):
            self.data = words_path_data

        def reload_if_changed(self):
            return False

    eng, client, tones, gate = engine(StaticWords())
    eng.handle("take eight")
    assert client.sent[-1][0] == "take"
    assert client.sent[-1][1]["bars"] == 8
    assert gate.mode == "play"
    assert gate.write([0.5, -0.25]) == [0.0, 0.0]
    eng.handle("music bot")
    assert eng.mode == "call"
    assert gate.mode == "call"
    assert gate.write([0.5]) == [0.5]
    before = len(client.sent)
    eng.handle("take")
    assert len(client.sent) == before
    eng.handle("bye bot")
    assert eng.mode == "play"
    assert "mode" in tones.played


def test_hot_reload_picks_up_a_new_instrument(tmp_path):
    path = tmp_path / "words.json"
    path.write_text(json.dumps({"instruments": {"keys": "Keys"}, "default_bars": 8}))
    words = WordList(path)
    assert "bass" not in words.grammar_phrases()
    path.write_text(json.dumps({
        "instruments": {"keys": "Keys", "bass": "Bass"},
        "default_bars": 8,
    }))
    # mtime can be identical on a fast filesystem; force the next check by
    # touching the file's recorded mtime.
    words.mtime = 0
    assert words.reload_if_changed() is True
    assert "bass" in words.grammar_phrases()
    action = translate("bass", words.data, "play")
    assert action["params"]["names"] == ["Bass"]


def test_fake_socket_receives_one_json_command():
    received = []

    def serve(sock):
        conn, _addr = sock.accept()
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            try:
                received.append(json.loads(data.decode("utf-8")))
                break
            except ValueError:
                continue
        conn.sendall(json.dumps({"status": "success", "result": {"ok": True}}).encode("utf-8"))
        conn.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    thread = threading.Thread(target=serve, args=(server,))
    thread.start()
    try:
        reply = AbletonClient("127.0.0.1", port, timeout=2).send("take", {"bars": 8})
    finally:
        thread.join(2)
        server.close()
    assert received == [{"type": "take", "params": {"bars": 8}}]
    assert reply["status"] == "success"


def test_debounce_drops_a_repeat():
    class StaticWords(object):
        data = {"instruments": {}, "default_bars": 8}

        def reload_if_changed(self):
            return False

    eng, client, _tones, _gate = engine(StaticWords())
    eng.handle("stop", now=10)
    eng.handle("stop", now=10.1)
    assert [item[0] for item in client.sent] == ["stop_performance"]

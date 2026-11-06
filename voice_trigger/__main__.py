"""Run the trigger. Microphone, Vosk, and Loopback are optional until configured."""

import argparse
import json
from pathlib import Path

from .engine import Engine
from .gate import LoopbackGate
from .tones import TonePlayer
from .words import WordList
from .client import AbletonClient


def main(argv=None):
    parser = argparse.ArgumentParser(description="AbletonMCP voice trigger")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    words_path = config_path.parent / config.get("words_file", "words.json")
    words = WordList(words_path)
    log_path = config_path.parent / config.get("log_file", "voice-trigger.log")

    def log(line):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    client = AbletonClient(
        config.get("ableton_host", "127.0.0.1"),
        config.get("ableton_port", 9877),
        config.get("command_timeout", 1.5),
    )
    tones = TonePlayer(config.get("tone_output_device"))
    gate = LoopbackGate(config.get("loopback_output_device"))
    engine = Engine(
        words, client, tones=tones, gate=gate, log=log,
        min_confidence=config.get("min_confidence", 0.65),
        debounce_sec=config.get("debounce_sec", 0.45),
    )
    if not config.get("loopback_output_device"):
        log("loopback_output_device is unset; call mic gate is off; recognition still uses the real mic")
    log("voice trigger ready mode=play grammar=%d" % len(words.grammar_phrases()))
    model = config.get("vosk_model")
    if not model:
        log("no vosk_model in config; exiting without opening the microphone")
        return 0
    try:
        import sounddevice  # noqa: F401
        import vosk
    except ImportError as exc:
        log("audio dependencies missing: %s" % exc)
        return 1
    return run_audio(engine, words, config, vosk)


def run_audio(engine, words, config, vosk):
    import sounddevice
    model = vosk.Model(config["vosk_model"])
    rate = int(config.get("sample_rate", 16000))
    recognizer = vosk.KaldiRecognizer(model, rate, words.grammar_json())
    device = config.get("input_device")

    def callback(indata, frames, time_info, status):
        if status:
            engine.log("audio %s" % status)
        samples = bytes(indata)
        try:
            import array
            values = array.array("h")
            values.frombytes(samples)
            engine.gate.write([v / 32768.0 for v in values])
        except Exception:
            pass
        if words.reload_if_changed():
            recognizer.SetGrammar(words.grammar_json())
            engine.log("reloaded words")
        if recognizer.AcceptWaveform(samples):
            result = json.loads(recognizer.Result() or "{}")
            text = result.get("text") or ""
            if text:
                engine.handle(text, confidence=result.get("confidence"))

    with sounddevice.RawInputStream(
        samplerate=rate, blocksize=4000, dtype="int16", channels=1,
        device=device, callback=callback,
    ):
        import time
        while True:
            time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())

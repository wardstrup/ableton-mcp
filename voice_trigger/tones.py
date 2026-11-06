"""Short confirmation tones. Playback is fire-and-forget; failures are ignored."""

import math


_FREQ = {
    "heard": 660.0,
    "ok": 880.0,
    "error": 220.0,
    "mode": 520.0,
}


def render_tone(kind, sample_rate=16000, ms=70):
    freq = _FREQ.get(kind, 440.0)
    count = int(sample_rate * (ms / 1000.0))
    return [0.2 * math.sin(2.0 * math.pi * freq * i / sample_rate) for i in range(count)]


class TonePlayer(object):
    def __init__(self, output_device=None, player=None):
        self.output_device = output_device
        self.player = player
        self.played = []

    def play(self, kind):
        samples = render_tone(kind)
        self.played.append(kind)
        if self.player is None or not self.output_device:
            return
        try:
            self.player(self.output_device, samples)
        except Exception:
            return

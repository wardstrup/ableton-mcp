"""Feed the call app's mic from the real microphone only while CALL mode is on.

Recognition always reads the real mic. This gate is a second stream: in CALL
mode it copies that mic to the Loopback virtual device; in PLAY mode it
writes silence. The call app uses the Loopback device as its microphone, so
muting the system input is not required and would deafen the trigger.
"""


class LoopbackGate(object):
    def __init__(self, output_device=None, player=None):
        self.output_device = output_device
        self.player = player
        self.mode = "play"
        self.frames = []

    def set_mode(self, mode):
        self.mode = mode

    def write(self, samples):
        """samples is a sequence of floats from the real mic. Never blocks the recognizer."""
        if self.mode == "call":
            outgoing = list(samples)
        else:
            outgoing = [0.0] * len(list(samples))
        self.frames.append(outgoing)
        if self.player is not None and self.output_device:
            self.player(self.output_device, outgoing)
        return outgoing

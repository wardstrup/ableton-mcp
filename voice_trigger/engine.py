"""Decide and dispatch. The recognizer and the socket are injected."""

import time


class Engine(object):
    def __init__(self, words, client, tones=None, gate=None, log=None,
                 min_confidence=0.65, debounce_sec=0.45):
        self.words = words
        self.client = client
        self.tones = tones
        self.gate = gate
        self.log = log or (lambda line: None)
        self.min_confidence = float(min_confidence)
        self.debounce_sec = float(debounce_sec)
        self.mode = "play"
        self.count_in = True
        self._last = {}

    def handle(self, text, confidence=None, now=None):
        self.words.reload_if_changed()
        if confidence is not None and float(confidence) < self.min_confidence:
            self.log("drop low confidence %s %s" % (confidence, text))
            return {"action": "drop", "reason": "confidence"}
        from .translate import translate
        action = translate(text, self.words.data, self.mode, count_in=self.count_in)
        stamp = time.time() if now is None else now
        if self._debounced(action, stamp):
            self.log("debounce %s" % action.get("phrase"))
            return {"action": "drop", "reason": "debounce"}
        self.log("heard %s mode=%s -> %s" % (text, self.mode, action.get("action")))
        if self.tones is not None:
            self.tones.play("heard")
        if action["action"] == "ignore":
            return action
        if action["action"] == "mode":
            self.mode = action["mode"]
            if self.gate is not None:
                self.gate.set_mode(self.mode)
            if self.tones is not None:
                self.tones.play("mode")
            self.log("mode %s" % self.mode)
            return action
        if action["action"] == "count":
            self.count_in = bool(action["count_in"])
            self._dispatch(action)
            return action
        if action["action"] == "command":
            if action["type"] == "take":
                action["params"]["count_in"] = self.count_in
            self._dispatch(action)
            return action
        return action

    def _debounced(self, action, stamp):
        if action.get("action") not in ("command", "count", "mode"):
            return False
        key = (action.get("phrase"), action.get("type"), json_key(action.get("params")))
        previous = self._last.get(key)
        self._last[key] = stamp
        return previous is not None and (stamp - previous) < self.debounce_sec

    def _dispatch(self, action):
        try:
            reply = self.client.send(action["type"], action.get("params") or {})
        except Exception as exc:
            self.log("error %s %s" % (action.get("type"), exc))
            if self.tones is not None:
                self.tones.play("error")
            action["error"] = str(exc)
            return
        action["reply"] = reply
        ok = isinstance(reply, dict) and reply.get("status") == "success"
        if self.tones is not None:
            self.tones.play("ok" if ok else "error")
        self.log("sent %s status=%s" % (action.get("type"), reply.get("status") if isinstance(reply, dict) else "bad"))


def json_key(params):
    if not params:
        return ""
    parts = []
    for key in sorted(params):
        parts.append("%s=%s" % (key, params[key]))
    return ",".join(parts)

"""Map a recognized phrase to a Remote Script command. No audio, no sockets."""

from .words import normalize, number_phrase


def translate(text, words, mode, count_in=True):
    """Return an action dict.

    mode is "play" or "call". CALL mode ignores every phrase except "bye bot".
    PLAY mode ignores "bye bot" and anything that is not a command.
    """
    phrase = normalize(text)
    if mode == "call":
        if phrase == "bye bot":
            return {"action": "mode", "mode": "play", "phrase": phrase}
        return {"action": "ignore", "phrase": phrase, "reason": "call_mode"}
    if phrase == "music bot":
        return {"action": "mode", "mode": "call", "phrase": phrase}
    if phrase == "bye bot":
        return {"action": "ignore", "phrase": phrase, "reason": "play_mode"}
    instruments = words.get("instruments") or {}
    bars = int(words.get("default_bars", 8))
    if phrase in instruments:
        return _arm([instruments[phrase]], phrase)
    if " and " in phrase:
        left, right = phrase.split(" and ", 1)
        if left in instruments and right in instruments and left != right:
            return _arm([instruments[left], instruments[right]], phrase)
    if phrase == "take":
        return _take(bars, False, count_in, phrase)
    if phrase == "take four":
        return _take(4, False, count_in, phrase)
    if phrase == "take eight":
        return _take(8, False, count_in, phrase)
    if phrase == "loop":
        return _take(bars, True, count_in, phrase)
    if phrase == "stop":
        return {"action": "command", "phrase": phrase, "type": "stop_performance", "params": {}}
    if phrase == "again":
        return {"action": "command", "phrase": phrase, "type": "again", "params": {}}
    if phrase == "keep":
        return {"action": "command", "phrase": phrase, "type": "keep_last", "params": {}}
    if phrase == "scrap":
        return {"action": "command", "phrase": phrase, "type": "scrap", "params": {}}
    if phrase == "cancel":
        return {"action": "command", "phrase": phrase, "type": "cancel", "params": {}}
    if phrase == "play":
        return {"action": "command", "phrase": phrase, "type": "play_transport", "params": {}}
    if phrase == "pause":
        return {"action": "command", "phrase": phrase, "type": "pause_transport", "params": {}}
    if phrase == "click on":
        return {"action": "command", "phrase": phrase, "type": "set_metronome", "params": {"enabled": True}}
    if phrase == "click off":
        return {"action": "command", "phrase": phrase, "type": "set_metronome", "params": {"enabled": False}}
    if phrase == "count on":
        return {"action": "count", "phrase": phrase, "count_in": True, "type": "set_count_in", "params": {"bars": 1}}
    if phrase == "count off":
        return {"action": "count", "phrase": phrase, "count_in": False, "type": "set_count_in", "params": {"bars": 0}}
    if phrase == "slower":
        return {"action": "command", "phrase": phrase, "type": "nudge_tempo", "params": {"delta": -5}}
    if phrase == "faster":
        return {"action": "command", "phrase": phrase, "type": "nudge_tempo", "params": {"delta": 5}}
    if phrase == "mark":
        return {"action": "command", "phrase": phrase, "type": "mark_playhead", "params": {}}
    if phrase == "session":
        return {"action": "command", "phrase": phrase, "type": "show_view", "params": {"view": "session"}}
    if phrase == "arrangement":
        return {"action": "command", "phrase": phrase, "type": "show_view", "params": {"view": "arrangement"}}
    if phrase.startswith("tempo "):
        tempo = _tempo_from_phrase(phrase[len("tempo "):])
        if tempo is None:
            return {"action": "ignore", "phrase": phrase, "reason": "tempo"}
        return {"action": "command", "phrase": phrase, "type": "set_tempo", "params": {"tempo": tempo}}
    return {"action": "ignore", "phrase": phrase, "reason": "unknown"}


def _arm(names, phrase):
    return {
        "action": "command",
        "phrase": phrase,
        "type": "arm_tracks",
        "params": {"names": names, "exclusive": True},
    }


def _take(bars, loop, count_in, phrase):
    return {
        "action": "command",
        "phrase": phrase,
        "type": "take",
        "params": {"bars": bars, "loop": bool(loop), "count_in": bool(count_in)},
    }


def _tempo_from_phrase(words):
    target = normalize(words)
    for tempo in range(40, 221):
        if number_phrase(tempo) == target:
            return tempo
    return None

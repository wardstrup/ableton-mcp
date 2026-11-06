"""Word list, Vosk grammar, and hot reload."""

import json
from pathlib import Path


_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_phrase(value):
    """English words for an integer tempo, 0 through 999."""
    number = int(value)
    if number < 0 or number > 999:
        raise ValueError("tempo out of range")
    if number < 20:
        return _ONES[number]
    if number < 100:
        rest = number % 10
        name = _TENS[number // 10]
        return name if rest == 0 else name + " " + _ONES[rest]
    hundreds = number // 100
    rest = number % 100
    name = _ONES[hundreds] + " hundred"
    if rest:
        name += " " + number_phrase(rest)
    return name


def load_words(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    instruments = data.get("instruments") or {}
    if not isinstance(instruments, dict):
        raise ValueError("instruments must be an object of spoken word to track name")
    cleaned = {}
    for spoken, track in instruments.items():
        key = normalize(spoken)
        if not key or not isinstance(track, str) or not track.strip():
            raise ValueError("bad instrument entry: %r" % spoken)
        cleaned[key] = track.strip()
    bars = int(data.get("default_bars", 8))
    return {"instruments": cleaned, "default_bars": bars}


def normalize(text):
    return " ".join(str(text or "").lower().replace("-", " ").split())


class WordList(object):
    """Reload words.json when its mtime changes. No process restart."""

    def __init__(self, path):
        self.path = Path(path)
        self.mtime = None
        self.data = {"instruments": {}, "default_bars": 8}
        self.reload_if_changed(force=True)

    def reload_if_changed(self, force=False):
        if not self.path.exists():
            return False
        mtime = self.path.stat().st_mtime
        if not force and mtime == self.mtime:
            return False
        self.data = load_words(self.path)
        self.mtime = mtime
        return True

    def grammar_phrases(self):
        names = list(self.data["instruments"].keys())
        phrases = [
            "music bot",
            "bye bot",
            "take",
            "take four",
            "take eight",
            "loop",
            "stop",
            "again",
            "keep",
            "scrap",
            "cancel",
            "play",
            "pause",
            "click on",
            "click off",
            "count on",
            "count off",
            "slower",
            "faster",
            "mark",
            "session",
            "arrangement",
        ]
        phrases.extend(names)
        for left in names:
            for right in names:
                if left != right:
                    phrases.append(left + " and " + right)
        for tempo in range(40, 221):
            phrases.append("tempo " + number_phrase(tempo))
        # Vosk wants a JSON list. Duplicates are harmless but noisy.
        seen = []
        for phrase in phrases:
            if phrase not in seen:
                seen.append(phrase)
        return seen

    def grammar_json(self):
        return json.dumps(self.grammar_phrases())

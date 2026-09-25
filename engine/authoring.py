"""Writing the script somewhere else, and bringing it back.

Two halves of one job. ``build_prompt`` hands the user a brief they can
paste into whatever tool they like; ``read_script_json`` takes what comes
back and turns it into beats the manual form can be filled from.

Both are built on ``authoring_facts``, which is also what the blank form
is rendered from. That is deliberate and it is the point of this module
existing at all: a prompt that asked for ten beats while the engine wanted
twelve would waste a whole round trip through another tool before anyone
noticed, and this codebase has already shipped two bugs from copies of
exactly these numbers drifting apart. There is one dict, and everything
reads it.

Nothing here calls a model. The manual path's whole guarantee is that it
cannot, and importing text a human fetched from somewhere else does not
change that — the user runs the other tool themselves, in their own
browser, and this module only ever reads the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import get_args

from engine.agents import LATIN_LETTERS_RE, WORD_TOLERANCE, latin_words
from engine.config import (beat_count, beat_word_range, speech_rate,
                           spoken_seconds, word_budget, words_per_beat)
from engine.contract import Motion, Role, Transition
from engine.gates.qc import pre_render_range

# Re-exported so the import path and the script gate hold a writer to the
# same rule. ``tests/test_authoring_import.py`` asserts the identity.
__all__ = ["authoring_facts", "build_prompt", "read_script_json",
           "ImportResult", "latin_words"]

# What a beat cannot be assembled without. Everything else this module is
# willing to fill in, because no other tool can reasonably be asked for a
# beat id or this project's private role arc.
REQUIRED_FIELDS = ("voice_text", "caption_text", "visual_prompt")


def authoring_facts(settings, *, topic_max: int) -> dict:
    """Every number the authoring surfaces need, from one place.

    ``topic_max`` is passed in rather than imported because it belongs to
    the HTTP layer's request limits, not to the engine's tuning.
    """
    budget = word_budget(settings)
    count = beat_count(settings)
    per_beat = words_per_beat(budget, count, settings)
    beat_words_min, beat_words_max = beat_word_range(per_beat, settings)
    gate_min, gate_max = pre_render_range(settings.duration_min,
                                          settings.duration_max)
    # Imported here rather than at module scope: engine.pipeline imports
    # engine.agents, and a top-level import would close a cycle.
    from engine.pipeline import default_roles

    return {
        "beat_count": count,
        "word_budget": budget,
        "word_tolerance": WORD_TOLERANCE,
        "word_min": round(budget * (1 - WORD_TOLERANCE)),
        "word_max": round(budget * (1 + WORD_TOLERANCE)),
        "words_per_beat": per_beat,
        "beat_words_min": beat_words_min,
        "beat_words_max": beat_words_max,
        "speech_rate": speech_rate(settings),
        "predicted_seconds": spoken_seconds(budget, settings),
        "target_seconds": settings.target_seconds,
        "duration_min": settings.duration_min,
        "duration_max": settings.duration_max,
        "gate_min": gate_min,
        "gate_max": gate_max,
        "sticker_max": settings.sticker_max,
        "default_roles": default_roles(count, settings),
        "roles": list(get_args(Role)),
        "motions": list(get_args(Motion)),
        "transitions": list(get_args(Transition)),
        # The Devanagari rule's own regex, so a panel warning cannot
        # disagree with the server's refusal.
        "latin_pattern": LATIN_LETTERS_RE.pattern,
        "topic_max": topic_max,
    }


def build_prompt(topic: str, facts: dict) -> str:
    """A brief for another tool, with this engine's real numbers in it.

    Written to be pasted whole. Every figure is interpolated from
    ``facts`` — none is spelled out here, so changing
    ``RAHASYA_SCRIPT_BEATS`` or the speech rate changes what the other
    tool is asked for, with nothing to keep in step by hand.

    It asks for no source URLs, and says why. A model asked for citations
    supplies ones that look right; the import would then file them as
    provenance, and neither QC nor the publish checklist has any way to
    tell an invented URL from a real one. Sources are the user's to add.
    """
    count = facts["beat_count"]
    example = {
        "topic": topic or "<your topic>",
        "beats": [
            {"voice_text": "<Devanagari, spoken>",
             "caption_text": "<Roman Hinglish, burned on screen>",
             "visual_prompt": "<English, what the footage should show>",
             "on_screen_text": "<optional: 2-4 words, or omit>",
             "sticker": {"word": "<a word from this caption_text>",
                         "terms": ["<english noun>", "<english noun>"]}},
        ],
    }
    return f"""Write a script for a {facts['duration_min']:.0f}-{facts['duration_max']:.0f} second
Hinglish dark-mystery short about:

  {topic or '<your topic>'}

Return JSON only — no prose before or after it, no markdown fence.

Shape, with exactly {count} objects in "beats":

{json.dumps(example, ensure_ascii=False, indent=2)}

Three fields are required on every beat, and two are optional:

  voice_text     What is spoken. **Devanagari script only.** Not a style
                 rule: the voice reads Latin letters as English and
                 mispronounces them. Write numbers as words too
                 ("अठारह", not "18"). See the vocabulary note below —
                 the script is Devanagari, the language is Hinglish.
  caption_text   The same line in Roman Hinglish. This is what gets
                 burned on screen, so digits are fine here.
  visual_prompt  English, one sentence, describing the shot — this is
                 fed to a stock-footage search, so name things that can
                 be filmed rather than moods.

  on_screen_text Optional. Two to four words that land in the middle of
                 the frame for the middle of the beat — the pattern
                 interrupt, not a second subtitle. Roman, like the
                 caption. Omit the field on beats that have no punch in
                 them; one in every beat is noise, and a beat that has
                 to reach for one has none.
  sticker        Optional. An icon that pops on one word:

                     "sticker": {{"word": "darwaza",
                                 "terms": ["door", "gate"]}}

                 ``word`` must be a word that appears in this beat's
                 caption_text, spelled the same way — the icon is timed
                 off that word and a word the caption does not contain
                 has no clock to sit on.

                 ``terms`` are English, one noun each, and they are
                 searched against an icon library named for **objects**:
                 "dream" finds nothing and "cloud" finds an icon, so
                 name the thing that stands for the idea. Give two or
                 three; the first that matches wins.

                 At most {facts['sticker_max']} in the whole script —
                 they are punctuation, and punctuation everywhere is
                 just noise. Omit the field on every other beat.

**Write Hinglish, not Hindi.** Devanagari is the script, not the
vocabulary. An English word the audience actually says stays that
English word, spelled out in Devanagari — transliterated, never
translated into its literary Hindi equivalent:

    ads       ऐड्स        not  विज्ञापन
    search    सर्च        not  खोज
    video     वीडियो      not  चलचित्र
    result    रिज़ल्ट      not  परिणाम
    confirm   कन्फर्म      not  पुष्टि करना
    DNA       डीएनए

If a Hindi word is the one people use — दरवाज़ा, रात, सच — use it. The
test is what someone would say out loud, not what a textbook prefers.
Formal or literary Hindi reads as a news bulletin and loses the room.

Length is the constraint that matters most:

  * {facts['word_budget']} spoken words across the whole script
    (anything from {facts['word_min']} to {facts['word_max']} is accepted).
  * Roughly {facts['words_per_beat']} words a beat, and between
    {facts['beat_words_min']} and {facts['beat_words_max']}.
  * That is measured, not guessed: the voice speaks
    {facts['speech_rate']} words a second, so {facts['word_budget']} words
    is about {facts['predicted_seconds']:.0f} seconds.

Count the words in voice_text. A script over budget is rejected before
anything is rendered.

The beats run as an arc — the first is the hook that has to earn three
seconds of attention, the last is the call to action.

**Do not include source URLs, citations or a "sources" field.** They
would be filed as this video's provenance, and an invented URL is
indistinguishable from a real one once it is in there. Sources get added
by hand afterwards.
"""


@dataclass
class ImportResult:
    """What came back from a paste, and what is wrong with it.

    ``problems`` is the only thing that decides acceptance. ``filled`` and
    ``notes`` are the record of what this module did on the user's behalf
    — a silent repair is the failure this separation exists to prevent.
    """

    beats: list[dict] = field(default_factory=list)
    topic: str = ""
    filled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    dropped_sources: int = 0
    words: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {"ok": self.ok, "beats": self.beats, "topic": self.topic,
                "filled": self.filled, "notes": self.notes,
                "problems": self.problems,
                "dropped_sources": self.dropped_sources,
                "words": self.words}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _sticker(raw) -> dict | None:
    """The beat's sticker request, or None when it asked for none.

    Tolerant on purpose: a model that returns a bare string, drops
    ``terms``, or returns them as one comma-joined string is asking for a
    sticker and should get one. Only a request with no word at all is
    nothing, because a sticker with no word has no clock.
    """
    if not isinstance(raw, dict):
        return None
    word = _text(raw.get("word"))
    if not word:
        return None
    terms = raw.get("terms")
    if isinstance(terms, str):
        terms = terms.split(",")
    if not isinstance(terms, (list, tuple)):
        terms = []
    clean = [t for t in (_text(term).lower() for term in terms) if t]
    return {"word": word, "terms": clean}


def read_script_json(raw: str, facts: dict, settings) -> ImportResult:
    """Turn a pasted JSON script into beats the manual form can hold.

    Tolerant about shape, strict about content. Another tool cannot be
    asked for beat ids, this project's role arc, or its motion and
    transition vocabulary, so those are filled from ``facts`` and every
    fill is recorded. Everything that is actually the writer's decision —
    the three texts, the beat count, the script's length, the Devanagari
    rule — is refused with a reason naming the beat, never corrected.

    Returns an ``ImportResult`` rather than raising: a paste with four
    things wrong should report four things, not the first one.
    """
    result = ImportResult()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        result.problems.append(
            f"that is not valid JSON: {exc.msg}, at line {exc.lineno} "
            f"column {exc.colno}. Paste the whole object, from the first "
            f"{{ to the last }}, with no markdown fence around it.")
        return result

    if not isinstance(data, dict):
        result.problems.append(
            f"the JSON is a {type(data).__name__}, not an object. It needs "
            f'to be {{"topic": ..., "beats": [...]}}.')
        return result

    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        result.problems.append(
            'there is no "beats" list in that JSON. It needs a "beats" '
            "array with one object per beat.")
        return result

    result.topic = _text(data.get("topic"))

    sources = data.get("sources")
    if isinstance(sources, list) and sources:
        result.dropped_sources = len(sources)
        result.notes.append(
            f"{len(sources)} source(s) in the JSON were dropped. A URL a "
            f"model supplied cannot be told apart from one you checked, "
            f"and it would be filed as this video's provenance — add "
            f"sources yourself below, or tick that the script asserts "
            f"nothing.")

    expected = facts["beat_count"]
    if len(raw_beats) != expected:
        result.problems.append(
            f"that JSON has {len(raw_beats)} beats and this engine wants "
            f"{expected}. Ask for exactly {expected}, or change "
            f"RAHASYA_SCRIPT_BEATS and reload.")

    roles = facts["default_roles"]
    filled: dict[str, int] = {}
    total_words = 0

    for index, item in enumerate(raw_beats):
        beat_id = f"b{index + 1}"
        if not isinstance(item, dict):
            result.problems.append(
                f"{beat_id}: expected an object, found a "
                f"{type(item).__name__}.")
            continue

        beat: dict = {"beat_id": beat_id}
        if _text(item.get("beat_id")):
            beat["beat_id"] = _text(item["beat_id"])
        else:
            filled["beat_id"] = filled.get("beat_id", 0) + 1

        for name in REQUIRED_FIELDS:
            value = _text(item.get(name))
            if not value:
                result.problems.append(
                    f"{beat_id}: {name} is missing or empty.")
            beat[name] = value

        latin = latin_words(beat.get("voice_text", ""))
        if latin:
            result.problems.append(
                f"{beat_id}: voice_text has Latin script in it "
                f"({', '.join(latin[:6])}). The voice reads those as "
                f"English and mispronounces them — write the sound in "
                f"Devanagari and leave the Roman spelling in "
                f"caption_text.")

        total_words += len(beat.get("voice_text", "").split())
        beat["on_screen_text"] = _text(item.get("on_screen_text")) or None
        beat["sticker"] = _sticker(item.get("sticker"))

        for name, vocabulary, default in (
                ("role", facts["roles"],
                 roles[index] if index < len(roles) else "setup"),
                ("motion", facts["motions"], facts["motions"][0]),
                ("transition", facts["transitions"],
                 facts["transitions"][0])):
            given = _text(item.get(name))
            if not given:
                beat[name] = default
                filled[name] = filled.get(name, 0) + 1
            elif given not in vocabulary:
                result.problems.append(
                    f"{beat_id}: {name} is {given!r}, which is not one of "
                    f"{', '.join(vocabulary)}.")
                beat[name] = default
            else:
                beat[name] = given

        result.beats.append(beat)

    result.words = total_words
    if not (facts["word_min"] <= total_words <= facts["word_max"]):
        result.problems.append(
            f"the script is {total_words} spoken words and this engine "
            f"accepts {facts['word_min']} to {facts['word_max']} "
            f"(target {facts['word_budget']}). At "
            f"{facts['speech_rate']} words a second that would run about "
            f"{total_words / facts['speech_rate']:.0f}s, against a "
            f"{facts['duration_min']:.0f}-{facts['duration_max']:.0f}s "
            f"window.")

    for name, n in sorted(filled.items()):
        result.filled.append(f"{name} filled on {n} beat(s)")

    return result

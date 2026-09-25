"""What the copyable prompt asks for, against what the importer accepts.

``read_script_json`` has always read ``on_screen_text`` and ``sticker``
off a pasted beat. The prompt never mentioned either, so no model ever
sent them and both arrived empty on every hand-written script -- the
punch line and the sticker cue had to be typed in afterwards, one beat
at a time, for a feature the importer already supported.

The other half is vocabulary. The prompt said "Devanagari script only",
which a model reads as "write literary Hindi": it returns विज्ञापन for
ads and परिणाम for results. That is not how the channel speaks, and
the fix is the rule the repair pass already states elsewhere -- English
words stay English words, transliterated rather than translated
(English -> इंग्लिश, ads -> ऐड्स).

The test that matters most here is the round trip: whatever the prompt
shows as its example has to survive ``read_script_json`` with the punch
and the sticker still on the beat. A prompt and a parser that disagree
is the same bug in two files.
"""

from __future__ import annotations

import json
import re

import pytest

from engine.authoring import authoring_facts, build_prompt, read_script_json
from engine.config import Settings


@pytest.fixture()
def settings():
    return Settings()


@pytest.fixture()
def facts(settings):
    return authoring_facts(settings, topic_max=120)


def _example(text: str) -> dict:
    """The JSON block the prompt shows, parsed back out of it."""
    match = re.search(r"\{[\s\S]*?\n\}", text)
    assert match, "the prompt shows no JSON example"
    return json.loads(match.group(0))


# --- the two fields nobody was ever asked for -------------------------------


def test_the_example_shows_the_punch_line(facts):
    beat = _example(build_prompt("x", facts))["beats"][0]

    assert "on_screen_text" in beat


def test_the_example_shows_the_sticker_cue(facts):
    beat = _example(build_prompt("x", facts))["beats"][0]

    assert "sticker" in beat
    assert isinstance(beat["sticker"], dict)
    assert "word" in beat["sticker"] and "terms" in beat["sticker"]


def test_both_are_explained_not_just_shown(facts):
    text = build_prompt("x", facts).lower()

    assert "on_screen_text" in text
    assert "sticker" in text


def test_the_sticker_word_must_come_from_the_caption(facts):
    """A sticker sits on a word's timing, so a word that is not in
    caption_text has no clock to sit on."""
    text = build_prompt("x", facts).lower()

    assert "caption_text" in text
    assert re.search(r"sticker[\s\S]{0,400}caption_text", text), (
        "nothing ties the sticker's word to the caption")


def test_the_sticker_cap_comes_from_settings(facts, settings):
    """Interpolated like every other number here, so changing
    RAHASYA_STICKER_MAX changes what the other tool is told."""
    text = build_prompt("x", facts)

    assert str(settings.sticker_max) in text
    assert facts["sticker_max"] == settings.sticker_max


def test_both_fields_are_optional(facts):
    """Not every beat earns a punch or a sticker, and a script that
    forces one onto all ten is worse than one that uses neither."""
    text = build_prompt("x", facts).lower()

    assert "optional" in text or "omit" in text or "leave it out" in text


# --- Hinglish, not Hindi ----------------------------------------------------


def test_the_prompt_says_english_words_stay_english(facts):
    """"Devanagari only" reads as "write in Hindi", and the model
    returns विज्ञापन where the channel would say ऐड्स."""
    text = build_prompt("x", facts)

    assert "transliterat" in text.lower(), (
        "nothing tells the model to spell English words, not translate "
        "them")


def test_the_prompt_shows_what_transliteration_looks_like(facts):
    """A rule with no example is a rule a model applies to the word
    'Devanagari' and not to its output."""
    text = build_prompt("x", facts)

    assert any(w in text for w in ("इंग्लिश", "ऐड्स", "डीएनए")), (
        "no worked example of an English word in Devanagari")


def test_the_prompt_warns_against_literary_hindi(facts):
    text = build_prompt("x", facts).lower()

    assert any(w in text for w in ("literary", "pure hindi", "shuddh",
                                   "formal hindi")), (
        "nothing warns the model off the register that produced "
        "विज्ञापन")


def test_the_script_rule_itself_is_unchanged(facts):
    """Devanagari is still required -- the voice mispronounces Latin
    letters. Only the vocabulary guidance is new."""
    text = build_prompt("x", facts)

    assert "Devanagari" in text


# --- prompt and parser agree ------------------------------------------------


def test_the_prompts_own_example_imports_with_both_fields(facts, settings):
    """The round trip. A prompt that asks for a shape the importer drops
    is the same bug written twice."""
    example = _example(build_prompt("Kuldhara", facts))
    beat = dict(example["beats"][0])
    beat["voice_text"] = "रात के तीन बजे दरवाज़ा खुला"
    beat["caption_text"] = "raat ke teen baje darwaza khula"
    beat["visual_prompt"] = "A wooden door opening in a dark room."
    beat["on_screen_text"] = "3 baje"
    beat["sticker"] = {"word": "darwaza", "terms": ["door"]}

    result = read_script_json(
        json.dumps({"topic": "Kuldhara",
                    "beats": [beat] * facts["beat_count"]},
                   ensure_ascii=False),
        facts, settings)

    assert result.beats, result.problems
    first = result.beats[0]
    assert first["on_screen_text"] == "3 baje"
    assert first["sticker"] == {"word": "darwaza", "terms": ["door"]}


def test_a_script_without_them_still_imports(facts, settings):
    """Optional means optional: the fields were absent for every script
    written before this, and those must keep importing."""
    beat = {"voice_text": "रात के तीन बजे दरवाज़ा खुला",
            "caption_text": "raat ke teen baje darwaza khula",
            "visual_prompt": "A wooden door opening in a dark room."}

    result = read_script_json(
        json.dumps({"topic": "x", "beats": [beat] * facts["beat_count"]},
                   ensure_ascii=False),
        facts, settings)

    assert result.beats, result.problems
    assert result.beats[0]["on_screen_text"] is None
    assert result.beats[0]["sticker"] is None

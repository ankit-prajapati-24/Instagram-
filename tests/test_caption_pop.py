r"""Lighting the active caption word, without moving the block.

The karaoke highlight has always been there -- ``\k`` per word, white to
gold. A reviewer watching a finished reel still read the captions as
"static paragraphs", and at Hinglish speaking speed that is fair: a
colour swap on a 72px word lasting a third of a second is easy to miss.

The first attempt grew the word, and shipped a reel whose caption block
jumped up and down. Scaling both axes reflows the line sideways, which
is obvious; what is not obvious is that ``\fscy`` alone grows the *line
box*, so a block anchored at the bottom walks up the frame. Measured on
real captions rendered over black, sampling through one beat:

    variant  block top moves   lit pixels min..max
    none                 0px   42372..42962
    fscy                72px   42372..54406   <- the jumping
    glow                 2px   42372..85289

A glow changes only how a glyph is painted, so the metrics cannot move;
the 2px is blur bleeding past the top row, not the text. It is also the
more visible of the two -- twice the lit pixels of a plain line, where
the scale managed a quarter more.

The version of this file that shipped the jumping tested that the line
did not move *sideways* and called that "no reflow". The vertical was
the axis that broke, and nothing here looked at it.

**Per-word ``\t`` really does target one word.** Rendered three words
each emphasised during its own second and counted lit pixels:

           peak    settled     plain
    w1    15459      12094     12094
    w2    14121      11997     11997
    w3    12752      11906     11906

Each peaks in its own window and the settled frames come back
byte-identical to a plain line, so nothing leaks onto the words after
it.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.captions import (COLOUR_OUTLINE, HIGHLIGHT_BLUR,
                                      HIGHLIGHT_GLOW, HIGHLIGHT_RISE_MS,
                                      _karaoke_line, build_ass)
from engine.contract import WordTiming
from tests.factories import make_plan


def _beat(line="Kya tumhein pata hai", seconds=2.4):
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = line
    beat.measured_seconds = seconds
    beat.words = caption_timings(line, seconds)
    return plan, beat


# --- the emphasis -----------------------------------------------------------


def test_every_word_gets_its_own_emphasis():
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count("\\t(") == len(beat.words) * 2, (
        "each word needs a rise and a settle")


def test_nothing_about_the_glyph_metrics_changes():
    """The defect this replaced. Any size change moves the line box, and
    a bottom-anchored caption block then walks up the frame -- measured
    at 72px of travel."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert "\\blur" in body
    assert "\\fscy" not in body, "fscy grows the line box: 72px of jump"
    assert "\\fscx" not in body, "fscx reflows the line sideways"
    assert "\\fs" not in body, "font size moves the line box too"


def test_the_word_goes_dark_again():
    """A word left lit stays lit for the rest of the line, and by the
    end every word is shouting."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count(f"\\blur{HIGHLIGHT_BLUR}") == len(beat.words)
    assert body.count("\\blur0") == len(beat.words)
    assert body.count(f"\\3c{COLOUR_OUTLINE}") == len(beat.words), (
        "the outline must return to the style's own colour")


def test_the_emphasis_is_timed_to_the_word_not_the_line():
    """The whole point: the word being spoken is the one that lights."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")
    rises = [int(m) for m in
             re.findall(r"\\t\((\d+),\d+,\\3c" + re.escape(HIGHLIGHT_GLOW),
                        body)]

    assert len(rises) == len(beat.words)
    assert rises == sorted(rises)
    assert rises[0] == 0
    assert rises[-1] == pytest.approx(
        int(beat.words[-1].start * 1000), abs=2)


def test_the_karaoke_colour_sweep_survives():
    """The glow is added to the highlight, not swapped for it."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count("\\k") >= len(beat.words)


# --- the awkward cases ------------------------------------------------------


def test_a_word_shorter_than_the_rise_still_settles():
    """"ye" at speaking speed can be under 100ms. A rise that outran the
    word would leave it lit while the next one lit too."""
    _, beat = _beat()
    beat.words = [WordTiming(word="ye", start=0.0, end=0.05),
                  WordTiming(word="baat", start=0.05, end=1.0)]

    body = _karaoke_line(beat, "caption_text")
    times = [(int(a), int(b)) for a, b in
             re.findall(r"\\t\((\d+),(\d+),", body)]

    assert all(start <= end for start, end in times)
    assert times[0][1] <= 50, "the rise outran the word it belongs to"


def test_a_beat_with_no_timings_is_left_plain():
    """Before VOICE there is nothing to time an emphasis against."""
    _, beat = _beat()
    beat.words = []

    body = _karaoke_line(beat, "caption_text")

    assert "\\t(" not in body
    assert "\\k" not in body


def test_devanagari_captions_are_left_plain():
    """voice_text has a different word count from the Roman timings, so
    tagging it would light the wrong syllables."""
    _, beat = _beat()

    body = _karaoke_line(beat, "voice_text")

    assert "\\t(" not in body


def test_the_emphasis_can_be_turned_off(monkeypatch):
    import engine.assembly.captions as captions

    monkeypatch.setattr(captions, "HIGHLIGHT_BLUR", 0)
    _, beat = _beat()

    assert "\\t(" not in captions._karaoke_line(beat, "caption_text")


# --- the document still parses ---------------------------------------------


def test_the_whole_document_still_builds():
    plan, _ = _beat()

    text = build_ass(plan)

    assert "[Events]" in text
    assert "\\blur" in text
    assert text.count("Dialogue:") >= 1


def test_braces_in_the_caption_cannot_break_the_tags():
    """Override blocks are brace-delimited, so an unescaped brace in the
    text would end the block early and dump tags on screen."""
    plan, beat = _beat(line="kya {yeh} sach hai")

    body = _karaoke_line(beat, "caption_text")

    assert "\\{" in body and "\\}" in body
    assert HIGHLIGHT_RISE_MS > 0

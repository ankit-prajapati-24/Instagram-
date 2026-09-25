"""Making the active caption word pop, not just change colour.

The karaoke highlight has always been there -- ``\\k`` per word, white
to gold. A reviewer watching a finished reel still read the captions as
"static paragraphs", and at Hinglish speaking speed that is fair: a
colour swap on a 72px word lasting a third of a second is easy to miss.

So the active word also grows. Two things were measured before choosing
how.

**Per-word ``\\t`` really does target one word.** Rendered three words
each scaled during its own second, and counted bright pixels in the
caption band:

           peak    settled     plain
    w1    15459      12094     12094
    w2    14121      11997     11997
    w3    12752      11906     11906

Each word peaks in its own window and the settled frames come back
byte-identical to the unscaled line, so nothing leaks onto the words
after it.

**Only the height grows.** Scaling both axes reflows a centred line and
shoves every other word sideways, which reads as jitter. Measured at one
word's peak, against the same line unscaled:

    plain  x 221-859 (w 638)  h 59
    both   x 144-934 (w 790)  h 76   <- whole line moved
    tall   x 221-859 (w 638)  h 76   <- identical x, 29% taller

``\\fscy`` changes no advance widths, so the line cannot reflow. That is
why the pop is vertical only and not a matter of taste.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.captions import (HIGHLIGHT_RISE_MS, HIGHLIGHT_SCALE_Y,
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


# --- the pop ----------------------------------------------------------------


def test_every_word_gets_its_own_pop():
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count("\\t(") == len(beat.words) * 2, (
        "each word needs a rise and a settle")


def test_the_pop_grows_the_height_only():
    """Scaling the width reflows a centred line; measured, it moved the
    whole line 152px wider."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert "\\fscy" in body
    assert "\\fscx" not in body, "fscx reflows the line into jitter"


def test_the_word_comes_back_down():
    """A word left large stays large for the rest of the line, and by
    the end every word is shouting."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count(f"\\fscy{HIGHLIGHT_SCALE_Y}") == len(beat.words)
    assert body.count("\\fscy100") == len(beat.words)


def test_the_pop_is_timed_to_the_word_not_the_line():
    """The whole point: the word being spoken is the one that moves."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")
    rises = [int(m) for m in re.findall(r"\\t\((\d+),\d+,\\fscy1[1-9]", body)]

    assert rises == sorted(rises)
    assert rises[0] == 0
    assert rises[-1] == pytest.approx(
        int(beat.words[-1].start * 1000), abs=2)


def test_the_karaoke_colour_sweep_survives():
    """The pop is added to the highlight, not swapped for it."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count("\\k") >= len(beat.words)


# --- the awkward cases ------------------------------------------------------


def test_a_word_shorter_than_the_rise_still_settles():
    """"ye" at speaking speed can be under 100ms. A rise that outran the
    word would leave it large while the next one popped too."""
    _, beat = _beat()
    beat.words = [WordTiming(word="ye", start=0.0, end=0.05),
                  WordTiming(word="baat", start=0.05, end=1.0)]

    body = _karaoke_line(beat, "caption_text")
    times = [(int(a), int(b)) for a, b in
             re.findall(r"\\t\((\d+),(\d+),", body)]

    assert all(start <= end for start, end in times)
    assert times[0][1] <= 50, "the rise outran the word it belongs to"


def test_a_beat_with_no_timings_is_left_plain():
    """Before VOICE there is nothing to time a pop against."""
    _, beat = _beat()
    beat.words = []

    body = _karaoke_line(beat, "caption_text")

    assert "\\t(" not in body
    assert "\\k" not in body


def test_devanagari_captions_are_left_plain():
    """voice_text has a different word count from the Roman timings, so
    tagging it would put the pop on the wrong syllables."""
    _, beat = _beat()

    body = _karaoke_line(beat, "voice_text")

    assert "\\t(" not in body


def test_the_pop_can_be_turned_off(monkeypatch):
    import engine.assembly.captions as captions

    monkeypatch.setattr(captions, "HIGHLIGHT_SCALE_Y", 100)
    _, beat = _beat()

    assert "\\t(" not in captions._karaoke_line(beat, "caption_text")


# --- the document still parses ---------------------------------------------


def test_the_whole_document_still_builds():
    plan, _ = _beat()

    text = build_ass(plan)

    assert "[Events]" in text
    assert "\\fscy" in text
    assert text.count("Dialogue:") >= 1


def test_braces_in_the_caption_cannot_break_the_tags():
    """Override blocks are brace-delimited, so an unescaped brace in the
    text would end the block early and dump tags on screen."""
    plan, beat = _beat(line="kya {yeh} sach hai")

    body = _karaoke_line(beat, "caption_text")

    assert "\\{" in body and "\\}" in body
    assert HIGHLIGHT_RISE_MS > 0

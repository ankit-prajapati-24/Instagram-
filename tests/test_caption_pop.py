r"""The karaoke highlight, and the emphasis that is not switched on.

Captions sweep white to gold as each word is spoken -- ``\k`` per word,
the style's secondary colour flipping to its primary. A reviewer
watching a finished reel called that "static paragraphs", and three
attempts were made to add something on top of it. Each one was visible
to a viewer as a fault:

* ``\fscy 130`` grew the *line box*, not just the glyph, so a block
  anchored at the bottom walked 83px up the frame. Reported as the
  captions moving up and down.
* A white glow. White already means "not said yet" in this palette, so
  every word flashed it on the way to gold. Counted through one word's
  onset, white pixels went 3387 -> 6435 -> 9804 -> 3540 where the plain
  sweep held flat. Reported as the captions blinking white.
* A gold glow. No blink, but the outline is 5px and already gold, so
  any blur strong enough to notice fills the counters -- at the peak of
  one word on real footage, "rehti" read as a gold blob.

So the sweep ships on its own, and ``HIGHLIGHT_BLUR`` is 0. The
machinery stays because it is sound: a thinner outline, or one that is
not the text colour, would make a glow work. These tests cover both --
what the tags do when the knob is turned up, and the fact that it is
down.

The test that let the first two through asserted the line did not move
*sideways*. It did, correctly, and said nothing about the axis that
broke or the colour that clashed.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.captions import (COLOUR_OUTLINE, COLOUR_SPOKEN,
                                      COLOUR_UPCOMING, HIGHLIGHT_GLOW,
                                      HIGHLIGHT_RISE_MS, _karaoke_line,
                                      build_ass)
from engine.contract import WordTiming
from tests.factories import make_plan

GLOW_BLUR = 2      # what the knob is tested at, not what ships


@pytest.fixture()
def glowing(monkeypatch):
    """The emphasis turned up, so the tags can be examined at all."""
    import engine.assembly.captions as captions

    monkeypatch.setattr(captions, "HIGHLIGHT_BLUR", GLOW_BLUR)
    return captions


def _beat(line="Kya tumhein pata hai", seconds=2.4):
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = line
    beat.measured_seconds = seconds
    beat.words = caption_timings(line, seconds)
    return plan, beat


# --- what ships -------------------------------------------------------------


def test_the_sweep_alone_is_what_ships():
    """Three added effects, three viewer-visible faults. The knob is
    down until the palette that defeats it changes."""
    from engine.config import Settings  # noqa: F401  (import kept cheap)
    import engine.assembly.captions as captions

    assert captions.HIGHLIGHT_BLUR == 0


def test_with_the_knob_down_no_word_carries_an_effect():
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert "\\t(" not in body
    assert "\\blur" not in body


def test_the_sweep_itself_is_untouched():
    """The highlight that works: one \\k per word, white to gold."""
    _, beat = _beat()

    body = _karaoke_line(beat, "caption_text")

    assert body.count("\\k") == len(beat.words)
    assert COLOUR_SPOKEN != COLOUR_UPCOMING


# --- the knob, when it is turned up -----------------------------------------


def test_every_word_gets_its_own_emphasis(glowing):
    _, beat = _beat()

    body = glowing._karaoke_line(beat, "caption_text")

    assert body.count("\\t(") == len(beat.words) * 2, (
        "each word needs a rise and a settle")


def test_nothing_about_the_glyph_metrics_changes(glowing):
    """The first fault. Any size change moves the line box, and a
    bottom-anchored caption block then walks up the frame -- measured at
    83px on a rendered reel."""
    _, beat = _beat()

    body = glowing._karaoke_line(beat, "caption_text")

    assert "\\blur" in body
    assert "\\fscy" not in body, "fscy grows the line box: 83px of travel"
    assert "\\fscx" not in body, "fscx reflows the line sideways"
    assert "\\fs" not in body, "font size moves the line box too"


def test_the_glow_is_not_the_colour_of_an_unspoken_word():
    """The second fault. White is load-bearing here: it is what a word
    looks like before it is said, so lighting the active word white made
    every word flash the "not yet" colour on its way to gold."""
    assert HIGHLIGHT_GLOW != COLOUR_UPCOMING


def test_the_glow_agrees_with_what_spoken_already_looks_like():
    """One accent colour for one meaning. A third would be a third
    thing to decode in a third of a second."""
    assert HIGHLIGHT_GLOW == COLOUR_SPOKEN


def test_the_word_goes_dark_again(glowing):
    """A word left lit stays lit for the rest of the line, and by the
    end every word is shouting."""
    _, beat = _beat()

    body = glowing._karaoke_line(beat, "caption_text")

    assert body.count(f"\\blur{GLOW_BLUR}") == len(beat.words)
    assert body.count("\\blur0") == len(beat.words)
    assert body.count(f"\\3c{COLOUR_OUTLINE}") == len(beat.words), (
        "the outline must return to the style's own colour")


def test_the_emphasis_is_timed_to_the_word_not_the_line(glowing):
    """The whole point: the word being spoken is the one that lights."""
    _, beat = _beat()

    body = glowing._karaoke_line(beat, "caption_text")
    rises = [int(m) for m in
             re.findall(r"\\t\((\d+),\d+,\\3c" + re.escape(HIGHLIGHT_GLOW),
                        body)]

    assert len(rises) == len(beat.words)
    assert rises == sorted(rises)
    assert rises[0] == 0
    assert rises[-1] == pytest.approx(
        int(beat.words[-1].start * 1000), abs=2)


def test_a_word_shorter_than_the_rise_still_settles(glowing):
    """"ye" at speaking speed can be under 100ms. A rise that outran the
    word would leave it lit while the next one lit too."""
    _, beat = _beat()
    beat.words = [WordTiming(word="ye", start=0.0, end=0.05),
                  WordTiming(word="baat", start=0.05, end=1.0)]

    body = glowing._karaoke_line(beat, "caption_text")
    times = [(int(a), int(b)) for a, b in
             re.findall(r"\\t\((\d+),(\d+),", body)]

    assert all(start <= end for start, end in times)
    assert times[0][1] <= 50, "the rise outran the word it belongs to"


# --- the awkward cases ------------------------------------------------------


def test_a_beat_with_no_timings_is_left_plain(glowing):
    """Before VOICE there is nothing to time anything against."""
    _, beat = _beat()
    beat.words = []

    body = glowing._karaoke_line(beat, "caption_text")

    assert "\\t(" not in body
    assert "\\k" not in body


def test_devanagari_captions_are_left_plain(glowing):
    """voice_text has a different word count from the Roman timings, so
    tagging it would light the wrong syllables."""
    _, beat = _beat()

    body = glowing._karaoke_line(beat, "voice_text")

    assert "\\t(" not in body


# --- the document still parses ---------------------------------------------


def test_the_whole_document_still_builds():
    plan, _ = _beat()

    text = build_ass(plan)

    assert "[Events]" in text
    assert "\\k" in text
    assert text.count("Dialogue:") >= 1


def test_braces_in_the_caption_cannot_break_the_tags():
    """Override blocks are brace-delimited, so an unescaped brace in the
    text would end the block early and dump tags on screen."""
    plan, beat = _beat(line="kya {yeh} sach hai")

    body = _karaoke_line(beat, "caption_text")

    assert "\\{" in body and "\\}" in body
    assert HIGHLIGHT_RISE_MS > 0

r"""The caption document, built from a Look.

`build_ass` took font, size and margin as separate arguments and
`pipeline.render_stage` passed three of them from three places. One
object replaces all of it, which is what makes a picker possible: there
is now a single value to store and a single value to preview.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.captions import build_ass
from engine.assembly.looks import PRESETS, resolve
from tests.factories import make_plan


def _plan():
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = "Kya tumhein pata hai"
    beat.on_screen_text = "Not Enough"
    beat.measured_seconds = 2.4
    beat.words = caption_timings(beat.caption_text, 2.4)
    return plan


def _style(text: str, name: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith(f"Style: {name},"):
            return line.split(",")
    raise AssertionError(f"no {name} style in the document")


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_the_document_carries_the_looks_own_values(look_id):
    look = resolve(look_id)

    text = build_ass(_plan(), look=look)

    style = _style(text, "Default")
    assert style[1] == look.font
    assert style[2] == str(look.caption_size)
    assert style[3] == look.spoken
    assert style[4] == look.upcoming
    assert style[-2] == str(look.margin_v)


def test_the_punch_uses_its_own_font_and_size():
    look = resolve("blocky-urban")

    style = _style(build_ass(_plan(), look=look), "Punch")

    assert style[1] == look.punch_font
    assert style[2] == str(look.punch_size)


def test_the_punch_carries_its_looks_animation():
    text = build_ass(_plan(), look=resolve("blocky-urban"))

    punch = [l for l in text.splitlines() if ",Punch," in l]
    assert punch, "no punch line in the document"
    assert "\\alpha" in punch[0], "the letters animation did not reach it"


def test_no_look_means_the_default():
    assert build_ass(_plan()) == build_ass(_plan(), look=resolve(None))


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_no_look_puts_a_metric_change_on_the_caption(look_id):
    """The rule the three shipped faults each broke. The punch may
    scale; the caption line may not."""
    text = build_ass(_plan(), look=resolve(look_id))

    for line in text.splitlines():
        if line.startswith("Dialogue:") and ",Default," in line:
            assert "\\fscx" not in line
            assert "\\fscy" not in line
            assert re.search(r"\\fs\d", line) is None

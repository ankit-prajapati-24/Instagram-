"""What stops a sticker from simply being made bigger.

A reviewer watching a finished reel called the stickers too small to
register -- 0.17 of frame width is 184px on a 1080 frame -- and asked
for 300-350px. Changing the number is one line. These are the two
things that have to move with it, written down because neither is
visible from the constant itself and one of them fails *silently*.

**1. The baked art is matched by exact size.** ``baked_sequence``
accepts a cached sequence only when its recorded size equals the size
being asked for. The five committed concepts under
``engine/data/stickers`` are baked at size 184 / canvas 232, so a
request for anything else matches nothing and every one of them falls
through to its emoji fallback -- no error, no warning, just plainer
stickers and a lost Lordicon credit. Raising the fraction means
re-baking, and at 0.30 that is roughly three times the pixels of the
current 11MB of committed art.

**2. There is a ceiling, and the pop sets it.** A sticker grows to
``OVERSHOOT`` -- 1.25x -- on the way in, and that peak is the frame it
can escape on. Above it the platform draws its own controls over
roughly the top 200px; below it the Punch caption style is Alignment 5
with MarginV 0, which is dead centre, about y 900. Measured peak boxes:

     frac  rest  peak   peak box y
     0.17   184   230   307-537   <- shipped
     0.30   324   405   220-624   <- the largest that still clears
     0.34   366   457   194-651   <- into the chrome

So 0.30 is reachable, and the review's own suggestion -- centre the
sticker, ``overlay=(H-h)/2-180`` -- is not: that is where the Punch
caption already is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.assembly.stickers import (ANCHOR_X_FRACTIONS, BAKED_ROOT,
                                      OVERSHOOT, SIZE_FRACTION,
                                      sticker_anchor, sticker_canvas,
                                      sticker_size)

WIDTH, HEIGHT = 1080, 1920

# Where the platform's own controls stop, and where the centred Punch
# caption starts. Both read off the shipped caption styles and the usual
# Reels/Shorts overlay rather than guessed.
CHROME_BOTTOM = 200
PUNCH_TOP = 900

# The largest fraction whose *peak* still clears the chrome, measured.
CEILING = 0.30


def _peak_box(slot: int, fraction: float = SIZE_FRACTION):
    """The sticker at the top of its pop, as ``(left, top, right, bottom)``."""
    peak = int(sticker_size(WIDTH, fraction) * OVERSHOOT)
    cx, cy = sticker_anchor(slot, WIDTH, HEIGHT)
    return cx - peak // 2, cy - peak // 2, cx + peak // 2, cy + peak // 2


def _baked_metas():
    return sorted(BAKED_ROOT.glob("*/*/meta.json"))


# --- the silent one ---------------------------------------------------------


def test_the_two_default_sizes_agree():
    """SIZE_FRACTION and Settings.sticker_scale both answer "how big",
    and a render reads whichever it can reach. Disagreeing is invisible:
    a size the committed bake does not cover is baked on demand rather
    than failing, so the only symptom would be two code paths quietly
    producing different-sized stickers."""
    from engine.config import Settings

    assert SIZE_FRACTION == Settings().sticker_scale


def test_a_size_the_committed_art_misses_is_baked_rather_than_dropped():
    """This used to be the silent defect: baked_sequence matches by
    exact size, so any scale but the committed one dropped every
    designed concept to its emoji and stopped owing Lordicon a credit.
    It is covered now, and this is the wire that covers it."""
    from engine.assembly import sticker_bake
    from engine.assembly import stickers as stk_mod
    import inspect

    assert hasattr(sticker_bake, "bake_on_demand")
    assert "bake_on_demand" in inspect.getsource(stk_mod.prepare)


def test_every_baked_concept_agrees_on_one_size():
    """Two concepts baked at different sizes would mean one of them is
    always falling through, whichever size is configured."""
    sizes = {json.loads(p.read_text(encoding="utf-8"))["canvas"]
             for p in _baked_metas()}

    assert len(sizes) == 1, f"the committed art is baked at {sizes}"


# --- the ceiling ------------------------------------------------------------


@pytest.mark.parametrize("slot", range(len(ANCHOR_X_FRACTIONS)))
def test_the_pop_never_reaches_the_platform_chrome(slot):
    """The peak, not the resting size: a sticker that fits at rest can
    still punch into the chrome on the way in."""
    _, top, _, _ = _peak_box(slot)

    assert top >= CHROME_BOTTOM


@pytest.mark.parametrize("slot", range(len(ANCHOR_X_FRACTIONS)))
def test_the_pop_never_reaches_the_centred_caption(slot):
    """The Punch style is Alignment 5 -- dead centre. Putting a sticker
    there, as the review suggested, would cover the line it punctuates."""
    _, _, _, bottom = _peak_box(slot)

    assert bottom <= PUNCH_TOP


@pytest.mark.parametrize("slot", range(len(ANCHOR_X_FRACTIONS)))
def test_the_pop_stays_inside_the_frame_sideways(slot):
    left, _, right, _ = _peak_box(slot)

    assert left >= 0
    assert right <= WIDTH


def test_the_ceiling_is_where_it_was_measured():
    """0.30 clears; the next step up does not. Both directions, so the
    number is pinned rather than merely asserted to be safe."""
    _, top_at_ceiling, _, _ = _peak_box(0, fraction=CEILING)
    _, top_above, _, _ = _peak_box(0, fraction=0.34)

    assert top_at_ceiling >= CHROME_BOTTOM
    assert top_above < CHROME_BOTTOM


def test_the_shipped_size_is_under_the_ceiling():
    assert SIZE_FRACTION <= CEILING


def test_the_size_is_a_fraction_of_the_frame_not_a_pixel_count():
    """The renderer's frame size is configurable, and a hard pixel
    number would stop being the same share of the width."""
    assert sticker_size(540) == pytest.approx(sticker_size(1080) / 2, abs=2)

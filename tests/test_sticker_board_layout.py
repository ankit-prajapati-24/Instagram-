"""The sticker board: one search on top, and cards that fit their column.

Both boards lay their cards out as ``<figure>``, and a browser gives a
figure 40px of side margin by default. Neither reset it, so every card
was 80px narrower than the grid track it sat in. Measured in a browser
against the real panel:

                  track    card
    sticker       174px     94px
    clip          221px    141px

That one number is why the sticker board looked broken. In a 94px card
a 44px icon fits once per row, so eight candidates became an eight-row
column 394px tall inside a 637px card; and the per-card search input,
with its 120px min-width, could not fit at all and hung 35px outside
its own card -- the overlap. After the reset:

                  track    card    card height
    sticker       174px    174px    637 -> 288px
    clip          221px    221px

Searching then moved above the board, mirroring the clip search. Per
card it meant running the same search once per sticker, in a box too
narrow to read what you had typed. One box now serves every cue, and
each result carries a dropdown of the cues that fired, so a single
search can answer several stickers.

The browser measurements live in the commit; what these hold is the
structure those measurements depend on.
"""

from __future__ import annotations

import re

import pytest

import engine.app as app_mod

PAGE = (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")


def _styles() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", PAGE, re.S))


# --- the root cause ---------------------------------------------------------


@pytest.mark.parametrize("card", [".clipcard", ".stickercard"])
def test_a_card_does_not_keep_the_figures_default_margin(card):
    """80px of browser default, silently eating the grid track."""
    styles = _styles()
    reset = re.search(
        r"([^{}]*\.(?:clipcard|stickercard)[^{}]*)\{([^}]*margin:\s*0[^}]*)\}",
        styles)

    assert reset, "no margin reset for the card figures"
    assert card in reset.group(1), f"{card} is not covered by the reset"


def test_the_cards_are_still_figures():
    """If they stop being figures the reset is dead weight, and this
    test is how anyone finds that out."""
    assert 'class="clipcard' in PAGE
    assert 'class="stickercard' in PAGE
    assert "<figure class=\"clipcard" in PAGE or "figure class=\"clipcard" \
        in PAGE


# --- one search, not one per card -------------------------------------------


def test_there_is_exactly_one_sticker_search_box():
    """Per card it was the same search typed once per sticker, in a box
    35px wider than the card holding it."""
    assert PAGE.count('id="stk-q"') == 1
    assert "stickerq-" not in PAGE, "a per-card search input survives"
    assert "stickerfind" not in PAGE, "the per-card search styling survives"


def test_the_old_per_card_search_function_is_gone():
    """Dead code that still looks callable is worse than none."""
    assert "searchStickers(" not in PAGE


def test_the_search_lives_above_the_board():
    """Reading order is the point: search, then results, then what you
    already have."""
    assert PAGE.index('id="stk-q"') < PAGE.index('id="stickergrid"')


# --- a result can go on any cue ---------------------------------------------


def test_every_result_offers_the_cues_to_replace():
    """The "replace" half. Without it a search can only answer the cue
    it was run from, which is what the per-card box already did."""
    assert "stickerCueOptions" in PAGE
    assert "stkaim-" in PAGE, "no per-result cue selector"


def test_choosing_from_the_search_goes_through_the_normal_choose():
    """One path to the bake, so the busy state, the 422 handling and the
    'in use now' line all keep working."""
    assert "useStickerHere" in PAGE
    assert "chooseSticker(beatId, slug)" in PAGE


def test_the_search_route_comes_from_the_row_not_the_page():
    """The row carries `search`; hardcoding it here would be the panel
    inventing a URL the server already provides."""
    assert "rows[0].search" in PAGE
    assert "/api/sticker-search" not in PAGE


def test_a_searched_icon_is_added_to_the_cards_own_strip():
    """chooseSticker marks the thumbnail whose slug matches, and an icon
    found in the top search is not in that card's strip yet -- without
    this the choice lands but the card never shows it as chosen."""
    assert "LAST_STICKER_HITS" in PAGE
    assert "row.candidates" in PAGE

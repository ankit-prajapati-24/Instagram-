r"""The look picker on the clip gate.

Structure only -- the geometry is measured in a browser, not asserted
here. What these hold is that the panel reads its URLs from the server
rather than building them, which an earlier sticker test got backwards:
it looked for the literal route in index.html, which would have passed
only if the panel hardcoded it.
"""

from __future__ import annotations

import re

import engine.app as app_mod

PAGE = (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")


def _styles() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", PAGE, re.S))


def test_there_is_a_look_block_on_the_clip_gate():
    assert 'id="lookgrid"' in PAGE
    assert PAGE.index('id="lookgrid"') < PAGE.index('id="btn-release"')


def test_each_preset_tile_plays_its_own_preview():
    """A still cannot show an animation, which is half the choice."""
    assert "lookTiles" in PAGE
    assert "<video" in PAGE


def test_the_preview_url_comes_from_the_server():
    """The row carries it; a panel that spelled the route out itself
    would be inventing a URL the server already provides."""
    assert "/api/look-preview/" not in PAGE
    assert re.search(r"\.preview\b", PAGE)


def test_the_custom_panel_offers_the_three_parts():
    assert 'id="look-font"' in PAGE
    assert 'id="look-anim"' in PAGE
    assert 'id="look-colour"' in PAGE


def test_the_custom_choices_come_from_the_server_not_the_page():
    """fonts and animations are listed by /looks; hardcoding them here
    is how the panel offers a font the render cannot use."""
    assert "data.fonts" in PAGE or "LOOKS.fonts" in PAGE
    assert "data.animations" in PAGE or "LOOKS.animations" in PAGE


def test_a_look_can_be_cleared_from_the_panel():
    assert "clearLook" in PAGE


def test_the_look_cards_are_not_left_with_the_figure_margin():
    """Both other boards lost 80px a card to the browser's default
    figure margin before it was reset. A new card grid must not
    reintroduce it."""
    styles = _styles()
    reset = re.search(r"([^{}]*\.lookcard[^{}]*)\{([^}]*margin:\s*0[^}]*)\}",
                      styles)

    assert reset, "no margin reset for the look cards"

r"""One object holding every caption style decision.

The style was six loose arguments threaded from four places, which is
why nobody ever changed the font: there was no one thing to change. A
Look is that thing, and the presets are the answers already chosen.

`plain` is what ships today, so adding this changes nothing until
something is picked.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.looks import (ANIMATIONS, DEFAULT_LOOK_ID, PRESETS,
                                   punch_tags, resolve)


def test_the_default_is_what_ships_today():
    """Turning this on must not restyle anything by itself."""
    look = resolve(DEFAULT_LOOK_ID)

    assert look.font == "Arial"
    assert look.caption_size == 72
    assert look.spoken == "&H0000D7FF"
    assert look.upcoming == "&H00FFFFFF"
    assert look.punch_animation == "fade"


def test_every_preset_sits_at_the_measured_margin():
    """220 puts the second line under the phone's controls, 160 both."""
    assert {look.margin_v for look in PRESETS.values()} == {300}


def test_an_unknown_look_falls_back_rather_than_raising():
    """A preset can be renamed. A reel that was reviewed with it must
    still render."""
    assert resolve("no-such-look").look_id == DEFAULT_LOOK_ID
    assert resolve(None).look_id == DEFAULT_LOOK_ID


def test_every_preset_names_an_animation_that_exists():
    for look in PRESETS.values():
        assert look.punch_animation in ANIMATIONS, look.look_id


def test_the_chosen_preset_is_present():
    """Picked off the rendered sheets: Bungee, letter by letter, gold."""
    look = resolve("blocky-urban")

    assert look.font == "Bungee"
    assert look.punch_animation == "letters"
    assert look.spoken == "&H0000D7FF"


@pytest.mark.parametrize("animation", ANIMATIONS)
def test_no_animation_changes_a_caption_metric(animation):
    """The punch may scale; it is its own Dialogue on its own layer.
    This asserts the tags carry no metric change anyway for the two that
    are also offered on captions, and documents the rule for the rest.
    """
    tags = punch_tags(animation, "Not Enough")

    # `letters` interleaves an override block before each character, so
    # the text survives character by character and not as one run. Strip
    # the blocks and what is left must be exactly what was asked for.
    assert re.sub(r"\{[^}]*\}", "", tags) == "Not Enough"


def test_letters_animates_one_dialogue_not_one_per_character():
    """Positioning each character by hand overlaps the glyphs: measured,
    'Not Enough' rendered with N, E and g colliding, because a fixed
    advance per character does not match the font. One Dialogue lets
    libass lay the text out; only the paint is animated."""
    tags = punch_tags("letters", "Not Enough")

    assert "\\pos(" not in tags
    assert tags.count("\\alpha") >= len("NotEnough")
    assert "\\fscx" not in tags and "\\fscy" not in tags


def test_letters_keeps_the_spaces():
    tags = punch_tags("letters", "Not Enough")

    assert " " in tags


def test_fade_is_the_plain_one():
    assert punch_tags("fade", "Hi") == r"{\fad(180,180)}Hi"

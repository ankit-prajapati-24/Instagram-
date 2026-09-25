r"""Which look a reel actually renders in.

Three rungs, the same shape the music bed uses: the reel's own choice,
the channel default in settings, and `plain` underneath both. A reel
that never chose renders exactly as it did before looks existed.
"""

from __future__ import annotations

import pytest

from engine.assembly.looks import DEFAULT_LOOK_ID, from_row, resolve
from engine.config import Settings

ROW = dict(look_id="blocky-urban", font="Bungee", caption_size=56,
           spoken="&H0000D7FF", upcoming="&H00FFFFFF", margin_v=300,
           punch_font="Bungee", punch_size=72, punch_animation="letters")


@pytest.fixture()
def settings():
    return Settings()


def test_no_choice_and_no_setting_is_the_default(settings):
    settings.look = ""

    assert from_row(None, settings).look_id == DEFAULT_LOOK_ID


def test_the_setting_is_the_channel_default(settings):
    settings.look = "poster"

    assert from_row(None, settings).look_id == "poster"


def test_the_reels_own_choice_beats_the_setting(settings):
    settings.look = "poster"

    assert from_row(ROW, settings).font == "Bungee"


def test_a_stored_row_is_used_verbatim_not_re_resolved(settings):
    """The preset may have been redefined since. What was reviewed is
    what renders."""
    row = {**ROW, "font": "Teko", "caption_size": 99}

    look = from_row(row, settings)

    assert look.font == "Teko"
    assert look.caption_size == 99
    assert look.look_id == "blocky-urban"


def test_a_setting_naming_a_dead_preset_falls_back(settings, capsys):
    settings.look = "no-such-preset"

    assert from_row(None, settings).look_id == DEFAULT_LOOK_ID


def test_the_shipped_default_setting_is_plain():
    """Adding looks must restyle nothing until something is picked."""
    assert Settings().look == DEFAULT_LOOK_ID

r"""Getting a bundled font in front of libass.

`render` runs ffmpeg with cwd set to the .ass file's directory and
passes a bare filename, because a Windows drive-letter colon inside a
filtergraph is parsed as an option separator. `fontsdir` has the same
problem, so the fonts are staged next to the .ass and named
relatively.

Without this, libass resolves a family name against the system font set
and substitutes silently -- the reel renders in the wrong face and
nothing says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.assembly.fonts import (DEVANAGARI_FONT, FONT_DIR,
                                   FONT_FILES, STAGED_DIRNAME,
                                   devanagari_face, stage_fonts)
from engine.assembly.looks import PRESETS, resolve


def test_every_preset_font_is_either_bundled_or_a_system_face():
    """A preset naming a font nobody ships renders as a substitute."""
    for look in PRESETS.values():
        for name in (look.font, look.punch_font):
            assert name in FONT_FILES or name == "Arial", name


def test_every_bundled_file_is_actually_committed():
    missing = [f for f in FONT_FILES.values()
               if not (FONT_DIR / f).is_file()]

    assert not missing, f"named but not committed: {missing}"


def test_every_bundled_family_ships_its_licence():
    """SIL OFL requires the licence to travel with the font."""
    assert list(FONT_DIR.glob("*OFL.txt")) or (FONT_DIR / "OFL.txt").is_file()


def test_staging_copies_the_looks_fonts_next_to_the_captions(tmp_path):
    rel = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel == "fonts"
    assert (tmp_path / "fonts" / FONT_FILES["Bungee"]).is_file()


def test_the_returned_path_is_relative(tmp_path):
    """An absolute path carries a drive-letter colon, which the
    filtergraph parses as an option separator."""
    rel = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel is not None
    assert ":" not in rel
    assert not Path(rel).is_absolute()


def test_a_system_only_look_stages_nothing(tmp_path):
    """`plain` is Arial, which libass finds without help."""
    assert stage_fonts(resolve("plain"), tmp_path) is None
    assert not (tmp_path / "fonts").exists()


def test_a_missing_font_file_is_reported_not_swallowed(tmp_path,
                                                       monkeypatch,
                                                       capsys):
    """libass substitutes silently, so this is the only place the
    problem can be noticed."""
    import engine.assembly.fonts as fonts_mod

    monkeypatch.setattr(fonts_mod, "FONT_DIR", tmp_path / "empty")

    rel = fonts_mod.stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel is None
    assert "Bungee" in capsys.readouterr().err


def test_staging_twice_is_safe(tmp_path):
    """Two previews for one plan run at once."""
    first = stage_fonts(resolve("blocky-urban"), tmp_path)
    second = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert first == second == "fonts"


def test_the_devanagari_face_the_pipeline_falls_back_to_is_bundled():
    r"""The pipeline overrides the face on a Devanagari-sourced caption.

    If that name is not one this module ships, ``stage_fonts`` returns
    None for the whole reel and no ``fontsdir`` reaches libass, which
    then resolves the caption against whatever the host happens to have
    -- silently, exit code 0. That is the one failure this module
    exists to make loud, so the fallback face has to be a bundled one.
    """
    from engine.config import Settings

    assert devanagari_face(Settings().caption_font_devanagari) in FONT_FILES


def test_an_unbundled_devanagari_face_is_refused_out_loud(capsys):
    """This machine's .env really does name Nirmala UI, a system font.

    Passing it through would have cost the reel its fontsdir entirely.
    """
    got = devanagari_face("Nirmala UI")

    assert got == DEVANAGARI_FONT
    assert "Nirmala UI" in capsys.readouterr().err


def test_a_devanagari_reel_still_gets_a_fontsdir(tmp_path):
    """The same check, through the substitution the pipeline makes."""
    import dataclasses

    from engine.config import Settings

    deva = devanagari_face(Settings().caption_font_devanagari)
    look = dataclasses.replace(resolve("blocky-urban"),
                               font=deva, punch_font=deva)

    assert stage_fonts(look, tmp_path) == STAGED_DIRNAME
    assert (tmp_path / STAGED_DIRNAME /
            FONT_FILES[deva]).is_file()

"""Which bed a reel renders with, and what credit that owes.

Before this there was one answer for every reel: whatever
``find_music`` found in ``assets/music/``. A per-reel choice has to beat
that without breaking the reels that never made one -- a plan with no
choice must render exactly as it does today.

Two failures this guards against, both of which ship silently:

**A chosen file that is no longer on disk.** Work directories get
cleaned, and a plan can sit at a gate for days. Rendering in silence, or
failing the render outright, are both worse than quietly falling back to
the shared folder -- so the fallback is the behaviour and the missing
file is not an error.

**A credit that goes missing.** The art credit and the music credit are
separate obligations and a reel can owe both. Joining them is the whole
job; dropping either is a licence breach, and the direction that breaks
the licence is the silent one.
"""

from __future__ import annotations

import pytest

from engine.assembly.audio import resolve_bed
from engine.config import Settings


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.music = True
    s.music_dir = tmp_path / "assets" / "music"
    s.music_dir.mkdir(parents=True)
    (s.music_dir / "placeholder-drone.wav").write_bytes(b"RIFF0000WAVE")
    return s


def _choice(tmp_path, *, licence="cc0", attribution="", name="a1.mp3"):
    bed = tmp_path / "work" / "p1" / "music"
    bed.mkdir(parents=True, exist_ok=True)
    path = bed / name
    path.write_bytes(b"ID3" + b"\0" * 100)
    return {"openverse_id": "a1", "path": str(path), "title": "Creepy",
            "creator": "someone", "licence": licence,
            "attribution": attribution, "source_url": "https://x/1"}


def test_no_choice_falls_back_to_the_shared_folder(settings):
    path, credit = resolve_bed(None, settings)

    assert path and path.endswith("placeholder-drone.wav")
    assert credit == ""


def test_a_chosen_bed_beats_the_shared_folder(settings, tmp_path):
    path, _ = resolve_bed(_choice(tmp_path), settings)

    assert path.endswith("a1.mp3")


def test_a_chosen_bed_that_has_been_cleaned_up_falls_back(settings,
                                                          tmp_path):
    """A plan can wait at a gate for days; work directories do not."""
    choice = _choice(tmp_path)
    import os
    os.remove(choice["path"])

    path, credit = resolve_bed(choice, settings)

    assert path.endswith("placeholder-drone.wav")
    assert credit == "", "a bed that is not playing owes nothing"


def test_music_off_means_no_bed_even_with_a_choice(settings, tmp_path):
    """The switch is a switch. A stored pick must not turn music back on."""
    settings.music = False

    path, credit = resolve_bed(_choice(tmp_path), settings)

    assert path is None
    assert credit == ""


# --- the credit -------------------------------------------------------------


def test_a_cc_by_bed_owes_its_credit(settings, tmp_path):
    choice = _choice(tmp_path, licence="by",
                     attribution='"Creepy" by someone, CC BY 4.0')

    _, credit = resolve_bed(choice, settings)

    assert credit == '"Creepy" by someone, CC BY 4.0'


def test_a_cc0_bed_owes_nothing(settings, tmp_path):
    choice = _choice(tmp_path, licence="cc0",
                     attribution='"Creepy" by someone, CC0 1.0')

    _, credit = resolve_bed(choice, settings)

    assert credit == ""


def test_the_shared_folders_bed_owes_nothing(settings):
    """Whatever is dropped into assets/music/ by hand has no licence
    recorded, so nothing can be claimed about it either way."""
    _, credit = resolve_bed(None, settings)

    assert credit == ""


# --- joining it with the art credit -----------------------------------------


def test_both_credits_survive_together():
    from engine.assembly.audio import join_credits

    joined = join_credits("Art by Lordicon", '"Creepy" by someone, CC BY')

    assert "Lordicon" in joined
    assert "Creepy" in joined


def test_one_credit_alone_is_not_padded():
    from engine.assembly.audio import join_credits

    assert join_credits("Art by Lordicon", "") == "Art by Lordicon"
    assert join_credits("", "Music by x") == "Music by x"
    assert join_credits(None, None) is None


def test_the_same_credit_twice_is_said_once():
    from engine.assembly.audio import join_credits

    assert join_credits("Art by Lordicon", "Art by Lordicon") \
        == "Art by Lordicon"

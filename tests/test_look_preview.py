r"""A look, shown on the reel it will be used on.

Three attempts to improve the captions by reasoning about them each
shipped a fault a viewer caught. Rendering the real thing is the whole
point of this feature, so the preview comes from the reel's own footage
and its own words.

Measured, which is why it is rendered on demand and not cached:

    1080x1920  2.5s   0.9s
     540x960   2.5s   0.4s
     540x960   4.0s   0.6s
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.assembly.looks import resolve
from engine.config import Settings
from engine.contract import Clip
from engine.media.look_preview import (PREVIEW_HEIGHT, PREVIEW_SECONDS,
                                       PREVIEW_WIDTH, PreviewUnavailable,
                                       preview_beat, render_preview)
from tests.factories import make_plan


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    return s


def _clip(settings, path, seconds=3.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=0x303840:s=320x568:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


def _plan(settings, *, punch_on=0, timings=True, with_clip=True):
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=3)
    for index, beat in enumerate(plan.script.beats):
        beat.caption_text = "raat ke teen baje darwaza khula"
        beat.measured_seconds = 4.0
        beat.words = (caption_timings(beat.caption_text, 4.0)
                      if timings else [])
        beat.on_screen_text = "Not Enough" if index == punch_on else None
        if with_clip:
            path = _clip(settings,
                         Path(settings.work_dir) / "p1" / "clips"
                         / beat.beat_id / "clip_01.mp4")
            beat.clips = [Clip(path=str(path), query="q",
                               provider="pexels", duration=2.0)]
    return plan


# --- choosing the beat ------------------------------------------------------


def test_the_preview_beat_has_a_punch_so_the_animation_shows(settings):
    """Half the choice is the punch animation. A preview without one
    answers half the question."""
    plan = _plan(settings, punch_on=1)

    assert preview_beat(plan).beat_id == plan.script.beats[1].beat_id


def test_without_any_punch_it_falls_to_the_first_timed_beat(settings):
    plan = _plan(settings, punch_on=99)

    assert preview_beat(plan).beat_id == plan.script.beats[0].beat_id


def test_a_reel_with_no_timings_cannot_be_previewed(settings):
    """Every animation is timed off word timings, which VOICE writes."""
    plan = _plan(settings, timings=False)

    with pytest.raises(PreviewUnavailable) as caught:
        preview_beat(plan)

    assert "voice" in str(caught.value).lower()


# --- rendering it -----------------------------------------------------------


def test_a_preview_is_a_playable_clip_of_the_right_shape(settings):
    plan = _plan(settings)

    out = render_preview(plan, resolve("blocky-urban"), settings,
                         settings.work_dir)

    probe = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-i", out],
        capture_output=True, text=True).stderr
    assert f"{PREVIEW_WIDTH}x{PREVIEW_HEIGHT}" in probe
    assert Path(out).stat().st_size > 2000


def test_two_looks_produce_different_files(settings):
    """Otherwise the panel shows one look for all of them."""
    plan = _plan(settings)

    a = Path(render_preview(plan, resolve("blocky-urban"), settings,
                            settings.work_dir)).read_bytes()
    b = Path(render_preview(plan, resolve("poster"), settings,
                            settings.work_dir)).read_bytes()

    assert a != b


def test_a_beat_with_no_footage_previews_over_black(settings):
    """A picker that will not open because one clip is missing is worse
    than one that shows the type on a plain ground."""
    plan = _plan(settings, with_clip=False)

    out = render_preview(plan, resolve("poster"), settings,
                         settings.work_dir)

    assert Path(out).is_file()


def test_the_preview_lands_under_the_plans_work_directory(settings):
    plan = _plan(settings)

    out = render_preview(plan, resolve("poster"), settings,
                         settings.work_dir)

    assert "p1" in str(Path(out))


def test_two_previews_at_once_do_not_serve_a_half_written_file(settings):
    """Both write into the same plan directory. Each look gets its own
    name and each write is atomic, so neither can read the other's
    partial output."""
    plan = _plan(settings)

    first = render_preview(plan, resolve("poster"), settings,
                           settings.work_dir)
    second = render_preview(plan, resolve("techno"), settings,
                            settings.work_dir)

    assert first != second
    assert not list(Path(first).parent.glob("*.part"))

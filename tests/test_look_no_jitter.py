r"""No look may move the caption block.

Three caption treatments shipped and a viewer caught each one. The one
that mattered most was invisible to every test in the suite: `\fscy` on
the active word grows the *line box*, so a block anchored at the bottom
walks up the frame. Measured on the rendered reel, 83px of travel.

The test that let it through asserted the line did not move sideways.
It does not. The vertical was the axis that broke.

This renders each preset's captions over black and tracks the block's
top edge through a beat. Slow -- one ffmpeg run per sample -- so it
samples sparsely and runs over presets rather than over frames.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from engine.assembly.captions import build_ass
from engine.assembly.looks import PRESETS
from engine.config import Settings
from tests.factories import make_plan

W, H = 1080, 1920


def _plan():
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = ("Lok Sabha ya Rajya Sabha pahunchna darwaza hai "
                         "lekin sansad banna kaafi nahi")
    beat.on_screen_text = "Not Enough"
    beat.measured_seconds = 4.0
    beat.words = caption_timings(beat.caption_text, 4.0)
    return plan


def _top_edge(settings, ass_path, at):
    """The topmost lit row of the caption band, over black."""
    raw = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={W}x{H}:d=8", "-ss", str(at),
         "-frames:v", "1", "-vf",
         f"subtitles={ass_path.name},crop={W}:600:0:1320,format=gray",
         "-f", "rawvideo", "-"],
        cwd=str(ass_path.parent), capture_output=True).stdout
    if len(raw) < W * 600:
        return None
    for y in range(600):
        if max(raw[y * W:(y + 1) * W]) > 150:
            return y
    return None


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_no_preset_moves_the_caption_block(look_id, tmp_path):
    settings = Settings()
    ass_path = tmp_path / f"{look_id}.ass"
    ass_path.write_text(build_ass(_plan(), look=PRESETS[look_id],
                                  width=W, height=H), encoding="utf-8")

    tops = [t for t in (_top_edge(settings, ass_path, at)
                        for at in (0.4, 1.0, 1.6, 2.2, 2.8, 3.4))
            if t is not None]

    assert tops, f"{look_id} rendered no captions at all"
    assert max(tops) - min(tops) <= 2, (
        f"{look_id} moves the caption block {max(tops) - min(tops)}px; "
        f"83px of this shipped once")

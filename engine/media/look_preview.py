r"""A look, rendered onto the reel it will be used on.

Three attempts to improve the caption treatment by reasoning about it
each shipped a fault a viewer caught -- a scale that walked the block
83px up the frame, a white glow that read as blinking, a gold glow that
filled the counters. Every one was settled in minutes once a real frame
was on screen. This route exists so the frame comes first.

Rendered on demand and not cached. Measured:

    1080x1920  2.5s   0.9s
     540x960   2.5s   0.4s

At 0.4s a cache buys nothing and costs invalidation: a preview is stale
the moment the beat's clips, its caption or its timings change, and
three sources of staleness is three bugs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.assembly import fonts as fonts_mod
from engine.assembly.captions import build_ass
from engine.assembly.looks import Look
from engine.contract import Beat, ReelPlan

PREVIEW_SECONDS = 2.5
PREVIEW_WIDTH = 540
PREVIEW_HEIGHT = 960


class PreviewUnavailable(Exception):
    """The reel cannot be previewed yet. Carries a reason worth showing."""


def preview_beat(plan: ReelPlan) -> Beat:
    """The beat a preview should be cut from.

    One with a punch line first: half of what is being chosen is the
    punch animation, and a preview with no punch answers half the
    question. Failing that, any beat with word timings, because every
    animation here is timed off them.
    """
    timed = [b for b in plan.script.beats if b.words]
    if not timed:
        raise PreviewUnavailable(
            "this reel has no word timings yet, so there is nothing to "
            "time a caption against. Run voice first.")
    for beat in timed:
        if beat.on_screen_text:
            return beat
    return timed[0]


def render_preview(plan: ReelPlan, look: Look, settings,
                   work_dir: str | Path) -> str:
    """Render ``PREVIEW_SECONDS`` of one beat in ``look``. Returns a path.

    The captions are built for a one-beat plan so the document's clock
    starts at zero and the beat's own words land where they would.
    """
    beat = preview_beat(plan)
    target_dir = Path(work_dir) / plan.plan_id / "previews"
    target_dir.mkdir(parents=True, exist_ok=True)

    # A one-beat plan: build_ass lays events out from an offset of zero,
    # and a preview that started 30 seconds in would show nothing.
    single = plan.model_copy(deep=True)
    single.script.beats = [beat.model_copy(deep=True)]

    ass_name = f"{look.look_id}.ass"
    (target_dir / ass_name).write_text(
        build_ass(single, look=look,
                  width=PREVIEW_WIDTH * 2, height=PREVIEW_HEIGHT * 2),
        encoding="utf-8")
    fonts_dir = fonts_mod.stage_fonts(look, target_dir)

    source = next((c.path for c in beat.clips
                   if c.path and Path(c.path).is_file()), None)
    if source:
        inputs = ["-stream_loop", "-1", "-i", source]
    else:
        # A slot that is still unfilled previews over black rather than
        # refusing: the type is legible against it, and a picker that
        # will not open because one clip is missing is worse.
        inputs = ["-f", "lavfi", "-i",
                  f"color=c=black:s={PREVIEW_WIDTH}x{PREVIEW_HEIGHT}:d=10"]

    caption = f"subtitles=filename={ass_name}"
    if fonts_dir:
        caption += f":fontsdir={fonts_dir}"
    chain = (f"scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:"
             f"force_original_aspect_ratio=increase,"
             f"crop={PREVIEW_WIDTH}:{PREVIEW_HEIGHT},{caption},fps=30")

    target = target_dir / f"{look.look_id}.mp4"
    part = target_dir / f"{look.look_id}.mp4.part"
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", *inputs,
         "-t", str(PREVIEW_SECONDS), "-vf", chain, "-an",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-movflags", "+faststart",
         # The muxer has to be named: ffmpeg guesses it from the output
         # extension and ".part" is not one it knows, so without this it
         # refuses with "Invalid argument" before it encodes a frame.
         "-f", "mp4", part.name],
        cwd=str(target_dir), capture_output=True, text=True)
    if result.returncode != 0 or not part.is_file():
        part.unlink(missing_ok=True)
        raise PreviewUnavailable(
            f"the preview did not render: "
            f"{(result.stderr or '').strip()[-200:]}")
    # Renamed rather than written in place: two previews for one plan
    # run at once, and a reader must never get a half-written file.
    os.replace(part, target)
    return str(target)

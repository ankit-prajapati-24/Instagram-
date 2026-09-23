"""Bake designed art into the PNG sequences the renderer composites.

Run by hand after scripts/fetch_sticker_art.py, and only when the art or the
grade changes. The output is committed, so a render needs neither the
network nor any of this module.

    python scripts/bake_stickers.py

Why bake at all, rather than animate in the filtergraph: ``scale`` with
``eval=frame`` rebuilds its swscale context every frame, which took a
12-second 1080x1920 render with two animated stickers from 17.0s to 36.1s,
+113%. Pre-drawn frames cost tenths of a second, because the graph then does
nothing per frame but composite a fixed-size overlay. That measurement is
the reason this file exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly.sticker_art import (        # noqa: E402
    STYLES, apply_style, ground, has_trapped_background, interior_white,
    matte, resample_indices)
from engine.assembly.stickers import (           # noqa: E402
    HOLD_SECONDS, load_triggers, sticker_canvas, sticker_size)

ART_DIR = Path(__file__).resolve().parent.parent / "assets" / "lordicon"
BAKED_DIR = (Path(__file__).resolve().parent.parent / "engine" / "data"
             / "stickers")

# Exactly the visible life the emoji sticker already had, so this change
# alters what is on screen and never how long. Every timing test that passes
# today keeps passing.
#
# HOLD_SECONDS, not POP_SECONDS + HOLD_SECONDS: sticker_chain emits
# `trim=duration=HOLD_SECONDS` and gates `enable` to the same span, so 1.40s
# is the whole of it. Baking longer would put frames after the trim, and the
# animation would never reach its last one -- which is the exact failure
# this file exists to remove.
WINDOW_SECONDS = HOLD_SECONDS

LICENCE = "Animated icons by Lordicon.com"


def frame_count(fps: int) -> int:
    """How many PNGs one baked sequence holds at ``fps``."""
    return max(1, int(round(WINDOW_SECONDS * fps)))


def bake_one(gif: Path, out_dir: Path, *, style: str, size: int,
             fps: int) -> int:
    """Bake one source GIF into one style's frames. Returns the count."""
    frames_out = frame_count(fps)
    canvas = sticker_canvas(size)

    with Image.open(gif) as src:
        n_src = getattr(src, "n_frames", 1)
        sources = []
        for index in range(n_src):
            src.seek(index)
            sources.append(src.convert("RGB"))

    # Checked on one frame, not all of them: the trapped white that matters
    # is a fact about how the icon is drawn, and checking every frame of
    # every icon costs seconds for no new information.
    #
    # A fraction, not a count. A bright highlight inside the art is white and
    # is meant to be -- 2130-skull-poison has 363 such pixels where the bones
    # cross, and renders correctly. What must be caught is a *pocket* of
    # background the fill could not reach, which is an order of magnitude
    # bigger: 3.15% of the frame against 0.23% for that highlight.
    probe = sources[len(sources) // 2]
    if has_trapped_background(probe):
        trapped = interior_white(probe)
        raise ValueError(
            f"{gif.name} has {trapped} near-white pixels inside the art "
            f"({100 * trapped / (probe.size[0] * probe.size[1]):.2f}% of the "
            f"frame), which would render as a white blob where transparency "
            f"belongs. Choose another icon.")

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame-*.png"):
        old.unlink()

    # Matte each distinct source frame once. resample_indices repeats source
    # frames whenever the window needs more frames than the source has
    # spare, and the flood fill is the expensive part of this loop.
    matted: dict[int, Image.Image] = {}

    for out_index, src_index in enumerate(
            resample_indices(len(sources), frames_out)):
        if src_index not in matted:
            matted[src_index] = matte(sources[src_index])
        art = ground(apply_style(matted[src_index], style), style)
        art = art.resize((size, size), Image.LANCZOS)
        # Centred on the same square the emoji pop uses, so the overlay can
        # keep pinning a fixed x/y and geometry tests keep their meaning.
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        inset = (canvas - size) // 2
        frame.alpha_composite(art, (inset, inset))
        frame.save(out_dir / f"frame-{out_index:03d}.png")

    (out_dir / "meta.json").write_text(json.dumps({
        "source": gif.name, "style": style, "frames": frames_out,
        "fps": fps, "size": size, "canvas": canvas, "licence": LICENCE,
    }, indent=2), encoding="utf-8")
    return frames_out


def bake_all(*, width: int = 1080, fps: int = 30,
             scale: float = 0.17) -> int:
    size = sticker_size(width, scale)
    baked = 0
    for trigger in load_triggers():
        if not trigger.art:
            continue
        gif = ART_DIR / f"{trigger.name}.gif"
        if not gif.exists():
            print(f"[bake] {trigger.name}: no art, run "
                  f"scripts/fetch_sticker_art.py", file=sys.stderr)
            continue
        for style in STYLES:
            out = BAKED_DIR / trigger.name / style
            count = bake_one(gif, out, style=style, size=size, fps=fps)
            baked += 1
            print(f"[bake] {trigger.name}/{style}: {count} frames")
    return baked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--scale", type=float, default=0.17)
    args = parser.parse_args()
    bake_all(width=args.width, fps=args.fps, scale=args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

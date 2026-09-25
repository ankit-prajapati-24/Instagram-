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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly.sticker_art import STYLES   # noqa: E402
from engine.config import Settings               # noqa: E402
# The bake itself lives in the engine now, because the renderer bakes
# too: a size the committed art does not cover is baked on demand at
# render time. Two copies of "how art becomes frames" is exactly the
# drift that would let the committed bake and the on-demand one diverge.
# Re-exported, not just used: engine/assembly/sticker_choices.py and
# three test modules import these from here, and several monkeypatch
# `scripts.bake_stickers.bake_one`. The definitions moved into the
# engine so the renderer could bake too; the names stay reachable here
# so nothing that already works has to move with them.
from engine.assembly.sticker_bake import (       # noqa: E402,F401
    ART_DIR, LICENCE, WINDOW_SECONDS, bake_one, frame_count)
from engine.assembly.stickers import (           # noqa: E402
    load_triggers, sticker_size)

BAKED_DIR = (Path(__file__).resolve().parent.parent / "engine" / "data"
             / "stickers")

# What the committed bake is made at. Read from Settings so the shipped
# art and the renderer's default cannot drift apart -- the drift is
# invisible, because a mismatch just bakes on demand instead of failing.
DEFAULT_SCALE = Settings().sticker_scale

def bake_all(*, width: int = 1080, fps: int = 30,
             scale: float = DEFAULT_SCALE) -> int:
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
    parser.add_argument("--scale", type=float,
                        default=DEFAULT_SCALE)
    args = parser.parse_args()
    bake_all(width=args.width, fps=args.fps, scale=args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

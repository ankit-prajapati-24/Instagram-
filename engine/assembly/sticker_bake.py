"""Turning the designed source art into the PNG sequences we composite.

This used to live entirely in ``scripts/bake_stickers.py``, run by hand,
its output committed under ``engine/data/stickers``. That made the
sticker size a constant rather than a setting: ``baked_sequence``
matches a bake by *exact* size, so changing ``sticker_scale`` matched
nothing, dropped every designed concept to its emoji fallback, and
stopped the Lordicon credit being owed -- with no error anywhere.

The committed PNGs are a cache, not the source. The source GIFs sit in
``assets/lordicon`` at 400x400, against a baked canvas of 232, so there
is room to bake bigger with nothing lost. Measured on one concept:

    bake at 184: 42 frames, 6.9s,  789 KB
    bake at 318: 42 frames, 7.1s, 1650 KB

Baking costs the same whatever the size is, and a reel needs only the
concepts that actually fired, in the one style its beat's role calls
for. So ``bake_on_demand`` fills the gap at render time and the render
stops caring which size was committed.

Why bake at all, rather than animate in the filtergraph: ``scale`` with
``eval=frame`` rebuilds its swscale context every frame, which took a
12-second 1080x1920 render with two animated stickers from 17.0s to
36.1s, +113%. Pre-drawn frames cost tenths of a second, because the
graph then does nothing per frame but composite a fixed-size overlay.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from engine.assembly.sticker_art import (apply_style, ground,
                                         has_trapped_background,
                                         interior_white, matte,
                                         resample_indices)
from engine.assembly.stickers import (HOLD_SECONDS, baked_sequence,
                                      pop_scale, sticker_canvas)

ART_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "lordicon"

# Exactly the visible life the emoji sticker already had, so this changes
# what is on screen and never how long.
#
# HOLD_SECONDS, not POP_SECONDS + HOLD_SECONDS: sticker_chain emits
# `trim=duration=HOLD_SECONDS` and gates `enable` to the same span, so
# 1.40s is the whole of it. Baking longer would put frames after the trim
# and the animation would never reach its last one.
WINDOW_SECONDS = HOLD_SECONDS

LICENCE = "Animated icons by Lordicon.com"

# Bumped whenever the *drawing* changes, as opposed to the geometry. The
# frame count catches a window change and the size is in the key, but a
# sequence redrawn with the pop has the same count and the same size as one
# without it -- so without this every cached bake would keep being served,
# looking correct and being a version behind. 2: the pop curve reached
# designed art, which until then only the emoji rung had.
BAKE_VERSION = 2


def frame_count(fps: int) -> int:
    """How many PNGs one baked sequence holds at ``fps``."""
    return max(1, int(round(WINDOW_SECONDS * fps)))


def source_gif(name: str) -> Path:
    """Where a concept's source art lives. May not exist."""
    return ART_DIR / f"{name}.gif"


def bake_one(gif: Path, out_dir: Path, *, style: str, size: int,
             fps: int, licence: str = LICENCE) -> int:
    """Bake one source GIF into one style's frames. Returns the count.

    ``licence`` is what this art obliges the reel to say, recorded into the
    bake's ``meta.json`` so the credit follows the art. It defaults to
    Lordicon's because that is the library every committed sticker came
    from; a caller baking from anywhere else must say so.
    """
    from PIL import Image

    frames_out = frame_count(fps)
    canvas = sticker_canvas(size)

    with Image.open(gif) as src:
        n_src = getattr(src, "n_frames", 1)
        sources = []
        for index in range(n_src):
            src.seek(index)
            sources.append(src.convert("RGBA"))

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

    # Does this source carry its own transparency, or does it need it cut
    # out? Lordicon serves art on an opaque white ground, which `matte`
    # floods away from the border inward. Noto's animated emoji arrive with
    # real alpha and a pale, non-white ground, so the fill never starts --
    # it ran, found nothing near-white to eat, and the sticker shipped as an
    # opaque block over the footage.
    alpha_given = probe.getchannel("A").getextrema()[0] < 16

    # Only meaningful for the white-ground path: a pocket of background the
    # fill could not reach. A source that brought its own alpha has no such
    # pocket by construction, and running this on it would read the art's
    # own white as trapped.
    if not alpha_given and has_trapped_background(probe.convert("RGB")):
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
            frame_src = sources[src_index]
            matted[src_index] = (
                frame_src if alpha_given else matte(frame_src.convert("RGB")))
        art = ground(apply_style(matted[src_index], style), style)
        # The pop, drawn here rather than expressed in the filtergraph for
        # the reason at the top of this module, and with the same curve
        # `render_pop_frames` gives the emoji: designed art used to arrive
        # at full size while the pop *sound* fired on it anyway, so the ear
        # heard a jolt the eye never saw.
        scale = pop_scale(out_index / fps)
        side = int(round(size * scale)) // 2 * 2
        # Centred on the same square the emoji pop uses, so the overlay can
        # keep pinning a fixed x/y and geometry tests keep their meaning.
        # The canvas is already sized for OVERSHOOT, which is what makes
        # room for the frames that are bigger than `size`.
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        if side >= 2:
            grown = art.resize((side, side), Image.LANCZOS)
            frame.alpha_composite(grown, ((canvas - side) // 2,
                                          (canvas - side) // 2))
        frame.save(out_dir / f"frame-{out_index:03d}.png")

    _write_meta(out_dir, gif.name, style=style, frames=frames_out, fps=fps,
                size=size, canvas=canvas, licence=licence)
    return frames_out


def _write_meta(out_dir: Path, source: str, *, style: str, frames: int,
                fps: int, size: int, canvas: int,
                licence: str = LICENCE) -> None:
    """Write meta.json last and atomically.

    ``baked_sequence`` reads this to decide whether a folder is usable,
    so a meta that landed before the frames it describes would advertise
    a bake that is still being written.
    """
    payload = json.dumps({
        "source": source, "style": style, "frames": frames, "fps": fps,
        "size": size, "canvas": canvas, "licence": licence,
    }, indent=2)
    part = out_dir / "meta.json.part"
    part.write_text(payload, encoding="utf-8")
    os.replace(part, out_dir / "meta.json")


def bake_on_demand(name: str, style: str, cache_dir: str | Path, *,
                   fps: int, size: int):
    """Bake one concept at ``size`` into the cache, and hand it back.

    Returns what ``baked_sequence`` returns, or ``None`` -- and ``None``
    means the caller falls through to the emoji exactly as it did
    before, because a decoration that cannot be drawn must cost nothing
    beyond itself. Every way this can fail ends there: no source art, a
    Pillow that will not load, art with a pocket of trapped white that
    would render as a white blob.

    The result goes back through ``baked_sequence`` rather than being
    returned directly, so art baked here is validated exactly as the
    committed art is -- right frame count, right canvas, meta agreeing
    with what is on disk.
    """
    cache_dir = Path(cache_dir)
    ready = baked_sequence(name, style, fps=fps, size=size, root=cache_dir)
    if ready is not None:
        return ready

    gif = source_gif(name)
    if not gif.is_file():
        return None

    try:
        bake_one(gif, cache_dir / name / style, style=style, size=size,
                 fps=fps)
    except Exception as exc:                 # noqa: BLE001 - see docstring
        print(f"[stickers] {name}/{style} could not be baked at {size}px, "
              f"falling back: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return None

    return baked_sequence(name, style, fps=fps, size=size, root=cache_dir)

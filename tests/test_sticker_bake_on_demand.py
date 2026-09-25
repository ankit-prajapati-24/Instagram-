"""Baking the designed art at whatever size is asked for.

``sticker_scale`` has always been a config field, and changing it has
always broken the thing it was meant to change. ``baked_sequence``
matches committed art by *exact* size, and the five concepts under
``engine/data/stickers`` are baked at size 184 / canvas 232 -- so any
other scale matched nothing, every designed sticker silently became its
emoji fallback, and the Lordicon credit silently stopped being owed.
No error, no warning, just plainer stickers.

The committed PNGs are a cache, not the source. The source GIFs are in
``assets/lordicon`` and they are 400x400, against a baked canvas of 232
-- so there is room to bake bigger with nothing lost. Measured:

    bake at 184: 42 frames, 6.9s,  789 KB
    bake at 318: 42 frames, 7.1s, 1650 KB

Baking costs the same whatever the size, and a reel only needs the
concepts that actually fired, in the one style its beat's role calls
for. So a mismatch now bakes from the source into the cache instead of
giving up, and the committed art is still used untouched whenever it
does match.

The ceiling is the source: canvas 400 is size 318, which on a 1080
frame is a scale of 0.294. Past that the bake would be upscaling art it
already has at full resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.assembly import stickers as stk
from engine.assembly.sticker_bake import ART_DIR, bake_on_demand

pytest.importorskip("PIL")

BAKED = "death"          # ships committed art
STYLE = "punchy"
FPS = 30



def _committed_size(name: str = "death", style: str = "punchy") -> int | None:
    """The size engine/data/stickers is actually baked at, read rather
    than written down.

    An earlier version of this file hardcoded 184 and went red the day
    the committed art was re-baked at 318 -- which is a fact about the
    art, not about this feature, and a test that asserts it twice is a
    test that breaks on someone else's correct change.
    """
    meta = stk.BAKED_ROOT / name / style / "meta.json"
    if not meta.is_file():
        return None
    try:
        recorded = json.loads(meta.read_text(encoding="utf-8"))
        size = int(recorded["size"])
        frames = int(recorded["frames"])
    except (ValueError, KeyError):
        return None
    # None also when the shipped art was baked for a different
    # HOLD_SECONDS than the code currently wants: baked_sequence refuses
    # it on the frame count, so there is no usable committed art to
    # assert anything about. That is an art-and-code sync question, not
    # a fact about baking on demand -- and on-demand baking is precisely
    # what carries the render through it.
    if frames != round(stk.HOLD_SECONDS * FPS):
        return None
    return size


COMMITTED_SIZE = _committed_size()
# A size nothing is committed at, whatever the committed one happens to
# be. Small enough to bake quickly and still a real reduction of the
# 400px source.
OTHER_SIZE = 212 if COMMITTED_SIZE != 212 else 206


@pytest.fixture()
def cache(tmp_path):
    return tmp_path / "_stickers"


def _has_source(name=BAKED):
    return (ART_DIR / f"{name}.gif").is_file()


requires_art = pytest.mark.skipif(not _has_source(),
                                  reason="source GIFs are not present")


# --- the defect -------------------------------------------------------------


@pytest.mark.skipif(COMMITTED_SIZE is None, reason="no committed art")
def test_the_committed_art_still_answers_its_own_size():
    """Nothing is baked when the shipped art already has the answer."""
    found = stk.baked_sequence(BAKED, STYLE, fps=FPS, size=COMMITTED_SIZE)

    assert found is not None
    # Indexed, not unpacked: baked_sequence's tuple has grown a field
    # before and may again, and none of these tests are about its width.
    assert "data" in found[0].replace("\\", "/")


def test_a_different_size_matches_no_committed_art():
    """The state this feature exists to rescue: without a bake, this is
    the silent drop to emoji."""
    assert stk.baked_sequence(BAKED, STYLE, fps=FPS,
                              size=OTHER_SIZE) is None


# --- baking on demand -------------------------------------------------------


@requires_art
def test_a_size_with_no_committed_art_gets_baked(cache):
    found = bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)

    assert found is not None, "the designed art fell through to emoji"
    frames, canvas = found[1], found[2]
    assert canvas == stk.sticker_canvas(OTHER_SIZE)
    assert frames == round(stk.HOLD_SECONDS * FPS)


@requires_art
def test_the_bake_lands_in_the_cache_not_the_committed_tree(cache):
    """Writing into engine/data would mean a render mutating the repo."""
    before = sorted(stk.BAKED_ROOT.rglob("frame-*.png"))

    pattern = bake_on_demand(BAKED, STYLE, cache, fps=FPS,
                             size=OTHER_SIZE)[0]

    assert str(cache) in pattern
    assert sorted(stk.BAKED_ROOT.rglob("frame-*.png")) == before


@requires_art
def test_the_second_call_reuses_what_the_first_baked(cache):
    """Seven seconds a concept is fine once and not fine per render."""
    first = bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)
    stamps = {p: p.stat().st_mtime_ns
              for p in (cache / BAKED / STYLE).glob("frame-*.png")}
    assert stamps

    second = bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)

    assert second == first
    assert {p: p.stat().st_mtime_ns
            for p in (cache / BAKED / STYLE).glob("frame-*.png")} == stamps


@requires_art
def test_the_bake_records_the_size_it_was_actually_made_at(cache):
    """The meta is what baked_sequence checks; a bake that lied about
    its size would be accepted and then sit off its own anchor."""
    bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)

    meta = json.loads((cache / BAKED / STYLE / "meta.json")
                      .read_text(encoding="utf-8"))

    assert meta["size"] == OTHER_SIZE
    assert meta["canvas"] == stk.sticker_canvas(OTHER_SIZE)
    assert meta["licence"], "the Lordicon credit must survive the bake"


@requires_art
def test_what_is_baked_passes_the_same_checks_as_committed_art(cache):
    """Baked on demand or shipped, it goes through one validator."""
    bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)

    assert stk.baked_sequence(BAKED, STYLE, fps=FPS, size=OTHER_SIZE,
                              root=cache) is not None


# --- when it cannot ---------------------------------------------------------


def test_a_concept_with_no_source_gif_gives_up_quietly(cache):
    """Still the emoji fallback, as before. A missing decoration must
    cost nothing beyond itself."""
    assert bake_on_demand("no-such-concept", STYLE, cache, fps=FPS,
                          size=OTHER_SIZE) is None


def test_art_the_baker_refuses_gives_up_quietly(cache, tmp_path,
                                                monkeypatch):
    """bake_one raises on a pocket of trapped white, which would render
    as a white blob. That refusal must not take the render with it."""
    import engine.assembly.sticker_bake as bake_mod

    def refuses(*args, **kwargs):
        raise ValueError("trapped white")

    monkeypatch.setattr(bake_mod, "bake_one", refuses)
    monkeypatch.setattr(bake_mod, "source_gif",
                        lambda name: tmp_path / "x.gif")
    (tmp_path / "x.gif").write_bytes(b"not really a gif")

    assert bake_on_demand(BAKED, STYLE, cache, fps=FPS,
                          size=OTHER_SIZE) is None


@requires_art
def test_a_half_written_bake_is_not_used(cache):
    """The validator counts frames, so a bake interrupted partway
    through is refused rather than composited truncated."""
    bake_on_demand(BAKED, STYLE, cache, fps=FPS, size=OTHER_SIZE)
    frames = sorted((cache / BAKED / STYLE).glob("frame-*.png"))
    frames[-1].unlink()

    assert stk.baked_sequence(BAKED, STYLE, fps=FPS, size=OTHER_SIZE,
                              root=cache) is None


# --- the ceiling ------------------------------------------------------------


def test_the_shipped_scale_does_not_ask_for_more_than_the_source_has():
    """400x400 sources. Past canvas 400 the bake upscales art it
    already holds at full resolution."""
    from PIL import Image

    from engine.config import Settings

    gif = ART_DIR / f"{BAKED}.gif"
    if not gif.is_file():
        pytest.skip("source GIFs are not present")
    with Image.open(gif) as src:
        source_px = min(src.size)

    size = stk.sticker_size(1080, Settings().sticker_scale)

    assert stk.sticker_canvas(size) <= source_px, (
        f"scale asks for canvas {stk.sticker_canvas(size)} from "
        f"{source_px}px art")


def test_the_shipped_scale_is_big_enough_to_read():
    """The complaint that started this: 184px read as decoration."""
    from engine.config import Settings

    assert stk.sticker_size(1080, Settings().sticker_scale) >= 300

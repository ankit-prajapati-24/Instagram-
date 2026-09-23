"""Fetching and baking designed sticker art.

Nothing here talks to the network. The download is one URL built from the
trigger map, so the URL is tested as a pure function and the verification
is tested against GIFs written on the spot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from scripts.fetch_sticker_art import art_url, verify_gif


def _write_gif(path: Path, frames: int, side: int = 32,
               colour=(10, 20, 30)) -> Path:
    # Pillow's GIF encoder collapses byte-identical consecutive frames into
    # one, extending the previous frame's duration instead of writing a new
    # one -- unconditionally, not just under optimize=True. A fixed colour
    # for every frame would therefore always write a 1-frame file no matter
    # how many `images` are appended, so each frame nudges the colour to
    # stay distinct.
    images = [Image.new("RGB", (side, side),
                        tuple((c + i) % 256 for c in colour))
             for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=40, loop=0)
    return path


def test_the_url_is_built_from_the_manifest():
    art = {"source": "lordicon", "family": "wired",
           "variant": "flat", "slug": "2130-skull-poison"}
    assert art_url(art) == (
        "https://media.lordicon.com/icons/wired/flat/"
        "2130-skull-poison.gif")


def test_an_unknown_source_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="source"):
        art_url({"source": "elsewhere", "family": "wired",
                 "variant": "flat", "slug": "x"})


def test_a_real_gif_verifies_and_reports_its_shape(tmp_path):
    path = _write_gif(tmp_path / "ok.gif", frames=12, side=48)
    assert verify_gif(path) == (12, 48)


def test_an_html_error_page_saved_as_gif_is_refused(tmp_path):
    path = tmp_path / "bad.gif"
    path.write_bytes(b"<!DOCTYPE html><html><head><title>404")
    with pytest.raises(ValueError, match="not a GIF"):
        verify_gif(path)


def test_a_single_frame_gif_is_refused(tmp_path):
    path = _write_gif(tmp_path / "still.gif", frames=1)
    with pytest.raises(ValueError, match="single frame"):
        verify_gif(path)


from engine.assembly.sticker_art import has_trapped_background, interior_white, matte


def _disc(side=64, bg=(255, 255, 255), fg=(20, 30, 40), hole=None):
    """A solid disc on a background, optionally with a white hole in it."""
    from PIL import ImageDraw
    im = Image.new("RGB", (side, side), bg)
    d = ImageDraw.Draw(im)
    d.ellipse((8, 8, side - 8, side - 8), fill=fg)
    if hole:
        d.ellipse(hole, fill=(255, 255, 255))
    return im


def test_the_background_becomes_transparent_and_the_art_does_not():
    out = matte(_disc())
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0, "corner should be cut away"
    assert out.getpixel((32, 32))[3] == 255, "centre should survive"


def test_white_enclosed_by_the_art_is_reported_not_silently_removed():
    art = _disc(hole=(26, 26, 38, 38))
    out = matte(art)
    assert out.getpixel((32, 32))[3] == 255, \
        "an enclosed hole must stay opaque, not be punched through"
    assert interior_white(art) > 0


def test_a_clean_icon_reports_no_interior_white():
    assert interior_white(_disc()) == 0


def test_every_shipped_icon_mattes_without_holes():
    art_dir = Path("assets/lordicon")
    gifs = sorted(art_dir.glob("*.gif"))
    assert gifs, "run scripts/fetch_sticker_art.py first"
    for path in gifs:
        with Image.open(path) as im:
            im.seek(im.n_frames // 2)
            frame = im.convert("RGB")
        assert not has_trapped_background(frame), \
            f"{path.name} has a pocket of white inside the art; choose another icon"


def test_a_highlight_inside_the_art_is_not_mistaken_for_a_hole():
    """The threshold has to separate two real cases, not just admit one.

    A trapped pocket of background and a bright highlight are both white
    inside the silhouette; only their size tells them apart.
    """
    pocket = _disc(hole=(26, 26, 38, 38))
    assert has_trapped_background(pocket)

    speck = _disc(hole=(31, 31, 33, 33))
    assert interior_white(speck) > 0, "fixture should trap a few pixels"
    assert not has_trapped_background(speck)


from engine.assembly.sticker_art import resample_indices


def test_the_whole_source_animation_is_covered_end_to_end():
    out = resample_indices(101, 48)
    assert len(out) == 48
    assert out[0] == 0
    assert out[-1] == 100
    assert out == sorted(out), "time must not run backwards"


def test_a_short_source_repeats_frames_instead_of_ending_early():
    out = resample_indices(6, 48)
    assert len(out) == 48
    assert out[0] == 0 and out[-1] == 5
    assert set(out) == set(range(6))


def test_a_single_output_frame_does_not_divide_by_zero():
    assert resample_indices(101, 1) == [0]


def test_a_single_source_frame_fills_the_window():
    assert resample_indices(1, 5) == [0, 0, 0, 0, 0]


from engine.assembly.sticker_art import STYLES, apply_style


def _saturation(im):
    hsv = im.convert("RGB").convert("HSV")
    pixels = [p for p, a in zip(hsv.getdata(1), im.getdata(3)) if a > 200]
    return sum(pixels) / max(len(pixels), 1)


def test_both_styles_exist():
    assert STYLES == ("punchy", "dark")


def test_dark_is_less_saturated_than_punchy():
    art = matte(_disc(fg=(200, 40, 40)))
    assert _saturation(apply_style(art, "dark")) < \
        _saturation(apply_style(art, "punchy"))


def test_neither_style_disturbs_the_matte():
    art = matte(_disc())
    for style in STYLES:
        out = apply_style(art, style)
        assert out.mode == "RGBA"
        assert out.size == art.size
        assert out.getpixel((0, 0))[3] == 0, \
            f"{style} must not paint over transparent background"


def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError, match="style"):
        apply_style(matte(_disc()), "neon")


def test_dark_is_actually_darker_not_merely_paler():
    """The first dark grade desaturated and tinted without reducing
    luminance, so it came out flat yellow and *lighter* than the source.
    Saturation alone cannot catch that; brightness can."""
    art = matte(_disc(fg=(200, 40, 40)))
    dark = apply_style(art, "dark")

    def luminance(im):
        grey = im.convert("RGB").convert("L")
        vals = [v for v, a in zip(grey.getdata(), im.getdata(3)) if a > 200]
        return sum(vals) / max(len(vals), 1)

    assert luminance(dark) < luminance(art)


def test_dark_keeps_enough_colour_to_stay_legible():
    """Two differently coloured regions must still differ after grading.
    The first attempt flattened everything toward one gold, which is how
    the eye's pupil vanished into the eyeball around it. The threshold of
    100 sits between the old broken grade (which measured 63 and let the
    bug ship) and the current grade (146), so this test would have caught
    the defect it was written for."""
    from PIL import ImageDraw
    im = Image.new("RGB", (64, 64), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.ellipse((8, 8, 56, 56), fill=(200, 60, 40))
    d.ellipse((26, 26, 38, 38), fill=(40, 90, 200))
    graded = apply_style(matte(im), "dark").convert("RGB")

    outer = graded.getpixel((16, 32))
    inner = graded.getpixel((32, 32))
    spread = sum(abs(a - b) for a, b in zip(outer, inner))
    assert spread > 100, f"regions collapsed toward one colour (spread {spread})"


from engine.assembly.sticker_art import ground


def test_the_shadow_adds_opacity_below_the_art():
    art = matte(_disc(side=96))
    out = ground(art, "punchy")
    # A point just under the disc's bottom edge is empty before grounding.
    probe = (48, 92)
    assert art.getpixel(probe)[3] == 0
    assert out.getpixel(probe)[3] > 0, "shadow should fall below the art"


def test_grounding_keeps_the_canvas_size():
    art = matte(_disc(side=96))
    assert ground(art, "dark").size == art.size


def test_the_art_itself_stays_fully_opaque():
    art = matte(_disc(side=96))
    out = ground(art, "punchy")
    assert out.getpixel((48, 48))[3] == 255


def test_punchy_gets_a_light_outline_not_just_the_shadows_spill():
    """The shadow also puts alpha outside the art, so alpha alone proves
    nothing -- the earlier version of this test passed with the outline
    switched off. The outline is near-white and the shadow is black, so
    colour is what separates them."""
    art = matte(_disc(side=96))
    out = ground(art, "punchy")

    probe = (5, 48)                  # just outside the disc's left edge
    assert art.getpixel(probe)[3] == 0, "probe should start outside the art"

    red, green, blue, alpha = out.getpixel(probe)
    assert alpha > 0, "something should reach the probe"
    assert red > 140 and green > 140 and blue > 140, \
        f"expected the near-white outline, got {(red, green, blue)}"


def test_dark_gets_a_gold_glow_not_a_hard_edge():
    art = matte(_disc(side=96))
    out = ground(art, "dark")
    probe = (4, 48)
    assert out.getpixel(probe)[3] > 0, "glow should reach outside the art"
    red, green, blue, _ = out.getpixel(probe)
    assert red > blue, f"glow should be warm, got {(red, green, blue)}"


def test_grounding_leaves_opaque_art_alone_and_blends_only_its_rim():
    """The invariant is about *opaque* pixels.

    A translucent rim blending into the halo is intended -- it is what makes
    the halo sit behind the art instead of ringing it. What must not happen
    is the opaque body of the art changing, or its silhouette eroding.
    """
    art = matte(_disc(side=96))
    for style in STYLES:
        out = ground(art, style)
        assert out.size == art.size

        opaque = [(x, y) for y in range(96) for x in range(96)
                  if art.getpixel((x, y))[3] == 255]
        assert opaque, "fixture should have a solid interior"
        for point in opaque:
            assert out.getpixel(point) == art.getpixel(point), \
                f"{style} changed opaque art at {point}"


import json as _json

from scripts.bake_stickers import WINDOW_SECONDS, bake_one, frame_count


def test_the_window_is_exactly_the_span_the_chain_lets_through():
    """The bake must last as long as the chain actually keeps it on screen.

    Asserted against the emitted filtergraph, not against the constant the
    bake is defined from. ``WINDOW_SECONDS == stk.HOLD_SECONDS`` was the
    earlier form of this test and it could not fail: bake_stickers.py
    defines ``WINDOW_SECONDS = HOLD_SECONDS``, so it asserted
    ``HOLD_SECONDS == HOLD_SECONDS`` while claiming to guard Ruling 1. What
    matters is the two numbers ffmpeg is given -- the trim and the enable
    gate -- and whether ``frame_count(fps)`` frames at ``fps`` fill them.
    """
    import re

    from engine.assembly import stickers as stk

    fps = 30
    start = 2.5
    sticker = stk.Sticker(
        name="death", emoji="💀", word="kankaal", beat_index=0,
        start=start, slot=0, style="dark", baked=True, size=184, canvas=232,
        frames=frame_count(fps), pattern="x-%03d.png", png="x-041.png")

    parts, _label = stk.sticker_chain([sticker], "v", 4, fps=fps)
    graph = ";".join(parts)

    trim = float(re.search(r"trim=duration=([0-9.]+)", graph).group(1))
    gate = re.search(r"enable='between\(t,([0-9.]+),([0-9.]+)\)'", graph)
    gate_span = float(gate.group(2)) - float(gate.group(1))
    baked_span = frame_count(fps) / fps

    assert baked_span == pytest.approx(trim, abs=0.005), (
        f"the bake runs {baked_span:.3f}s but the chain trims to {trim:.3f}s")
    assert baked_span == pytest.approx(gate_span, abs=0.005), (
        f"the bake runs {baked_span:.3f}s but enable gates {gate_span:.3f}s")
    # And the constant the bake is defined from agrees with both of them.
    assert WINDOW_SECONDS == pytest.approx(trim, abs=0.005)


def test_the_frame_count_follows_the_configured_fps():
    assert frame_count(30) == 42
    assert frame_count(60) == 84
    assert frame_count(24) == 34


def test_baking_writes_one_png_per_output_frame_and_a_meta(tmp_path):
    gif = _write_gif(tmp_path / "src.gif", frames=9, side=64,
                     colour=(30, 40, 50))
    out = tmp_path / "baked"
    written = bake_one(gif, out, style="punchy", size=48, fps=30)

    pngs = sorted(out.glob("frame-*.png"))
    assert written == len(pngs) == frame_count(30)
    assert pngs[0].name == "frame-000.png"

    meta = _json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["frames"] == frame_count(30)
    assert meta["fps"] == 30
    assert meta["style"] == "punchy"
    assert meta["licence"] == "Animated icons by Lordicon.com"


def test_every_baked_frame_is_rgba_at_the_canvas_size(tmp_path):
    gif = _write_gif(tmp_path / "src.gif", frames=9, side=64)
    out = tmp_path / "baked"
    bake_one(gif, out, style="dark", size=48, fps=30)

    from engine.assembly.stickers import sticker_canvas
    canvas = sticker_canvas(48)
    for png in sorted(out.glob("frame-*.png")):
        with Image.open(png) as im:
            assert im.mode == "RGBA"
            assert im.size == (canvas, canvas)


def test_an_icon_with_trapped_white_is_refused_by_name(tmp_path):
    from PIL import ImageDraw
    frames = []
    for i in range(4):
        im = Image.new("RGB", (64, 64), (255, 255, 255))
        d = ImageDraw.Draw(im)
        # Vary the fill per frame. Pillow merges byte-identical consecutive
        # frames when writing a GIF, so four identical ones would be saved
        # as a single frame and this fixture would not be an animation.
        d.ellipse((8, 8, 56, 56), fill=(20 + i, 30, 40))
        d.ellipse((26, 26, 38, 38), fill=(255, 255, 255))
        frames.append(im)
    gif = tmp_path / "holed.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:],
                   duration=40, loop=0)

    with pytest.raises(ValueError, match="holed.gif"):
        bake_one(gif, tmp_path / "baked", style="punchy", size=48, fps=30)

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


def test_punchy_gets_an_outline_that_widens_the_silhouette():
    art = matte(_disc(side=96))
    out = ground(art, "punchy")
    # A ring just outside the disc's edge is empty before grounding and
    # opaque after, on the side the shadow does not fall.
    probe = (4, 48)
    assert art.getpixel(probe)[3] == 0
    assert out.getpixel(probe)[3] > 0


def test_dark_gets_a_gold_glow_not_a_hard_edge():
    art = matte(_disc(side=96))
    out = ground(art, "dark")
    probe = (4, 48)
    assert out.getpixel(probe)[3] > 0, "glow should reach outside the art"
    red, green, blue, _ = out.getpixel(probe)
    assert red > blue, f"glow should be warm, got {(red, green, blue)}"


def test_the_art_still_covers_its_own_halo():
    """The halo sits under the art, so the art's own pixels are unchanged."""
    art = matte(_disc(side=96))
    for style in STYLES:
        out = ground(art, style)
        assert out.getpixel((48, 48))[:3] == art.getpixel((48, 48))[:3]

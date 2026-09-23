"""Turning designed source art into sticker frames.

Pure image work: every function here takes pictures and returns pictures,
touches no disk and no settings. scripts/bake_stickers.py is what strings
them together, and the renderer never imports this file at all -- by the
time a render runs, all of this has already happened and been committed as
PNGs.

The source art is a GIF on a white background, because that is what the CDN
serves. GIF carries no partial alpha, so the matte has to be recovered
rather than read, and that is the first thing here.
"""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# A pixel is background-white when every channel is above this. Measured on
# the shipped icons: the art's lightest real tone is (235, 230, 239), so 238
# separates the two without clipping the art.
WHITE_CUTOFF = 238
# The value the flood fill writes. Neither 0 nor 255, so it cannot be
# confused with the mask it is filling.
_FILLED = 128


def _white_mask(rgb: Image.Image, cutoff: int) -> Image.Image:
    """An ``L`` image, 255 where every channel is above ``cutoff``."""
    red, green, blue = rgb.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    return darkest.point(lambda v: 255 if v > cutoff else 0)


def _background(rgb: Image.Image, cutoff: int) -> Image.Image:
    """An ``L`` image, 255 exactly where white reaches in from the border.

    The fill runs on a one-pixel white frame pasted around the mask. That
    single seed at (0, 0) then reaches every border-connected white pixel in
    one pass, which is both faster and less fiddly than seeding from each of
    the four edges -- and, unlike a per-edge fill, it cannot miss a white
    region that only touches a corner.
    """
    width, height = rgb.size
    padded = Image.new("L", (width + 2, height + 2), 255)
    padded.paste(_white_mask(rgb, cutoff), (1, 1))
    ImageDraw.floodfill(padded, (0, 0), _FILLED)
    reached = padded.crop((1, 1, width + 1, height + 1))
    return reached.point(lambda v: 255 if v == _FILLED else 0)


def matte(frame: Image.Image, *, cutoff: int = WHITE_CUTOFF) -> Image.Image:
    """``frame`` as RGBA with its white background cut away.

    Only white that is *connected to the border* is removed. White enclosed
    by the art -- the whites of an eye, a highlight -- is left alone, so a
    wrong icon comes out looking wrong rather than looking holed, and
    ``interior_white`` is what catches it before it ships.

    The alpha is then blurred by half a pixel. GIF edges are hard by
    construction, and a hard edge over moving footage shimmers; half a pixel
    is enough to stop that without softening the shape.
    """
    rgb = frame.convert("RGB")
    alpha = ImageChops.invert(_background(rgb, cutoff))
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.5))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def interior_white(frame: Image.Image, *,
                   cutoff: int = WHITE_CUTOFF) -> int:
    """How many near-white pixels the matte cannot reach.

    Non-zero means this icon would render with holes in it, or with white
    left inside it that reads as a hole over dark footage. The honest fix is
    a different icon, so the bake refuses rather than shipping either.
    """
    rgb = frame.convert("RGB")
    white = _white_mask(rgb, cutoff)
    reached = _background(rgb, cutoff)
    trapped = ImageChops.subtract(white, reached)
    return sum(1 for value in trapped.getdata() if value > 0)

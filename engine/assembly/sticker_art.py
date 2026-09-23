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
# Interior white above this fraction of the frame is a pocket of background
# the fill could not reach; below it, it is a highlight inside the art. A
# bright highlight is white and is meant to be -- 2130-skull-poison has 363
# such pixels where the bones cross and renders correctly -- while a real
# trapped pocket is an order of magnitude bigger: 3.15% of the frame against
# 0.23% for that highlight.
MAX_INTERIOR_WHITE_FRACTION = 0.01


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
    # histogram[i] is the count of pixels with value i; sum of buckets 1-255
    # is the count of all non-zero pixels, no deprecation warning.
    return sum(trapped.histogram()[1:])


def has_trapped_background(frame: Image.Image, *,
                           cutoff: int = WHITE_CUTOFF) -> bool:
    """True when enough white is trapped inside the art to read as a blob.

    The count alone cannot tell a highlight from a hole; the share of the
    frame can.
    """
    width, height = frame.size
    trapped = interior_white(frame, cutoff=cutoff)
    return trapped > MAX_INTERIOR_WHITE_FRACTION * width * height


def resample_indices(n_src: int, frames_out: int) -> list[int]:
    """Which source frame each output frame comes from.

    The source animations run 2.5-4.1s; a sticker lives 1.6s. Truncating
    would cut them mid-motion, which reads as a glitch rather than as a
    beat, so the whole arc is played faster instead. First and last output
    frames land exactly on the first and last source frames.

    ``max(..., 1)`` guards the one-output-frame case, where there is no
    span to divide across.
    """
    if frames_out <= 1:
        return [0]
    last = max(n_src - 1, 0)
    span = frames_out - 1
    return [round(i * last / span) for i in range(frames_out)]

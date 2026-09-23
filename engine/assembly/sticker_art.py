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

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

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

    The source animations run 2.5-4.1s; a sticker lives 1.40s. Truncating
    would cut them mid-motion, which reads as a glitch rather than as a
    beat, so the whole arc is played faster instead. First and last output
    frames land exactly on the first and last source frames.

    The early return when ``frames_out <= 1`` guards the case where
    output is a single frame, where there is no span to divide across.
    """
    if frames_out <= 1:
        return [0]
    last = max(n_src - 1, 0)
    span = frames_out - 1
    return [round(i * last / span) for i in range(frames_out)]


STYLES = ("punchy", "dark")

# The gold captions.py already highlights the spoken word with
# (COLOUR_SPOKEN = &H0000D7FF, which is #FFD700 in RGB). The channel has one
# accent colour; a sticker that introduced a second would read as a
# different video's asset dropped into this one.
ACCENT = (255, 215, 0)

# The dark grade, in the order it is applied. Brightness is what actually
# makes it dark -- desaturating and tinting alone can only make art pale and
# yellow. Saturation stays high enough that the icons keep the colour
# separation that makes them readable at 184px, and the tint is a warmth
# rather than a coat of paint. Chosen by rendering the five shipped icons
# under five candidate combinations and looking at them.
_DARK_BRIGHTNESS = 0.72
_DARK_SATURATION = 0.65
_DARK_TINT = 0.10
_PUNCHY_SATURATION = 1.15


def _tint(frame: Image.Image, colour: tuple[int, int, int],
          amount: float) -> Image.Image:
    """Blend the RGB toward ``colour``, leaving alpha exactly as it was."""
    rgb = frame.convert("RGB")
    flat = Image.new("RGB", frame.size, colour)
    blended = Image.blend(rgb, flat, amount)
    blended.putalpha(frame.getchannel("A"))
    return blended


def apply_style(frame: Image.Image, style: str) -> Image.Image:
    """One matted frame, graded for the beat role it will land on.

    Punchy keeps most of the source colour on purpose: hook and cta are
    where loudness earns its place. Dark drains and tints instead, so a
    reveal or a twist gets emphasis without the tonal clash a bright
    cartoon makes against this channel's footage.

    Alpha is carried through untouched by both. Grading must never move the
    matte -- Task 3 already decided which pixels exist.
    """
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; expected one of {STYLES}")
    alpha = frame.getchannel("A")
    if style == "punchy":
        out = ImageEnhance.Color(frame.convert("RGB")).enhance(
            _PUNCHY_SATURATION)
        out = out.convert("RGBA")
        out.putalpha(alpha)
        return out

    darkened = ImageEnhance.Brightness(
        frame.convert("RGB")).enhance(_DARK_BRIGHTNESS)
    drained = ImageEnhance.Color(darkened).enhance(
        _DARK_SATURATION).convert("RGBA")
    drained.putalpha(alpha)
    return _tint(drained, ACCENT, _DARK_TINT)


# Offset down, blur radius, and opacity of the drop shadow, per style. Dark
# sits deeper and softer because it has to separate the sticker from footage
# that is already dark; punchy only has to stop it floating.
_SHADOW = {
    "punchy": (6, 12, 115),
    "dark": (4, 16, 153),
}

# The halo that holds the sticker's shape against whatever is behind it.
# Punchy gets a hard outline because bright footage competes with the art's
# own edges; dark gets a soft gold glow instead, because an outline on a
# dark grade reads as a sticker cut out and pasted on. Both sit under the
# art and over the shadow.
#
# The glow is 170, not the "low opacity" the spec first asked for. Measured
# over a real frame from outputs/NANDA-DEVI-real-run.mp4: at 72 it was
# invisible against mottled mid-tone footage and at 120 still weak. The glow
# exists to separate the sticker from dark footage, and below ~150 it does
# not do that job.
_HALO = {
    # style: (kind, pixels, colour, opacity)
    "punchy": ("outline", 3, (245, 245, 250), 217),
    "dark": ("glow", 10, ACCENT, 170),
}


def ground(frame: Image.Image, style: str) -> Image.Image:
    """``frame`` with a drop shadow and halo under it, on the same canvas.

    Without this the sticker sits *on* the frame rather than *in* it -- the
    third of the three things that made the emoji read as clipart. The
    shadow is baked rather than drawn by ffmpeg because the renderer's whole
    speed argument rests on the overlay having nothing to compute per frame.

    The halo holds the shape against whatever is behind it. Punchy gets a
    hard outline to compete with bright footage's edges; dark gets a soft
    gold glow instead, because an outline on a dark grade reads as cut out
    and pasted on. Both sit under the art and over the shadow.

    The art's fully opaque pixels come through untouched; its anti-aliased rim
    does not, and must not. That rim is translucent because ``matte`` softens
    the alpha to stop hard GIF edges shimmering over moving footage, and
    blending it with the halo underneath is what makes the halo read as being
    *behind* the art rather than as a ring drawn around it.

    Drawn on the existing canvas, not a larger one: the canvas is what the
    overlay pins at a fixed x/y, and growing it here would move every
    sticker off its anchor.
    """
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; expected one of {STYLES}")
    offset, radius, opacity = _SHADOW[style]

    alpha = frame.getchannel("A")
    shadow_alpha = Image.new("L", frame.size, 0)
    shadow_alpha.paste(alpha, (0, offset))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(radius))
    shadow_alpha = shadow_alpha.point(lambda v: v * opacity // 255)

    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)

    kind, spread, colour, halo_opacity = _HALO[style]
    if kind == "outline":
        # MaxFilter grows the silhouette by (size - 1) // 2 pixels, so a 3px
        # outline needs a 7-wide window. Odd sizes only.
        halo_alpha = alpha.filter(ImageFilter.MaxFilter(2 * spread + 1))
    else:
        halo_alpha = alpha.filter(ImageFilter.GaussianBlur(spread))
    halo_alpha = halo_alpha.point(lambda v: v * halo_opacity // 255)

    halo = Image.new("RGBA", frame.size, colour + (0,))
    halo.putalpha(halo_alpha)

    out = Image.alpha_composite(shadow, halo)
    out = Image.alpha_composite(out, frame)
    return out

"""Scene visuals.

Primary path is OmniRoute ``/v1/images/generations``, which is also how this
reaches a local ComfyUI later: when free image tiers start throttling at
~40 images/day, ComfyUI registers as another provider behind the same endpoint
and none of this code changes.

Fallback path matters more than it usually would. On a gateway with no image
provider configured, the placeholder *is* the visual, so it is a real
composition — dark gradient, vignette, grain, horizon haze — not a test
pattern. A run with zero providers still produces a watchable Reel.
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from engine.contract import ReelPlan

# Appended to every visual prompt so scenes read as one piece rather than ten
# unrelated images. "no text" matters: burned captions are our text layer, and
# image models love to add garbled signage.
STYLE_SUFFIX = (
    ", dark cinematic still, vertical 9:16 composition, volumetric fog, "
    "low-key lighting, desaturated teal and amber, 35mm film grain, "
    "deep shadows, ominous atmosphere, photorealistic, no text, "
    "no watermark, no people facing camera"
)

WIDTH, HEIGHT = 1080, 1920

# Cool-to-warm pairs that all sit in the same tonal family, so consecutive
# placeholder beats look deliberate instead of random.
PALETTES = [
    ((8, 14, 20), (34, 58, 66)),
    ((10, 10, 18), (58, 44, 40)),
    ((6, 16, 18), (28, 62, 58)),
    ((14, 10, 14), (66, 40, 34)),
    ((8, 12, 24), (40, 50, 78)),
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ("C:/Windows/Fonts/segoeuib.ttf",
                      "C:/Windows/Fonts/arialbd.ttf",
                      "C:/Windows/Fonts/Nirmala.ttc"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def placeholder_image(out_path: str | Path, text: str = "",
                      seed: int = 0) -> str:
    """A deterministic dark cinematic frame.

    Deterministic per seed so a re-render of the same plan produces identical
    frames, which keeps the render idempotency key meaningful.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    top, bottom = PALETTES[seed % len(PALETTES)]
    base = Image.new("RGB", (WIDTH, HEIGHT), top)
    draw = ImageDraw.Draw(base)

    # Vertical gradient.
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        eased = ratio ** 1.4
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=(int(top[0] + (bottom[0] - top[0]) * eased),
                  int(top[1] + (bottom[1] - top[1]) * eased),
                  int(top[2] + (bottom[2] - top[2]) * eased)))

    # A soft haze band, placed differently per seed, reads as a horizon.
    haze = Image.new("L", (WIDTH, HEIGHT), 0)
    haze_draw = ImageDraw.Draw(haze)
    band_y = int(HEIGHT * (0.45 + rng.random() * 0.25))
    haze_draw.ellipse(
        [-WIDTH // 3, band_y - 180, WIDTH + WIDTH // 3, band_y + 180],
        fill=70)
    haze = haze.filter(ImageFilter.GaussianBlur(120))
    glow = Image.new("RGB", (WIDTH, HEIGHT),
                     (min(bottom[0] + 60, 255), min(bottom[1] + 52, 255),
                      min(bottom[2] + 44, 255)))
    base = Image.composite(glow, base, haze)

    # Vignette.
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).ellipse(
        [-int(WIDTH * 0.35), -int(HEIGHT * 0.12),
         int(WIDTH * 1.35), int(HEIGHT * 1.12)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(220))
    base = Image.composite(base, Image.new("RGB", (WIDTH, HEIGHT),
                                           (0, 0, 0)), mask)

    # Film grain. Deterministic, and subtle enough to survive H.264.
    grain = Image.new("L", (WIDTH // 3, HEIGHT // 3))
    grain.putdata([rng.randint(112, 142)
                   for _ in range((WIDTH // 3) * (HEIGHT // 3))])
    grain = grain.resize((WIDTH, HEIGHT), Image.BILINEAR)
    base = Image.blend(base, Image.merge("RGB", (grain, grain, grain)), 0.055)

    if text:
        draw = ImageDraw.Draw(base)
        font = _font(64)
        words = text.split()
        lines: list[str] = []
        current: list[str] = []
        for word in words:
            current.append(word)
            if len(" ".join(current)) > 22:
                lines.append(" ".join(current))
                current = []
        if current:
            lines.append(" ".join(current))
        lines = lines[:4]

        y = int(HEIGHT * 0.40)
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            x = (WIDTH - (box[2] - box[0])) // 2
            draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(226, 232, 236))
            y += int((box[3] - box[1]) * 1.9) + 14

    base.save(out_path, "PNG")
    return str(out_path)


def _checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]


def generate_beat_image(client, beat, out_path: str | Path, *,
                        seed: int = 0,
                        model: str | None = None) -> tuple[str, str]:
    """Return ``(path, provider)``, falling back rather than failing.

    A dead image provider must not cost us the whole video — the rest of the
    pipeline is expensive and already paid for by this point.
    """
    out_path = Path(out_path)
    prompt = beat.visual_prompt.rstrip(" .,") + STYLE_SUFFIX
    try:
        payloads = client.image(prompt, model=model, size="1024x1792")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(payloads[0])
        # Normalise to exact frame size so the render graph can trust it.
        with Image.open(out_path) as opened:
            resized = opened.convert("RGB").resize((WIDTH, HEIGHT),
                                                   Image.LANCZOS)
        resized.save(out_path, "PNG")
        provider = ""
        for call in reversed(getattr(client, "calls", [])):
            if call.provider:
                provider = call.provider
                break
        return str(out_path), provider or "omniroute"
    except Exception:
        # No text baked in: the ASS layer already draws on_screen_text,
        # and doing both rendered it twice at different sizes.
        return placeholder_image(out_path, "", seed=seed), "placeholder"


def generate_plan_images(plan: ReelPlan, client, work_dir: str | Path,
                         store=None, *, model: str | None = None,
                         progress=None) -> dict[str, int]:
    """Fill ``beat.image_path`` for every beat; report provider counts."""
    target_dir = Path(work_dir) / plan.plan_id / "images"
    target_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for index, beat in enumerate(plan.script.beats):
        path, provider = generate_beat_image(
            client, beat, target_dir / f"{beat.beat_id}.png",
            seed=index, model=model)
        beat.image_path = path
        beat.image_provider = provider
        counts[provider] = counts.get(provider, 0) + 1
        if store is not None:
            store.save_asset(plan.plan_id, beat.beat_id, "image", provider,
                             path, checksum=_checksum(path))
        if progress:
            progress(index + 1, len(plan.script.beats), beat.beat_id, provider)

    return counts

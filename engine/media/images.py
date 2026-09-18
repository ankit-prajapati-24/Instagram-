"""Scene visuals, as a three-tier chain.

    1. OmniRoute /v1/images/generations  — FLUX and friends, needs one key
    2. a keyless public endpoint         — works with no signup at all
    3. a generated placeholder frame     — always available

Each beat walks the chain until something returns a usable image, and the tier
that answered is recorded on the beat and in the ``assets`` table. A dead
provider never costs a render: by the time images are generated the script has
already been written and approved.

Why a chain rather than just the gateway: tier 1 is intermittently
unavailable, and measurement rather than guesswork says why. Two distinct
failures were observed, and they are not the same problem:

  * A model id the image router will not take. It requires a literal
    ``provider/model``, so ``auto/best-image`` and ``agy/*`` both come back
    400 "Invalid image model" even though /v1/models lists them.
  * Quota. On 2026-09-18 the configured provider served six images and then
    returned 429 RESOURCE_EXHAUSTED for the rest of the run, with a reset
    delay of 163 hours. Chat quota was unaffected and kept working, so the
    limit is per-capability -- a run can produce a full script and still get
    no visuals.

Local generation is not an option either: this machine has Intel Iris Xe
integrated graphics, and 40 images a day on that would take hours. Tier 2
fills the gap and steps aside on its own the moment tier 1 answers again.

Tier 2 caveats, both verified: the service stamps its name in the bottom-right
even when asked not to, so the bottom of the frame is cropped; and it returns
a smaller image than requested, so the result is scaled to cover 1080x1920.
"""

from __future__ import annotations

import hashlib
import io
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
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

# Tier 2. A free public endpoint, so it is best-effort by definition: short
# timeout, and any failure just falls through to the placeholder.
KEYLESS_BASE = "https://image.pollinations.ai/prompt/"
KEYLESS_WIDTH, KEYLESS_HEIGHT = 768, 1344
KEYLESS_TIMEOUT = 75.0
KEYLESS_ATTEMPTS = 3
KEYLESS_BACKOFF = 2.5
# Its watermark sits in the bottom-right corner. Captions occupy the bottom
# ~15% of the frame anyway, so losing 7% costs very little composition.
KEYLESS_CROP_BOTTOM = 0.07

# Cool-to-warm pairs that all sit in the same tonal family, so consecutive
# placeholder beats look deliberate instead of random.
PALETTES = [
    ((8, 14, 20), (34, 58, 66)),
    ((10, 10, 18), (58, 44, 40)),
    ((6, 16, 18), (28, 62, 58)),
    ((14, 10, 14), (66, 40, 34)),
    ((8, 12, 24), (40, 50, 78)),
]

# Prefixes, and they MUST be matched with startswith. Comparing payload[:4]
# against this tuple silently rejected every JPEG, because the JPEG marker is
# only two bytes long — so a working provider looked like a dead one and the
# whole chain fell through to the placeholder.
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8", b"RIFF", b"GIF8", b"BM")


def _font(size: int):
    for candidate in ("C:/Windows/Fonts/segoeuib.ttf",
                      "C:/Windows/Fonts/arialbd.ttf",
                      "C:/Windows/Fonts/Nirmala.ttc"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


# --- tier 3: placeholder ----------------------------------------------------

def placeholder_image(out_path: str | Path, text: str = "",
                      seed: int = 0) -> str:
    """A deterministic dark frame with a suggested horizon.

    Deterministic per seed so re-rendering the same plan produces identical
    frames, which is what makes the render idempotency key meaningful.

    It carries a ridge silhouette rather than being a bare gradient. A pure
    gradient behind captions reads as a broken render, and this is the tier
    that shows when nothing else answered — it should look deliberate.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    top, bottom = PALETTES[seed % len(PALETTES)]
    base = Image.new("RGB", (WIDTH, HEIGHT), top)
    draw = ImageDraw.Draw(base)

    for y in range(HEIGHT):
        eased = (y / HEIGHT) ** 1.4
        draw.line([(0, y), (WIDTH, y)],
                  fill=(int(top[0] + (bottom[0] - top[0]) * eased),
                        int(top[1] + (bottom[1] - top[1]) * eased),
                        int(top[2] + (bottom[2] - top[2]) * eased)))

    # A haze band, placed differently per seed, reads as a horizon.
    haze = Image.new("L", (WIDTH, HEIGHT), 0)
    band_y = int(HEIGHT * (0.45 + rng.random() * 0.22))
    ImageDraw.Draw(haze).ellipse(
        [-WIDTH // 3, band_y - 180, WIDTH + WIDTH // 3, band_y + 180], fill=70)
    haze = haze.filter(ImageFilter.GaussianBlur(120))
    glow = Image.new("RGB", (WIDTH, HEIGHT),
                     (min(bottom[0] + 60, 255), min(bottom[1] + 52, 255),
                      min(bottom[2] + 44, 255)))
    base = Image.composite(glow, base, haze)

    # Two ridge silhouettes, back one lighter, so the frame has depth.
    for layer, (darkness, lift) in enumerate(((0.55, 90), (0.18, 0))):
        ridge = [(0, HEIGHT)]
        x = 0
        y = band_y + lift + rng.randint(-40, 40)
        while x <= WIDTH:
            ridge.append((x, y))
            x += rng.randint(90, 220)
            y += rng.randint(-110, 110)
            y = max(min(y, band_y + lift + 260), band_y + lift - 260)
        ridge.append((WIDTH, HEIGHT))
        shade = tuple(int(c * darkness) for c in bottom)
        ImageDraw.Draw(base).polygon(ridge, fill=shade)
        if layer == 0:
            base = base.filter(ImageFilter.GaussianBlur(3))

    # Vignette.
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).ellipse(
        [-int(WIDTH * 0.35), -int(HEIGHT * 0.12),
         int(WIDTH * 1.35), int(HEIGHT * 1.12)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(220))
    base = Image.composite(base, Image.new("RGB", (WIDTH, HEIGHT),
                                           (0, 0, 0)), mask)

    # Film grain, subtle enough to survive H.264.
    grain = Image.new("L", (WIDTH // 3, HEIGHT // 3))
    grain.putdata([rng.randint(112, 142)
                   for _ in range((WIDTH // 3) * (HEIGHT // 3))])
    grain = grain.resize((WIDTH, HEIGHT), Image.BILINEAR)
    base = Image.blend(base, Image.merge("RGB", (grain, grain, grain)), 0.055)

    if text:
        draw = ImageDraw.Draw(base)
        font = _font(64)
        box = draw.textbbox((0, 0), text, font=font)
        x = (WIDTH - (box[2] - box[0])) // 2
        y = int(HEIGHT * 0.42)
        draw.text((x + 3, y + 3), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=(226, 232, 236))

    base.save(out_path, "PNG")
    return str(out_path)


# --- normalising whatever a provider returned -------------------------------

def cover_fit(payload: bytes, out_path: str | Path, *,
              crop_bottom: float = 0.0) -> str:
    """Scale-to-cover ``payload`` into a 1080x1920 PNG.

    Cover rather than a plain resize: providers return their own aspect
    ratios, and stretching a landscape frame into 9:16 makes everything in it
    look wrong. ``crop_bottom`` removes a fraction of the source height first,
    which is how the tier-2 watermark is dealt with.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(io.BytesIO(payload)) as opened:
        image = opened.convert("RGB")

        if crop_bottom > 0:
            keep = int(image.height * (1 - crop_bottom))
            image = image.crop((0, 0, image.width, max(keep, 1)))

        scale = max(WIDTH / image.width, HEIGHT / image.height)
        scaled = image.resize((max(int(image.width * scale), WIDTH),
                               max(int(image.height * scale), HEIGHT)),
                              Image.LANCZOS)
        left = (scaled.width - WIDTH) // 2
        top = (scaled.height - HEIGHT) // 2
        scaled.crop((left, top, left + WIDTH, top + HEIGHT)).save(
            out_path, "PNG")
    return str(out_path)


# --- tier 2: keyless --------------------------------------------------------

def keyless_url(prompt: str, seed: int) -> str:
    query = urllib.parse.urlencode({
        "width": KEYLESS_WIDTH,
        "height": KEYLESS_HEIGHT,
        "model": "flux",
        "nologo": "true",
        "seed": seed,
    })
    return f"{KEYLESS_BASE}{urllib.parse.quote(prompt)}?{query}"


def fetch_keyless(prompt: str, seed: int,
                  timeout: float = KEYLESS_TIMEOUT,
                  attempts: int = KEYLESS_ATTEMPTS) -> bytes:
    """Fetch one image, retrying a few times.

    Measured: a single attempt per beat landed 3 of 10 images, the rest
    falling through to placeholders. The endpoint is free and under load, so
    it answers 500 or times out and then succeeds moments later. Retrying is
    the difference between a video of scenes and a video of gradients.
    """
    url = keyless_url(prompt, seed)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code == 200 and response.content:
                return response.content
            last = RuntimeError(f"HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(KEYLESS_BACKOFF * (attempt + 1)
                       + random.uniform(0, 1.0))
    raise RuntimeError(f"keyless image failed after {attempts} attempts: "
                       f"{last}")


# --- the chain --------------------------------------------------------------

def _looks_like_an_image(payload: bytes) -> bool:
    """Guard against a 200 that carries an HTML error page.

    Free endpoints answer rate limits with markup and a success status, and
    saving that as a beat's visual fails much later in ffmpeg.
    """
    return bool(payload) and payload.startswith(IMAGE_MAGIC)


def generate_beat_image(client, beat, out_path: str | Path, *,
                        seed: int = 0, model: str | None = None,
                        use_keyless: bool = True,
                        keyless_fetch=fetch_keyless) -> tuple[str, str]:
    """Walk the chain for one beat. Returns ``(path, provider)``."""
    out_path = Path(out_path)
    prompt = beat.visual_prompt.rstrip(" .,") + STYLE_SUFFIX

    # Tier 1 — the gateway.
    try:
        payloads = client.image(prompt, model=model, size="1024x1792")
        if payloads and _looks_like_an_image(payloads[0]):
            provider = ""
            for call in reversed(getattr(client, "calls", [])):
                if getattr(call, "provider", ""):
                    provider = call.provider
                    break
            return cover_fit(payloads[0], out_path), provider or "omniroute"
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass

    # Tier 2 — keyless, best-effort.
    if use_keyless:
        try:
            payload = keyless_fetch(prompt, seed, KEYLESS_TIMEOUT)
            if _looks_like_an_image(payload):
                return (cover_fit(payload, out_path,
                                  crop_bottom=KEYLESS_CROP_BOTTOM),
                        "keyless")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass

    # Tier 3 — always works.
    return placeholder_image(out_path, "", seed=seed), "placeholder"


def _checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]


def generate_plan_images(plan: ReelPlan, client, work_dir: str | Path,
                         store=None, *, model: str | None = None,
                         use_keyless: bool = True,
                         workers: int = 4,
                         progress=None) -> dict[str, int]:
    """Fill ``beat.image_path`` for every beat; report provider counts.

    Beats are generated concurrently. Each one is an independent HTTP call
    that spends most of its time waiting, and at roughly 45 seconds apiece a
    thirteen-beat script took ten minutes in sequence.

    ``workers`` is deliberately small. Tier 2 is a free public endpoint that
    answers 500 under load — the retry already exists because of that, and
    hammering it with thirteen parallel requests would turn a slow path into
    a failing one. Four is a compromise between wall-clock and hit rate.
    """
    target_dir = Path(work_dir) / plan.plan_id / "images"
    target_dir.mkdir(parents=True, exist_ok=True)
    beats = plan.script.beats
    counts: dict[str, int] = {}

    def make(item):
        index, beat = item
        path, provider = generate_beat_image(
            client, beat, target_dir / f"{beat.beat_id}.png",
            seed=index, model=model, use_keyless=use_keyless)
        return index, beat, path, provider

    done = 0
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        # as_completed, so progress reflects real completions rather than
        # waiting on a slow beat in the middle.
        futures = [pool.submit(make, item) for item in enumerate(beats)]
        for future in as_completed(futures):
            index, beat, path, provider = future.result()
            beat.image_path = path
            beat.image_provider = provider
            counts[provider] = counts.get(provider, 0) + 1
            done += 1
            if store is not None:
                store.save_asset(plan.plan_id, beat.beat_id, "image",
                                 provider, path, checksum=_checksum(path))
            if progress:
                progress(done, len(beats), beat.beat_id, provider)

    return counts

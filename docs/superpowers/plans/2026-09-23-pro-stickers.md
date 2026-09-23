# Pro-Level Animated Stickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the frozen emoji sticker with designed art that animates for its whole time on screen and is graded to the beat it lands on.

**Architecture:** Lordicon GIFs are fetched once and baked once into RGBA PNG sequences under `engine/data/stickers/<trigger>/<style>/`. The renderer keeps doing exactly what it does today — composite a pre-drawn PNG sequence with `overlay` — so no render-time cost or dependency changes. All GIF decoding, matting, retiming and colour work happens in a bake script that runs by hand.

**Tech Stack:** Python 3.14, Pillow (already a dependency), ffmpeg v7.1 via imageio-ffmpeg, pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-pro-stickers-design.md`

## Global Constraints

- **No new runtime dependency.** Pillow and `imageio-ffmpeg` are already in `requirements.txt`; nothing else may be added. GIF was chosen over Lottie JSON precisely to avoid `rlottie-python`, whose wheels stop at cp312 and cannot install on this project's Python 3.14.
- **`WINDOW_SECONDS = HOLD_SECONDS` (1.40)** — *not* `POP_SECONDS + HOLD_SECONDS`. `sticker_chain` emits `trim=duration=HOLD_SECONDS` and gates `enable` to `start .. start + HOLD_SECONDS`, so 1.40s is a sticker's entire visible life. A longer bake would have its tail trimmed away and never reach its final frame. This change alters what is on screen, never how long. At 30fps that is **42 frames**.
- **The trigger map stays data.** `test_a_new_trigger_needs_no_code_change` must keep passing; adding a trigger *with* art must also need no code change.
- **A missing decoration must never cost a render.** `prepare()` returns `[]` rather than raising, at every new failure point.
- **The overlay must not touch the MAIN branch's PTS.** Every emitted label re-declares `settb=1/fps`, as every label in that graph already does.
- **Attribution string, verbatim:** `Animated icons by Lordicon.com`
- **Matte cutoff:** a pixel is background-white when **all three channels are > 238**.
- **The five shipped slugs**, family `wired`, variant `flat`: `death=2130-skull-poison`, `question=424-chat-question`, `science=440-dna`, `time=196-clock-arrow-rotate-left`, `witness=2813-creepy-eye-ball`.
- Tests in this suite prove rendering behaviour with **real ffmpeg renders and pixel reads**, not by asserting on filtergraph strings. Three bugs already shipped past a green suite that only compared graph text.

## Review Focus

1. **An icon with white inside the art** — the flood fill cannot reach it, so it would render with holes. Bake must fail loudly and name the icon. *(Task 3)*
2. **A source GIF with fewer frames than the output window needs** — resampling must repeat frames cleanly, not divide by zero or drop the tail. *(Task 4)*
3. **`fps` other than 30** — `sticker_scale` and `fps` are both configurable, so the baked frame count must follow the configured fps rather than a baked-in 42. *(Task 7)*
4. **A baked directory that exists but is short a frame** — a partially written bake must fall back to the emoji path, not composite a truncated animation. *(Task 8)*
5. **A slug that 404s or returns HTML** — the fetch script must refuse to write a file that is not a decodable multi-frame GIF, rather than leave a broken artefact for the bake to trip over. *(Task 2)*

---

### Task 1: Art manifest in the trigger map

**Files:**
- Modify: `engine/assembly/stickers.py` (the `Trigger` dataclass and `load_triggers`)
- Modify: `engine/data/stickers.json`
- Test: `tests/test_stickers.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Trigger.art: dict | None` with keys `source`, `family`, `variant`, `slug`. Tasks 2, 7 and 8 read it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_stickers.py`, under the "the trigger map is data" banner:

```python
def test_a_trigger_can_carry_art_and_one_without_it_still_loads(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"triggers": [
        {"name": "death", "emoji": "\U0001f480", "weight": 10,
         "match": ["kankaal"],
         "art": {"source": "lordicon", "family": "wired",
                 "variant": "flat", "slug": "2130-skull-poison"}},
        {"name": "plain", "emoji": "❓", "weight": 1,
         "match": ["kya"]},
    ]}), encoding="utf-8")

    by_name = {t.name: t for t in stk.load_triggers(path)}

    assert by_name["death"].art["slug"] == "2130-skull-poison"
    assert by_name["death"].art["variant"] == "flat"
    assert by_name["plain"].art is None


def test_every_shipped_art_entry_is_complete():
    for trigger in stk.load_triggers():
        if trigger.art is None:
            continue
        for key in ("source", "family", "variant", "slug"):
            assert trigger.art.get(key), \
                f"{trigger.name} art is missing {key}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stickers.py -k "carry_art or shipped_art" -v`
Expected: FAIL with `TypeError: Trigger.__init__() got an unexpected keyword argument 'art'` or `AttributeError: 'Trigger' object has no attribute 'art'`.

- [ ] **Step 3: Add the field to the dataclass**

In `engine/assembly/stickers.py`, replace the `Trigger` dataclass:

```python
@dataclass(frozen=True)
class Trigger:
    name: str
    emoji: str
    weight: int
    match: tuple[str, ...]
    # Designed art for this concept, or None to keep using the emoji glyph.
    # Data, not code: a new trigger with art is still a JSON edit. Keys are
    # source, family, variant, slug -- enough to rebuild the download URL
    # without storing one, so a CDN path change is a one-line fix here.
    art: dict | None = None
```

- [ ] **Step 4: Parse it in the loader**

In `load_triggers`, replace the `triggers.append(...)` call:

```python
        art = entry.get("art")
        triggers.append(Trigger(
            name=entry["name"], emoji=entry["emoji"],
            weight=int(entry.get("weight", 5)),
            match=tuple(a.strip().lower() for a in entry["match"] if a),
            art=dict(art) if art else None))
```

- [ ] **Step 5: Add art to the five shipped triggers**

In `engine/data/stickers.json`, add an `art` object to exactly these five entries, leaving every other entry untouched:

| trigger    | slug |
|------------|------|
| `death`    | `2130-skull-poison` |
| `question` | `424-chat-question` |
| `science`  | `440-dna` |
| `time`     | `196-clock-arrow-rotate-left` |
| `witness`  | `2813-creepy-eye-ball` |

Each one takes this shape, with only `slug` differing:

```json
"art": {
  "source": "lordicon",
  "family": "wired",
  "variant": "flat",
  "slug": "2130-skull-poison"
}
```

- [ ] **Step 6: Run the full sticker suite**

Run: `python -m pytest tests/test_stickers.py -v`
Expected: PASS, including `test_a_new_trigger_needs_no_code_change` and `test_the_trigger_map_is_a_data_file_not_python`.

- [ ] **Step 7: Commit**

```bash
git add engine/assembly/stickers.py engine/data/stickers.json tests/test_stickers.py
git commit -m "feat: let a trigger carry designed art alongside its emoji"
```

---

### Task 2: Fetch the art

**Files:**
- Create: `scripts/fetch_sticker_art.py`
- Test: `tests/test_sticker_art.py` (new file)

**Interfaces:**
- Consumes: `Trigger.art` from Task 1.
- Produces: `art_url(art: dict) -> str` and `verify_gif(path: Path) -> tuple[int, int]` returning `(frames, side)`. Task 7 reads the files this writes, from `assets/lordicon/<trigger>.gif`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sticker_art.py`:

```python
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
    images = [Image.new("RGB", (side, side), colour) for _ in range(frames)]
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.fetch_sticker_art'`.

- [ ] **Step 3: Write the fetch script**

Create `scripts/fetch_sticker_art.py`:

```python
"""Download the designed sticker art named in the trigger map.

Run by hand when the art changes, never during a render. The renderer only
ever sees the baked PNG sequences that scripts/bake_stickers.py produces
from these files.

    python scripts/fetch_sticker_art.py           # only what is missing
    python scripts/fetch_sticker_art.py --force   # re-download everything

Why the GIF and not the .lottie or .li: the .li payload Lordicon serves is
an obfuscated container, and decoding it would be working around their
access control. The GIF is what Lordicon itself puts in the page's
og:image, so it is served for public consumption, and Pillow already reads
it -- which is also how this feature avoids adding a dependency.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly.stickers import load_triggers  # noqa: E402

ART_DIR = Path(__file__).resolve().parent.parent / "assets" / "lordicon"

# Only one source is implemented. An unknown one raises rather than guessing
# a URL, because a guessed URL that 404s writes an HTML error page over the
# art and the failure surfaces later, in the bake, wearing the wrong name.
_TEMPLATES = {
    "lordicon": ("https://media.lordicon.com/icons/"
                 "{family}/{variant}/{slug}.gif"),
}


def art_url(art: dict) -> str:
    """The download URL for one manifest entry."""
    source = art.get("source")
    template = _TEMPLATES.get(source)
    if template is None:
        raise ValueError(f"unknown art source {source!r}")
    return template.format(family=art["family"], variant=art["variant"],
                           slug=art["slug"])


def verify_gif(path: Path) -> tuple[int, int]:
    """``(frames, side)`` for a usable source GIF, or raise.

    A 404 from a CDN is an HTML page with a 200-shaped body as often as not,
    and a one-frame GIF is a still that would bake into the same frozen
    sticker this whole change exists to remove. Both are caught here, at the
    only point where the fix is obvious: pick another icon.
    """
    from PIL import Image

    try:
        with Image.open(path) as im:
            if im.format != "GIF":
                raise ValueError(f"{path.name} is not a GIF ({im.format})")
            frames = getattr(im, "n_frames", 1)
            width, height = im.size
    except ValueError:
        raise
    except Exception as exc:                      # unreadable bytes
        raise ValueError(f"{path.name} is not a GIF: {exc}") from exc

    if frames < 2:
        raise ValueError(f"{path.name} has a single frame, not an animation")
    if width != height:
        raise ValueError(f"{path.name} is {width}x{height}, not square")
    return frames, width


def fetch(force: bool = False, art_dir: Path | None = None) -> int:
    art_dir = art_dir or ART_DIR
    art_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for trigger in load_triggers():
        if not trigger.art:
            continue
        dest = art_dir / f"{trigger.name}.gif"
        if dest.exists() and not force:
            print(f"[art] {trigger.name}: already here")
            continue
        url = art_url(trigger.art)
        # Download beside the target and only move it into place once it
        # verifies, so a failure can never leave a half-written file that
        # the bake would happily read.
        staged = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, staged)
        try:
            frames, side = verify_gif(staged)
        except ValueError:
            staged.unlink(missing_ok=True)
            raise
        staged.replace(dest)
        written += 1
        print(f"[art] {trigger.name}: {frames} frames at {side}px  <- {url}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="re-download art that is already present")
    args = parser.parse_args()
    fetch(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make `scripts` importable**

Create `scripts/__init__.py` as an empty file, so `from scripts.fetch_sticker_art import ...` resolves in tests.

```bash
touch scripts/__init__.py
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Actually fetch the art**

Run: `python scripts/fetch_sticker_art.py`
Expected: five lines reporting frame counts, all at 400px. Confirm `assets/lordicon/` holds `death.gif`, `question.gif`, `science.gif`, `time.gif`, `witness.gif` and no `.part` files.

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/fetch_sticker_art.py tests/test_sticker_art.py assets/lordicon
git commit -m "feat: fetch designed sticker art from the trigger manifest"
```

---

### Task 3: Matte the white background away

**Files:**
- Create: `engine/assembly/sticker_art.py`
- Test: `tests/test_sticker_art.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `matte(frame: Image.Image, *, cutoff: int = 238) -> Image.Image` returning RGBA, and `interior_white(frame: Image.Image, *, cutoff: int = 238) -> int`. Task 7 calls both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sticker_art.py`:

```python
from engine.assembly.sticker_art import interior_white, matte


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
        assert interior_white(frame) == 0, \
            f"{path.name} has white inside the art; choose another icon"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -k "matte or interior or shipped_icon" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.assembly.sticker_art'`.

- [ ] **Step 3: Write the module**

Create `engine/assembly/sticker_art.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/sticker_art.py tests/test_sticker_art.py
git commit -m "feat: recover an alpha matte from white-backed source art"
```

---

### Task 4: Retime the animation onto the window

**Files:**
- Modify: `engine/assembly/sticker_art.py`
- Test: `tests/test_sticker_art.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `resample_indices(n_src: int, frames_out: int) -> list[int]`. Task 7 calls it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sticker_art.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -k resample -v`
Expected: FAIL with `ImportError: cannot import name 'resample_indices'`.

- [ ] **Step 3: Implement it**

Append to `engine/assembly/sticker_art.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -k resample -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/sticker_art.py tests/test_sticker_art.py
git commit -m "feat: retime source art onto the sticker's own window"
```

---

### Task 5: Grade each style

**Files:**
- Modify: `engine/assembly/sticker_art.py`
- Test: `tests/test_sticker_art.py`

**Interfaces:**
- Consumes: `matte` from Task 3.
- Produces: `STYLES: tuple[str, ...]`, `apply_style(frame: Image.Image, style: str) -> Image.Image`. Tasks 7 and 8 use both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sticker_art.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -k "style or saturat" -v`
Expected: FAIL with `ImportError: cannot import name 'STYLES'`.

- [ ] **Step 3: Implement the grades**

Append to `engine/assembly/sticker_art.py`:

```python
from PIL import ImageEnhance

STYLES = ("punchy", "dark")

# The gold captions.py already highlights the spoken word with
# (COLOUR_SPOKEN = &H0000D7FF, which is #FFD700 in RGB). The channel has one
# accent colour; a sticker that introduced a second would read as a
# different video's asset dropped into this one.
ACCENT = (255, 215, 0)

# How far the dark grade pulls toward ACCENT, and how much colour it keeps.
_DARK_SATURATION = 0.35
_DARK_TINT = 0.45
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

    drained = ImageEnhance.Color(frame.convert("RGB")).enhance(
        _DARK_SATURATION).convert("RGBA")
    drained.putalpha(alpha)
    return _tint(drained, ACCENT, _DARK_TINT)
```

Move the `ImageEnhance` name into the existing import line at the top of the file rather than importing twice:

```python
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/sticker_art.py tests/test_sticker_art.py
git commit -m "feat: grade sticker art punchy or dark to match the beat"
```

---

### Task 6: Ground the sticker with a shadow

**Files:**
- Modify: `engine/assembly/sticker_art.py`
- Test: `tests/test_sticker_art.py`

**Interfaces:**
- Consumes: `STYLES` from Task 5.
- Produces: `ground(frame: Image.Image, style: str) -> Image.Image`. The returned image is the same size as the input. Task 7 calls it last.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sticker_art.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -k "shadow or grounding or opaque" -v`
Expected: FAIL with `ImportError: cannot import name 'ground'`.

- [ ] **Step 3: Implement it**

Append to `engine/assembly/sticker_art.py`:

```python
# Offset down, blur radius, and opacity of the drop shadow, per style. Dark
# sits deeper and softer because it has to separate the sticker from footage
# that is already dark; punchy only has to stop it floating.
_SHADOW = {
    "punchy": (6, 12, 115),
    "dark": (4, 16, 153),
}


def ground(frame: Image.Image, style: str) -> Image.Image:
    """``frame`` with a drop shadow under it, on the same canvas.

    Without this the sticker sits *on* the frame rather than *in* it -- the
    third of the three things that made the emoji read as clipart. The
    shadow is baked rather than drawn by ffmpeg because the renderer's whole
    speed argument rests on the overlay having nothing to compute per frame.

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

    out = Image.alpha_composite(shadow, frame)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: PASS, 20 tests.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/sticker_art.py tests/test_sticker_art.py
git commit -m "feat: ground the sticker with a baked drop shadow"
```

---

### Task 7: Bake the frames

**Files:**
- Create: `scripts/bake_stickers.py`
- Test: `tests/test_sticker_art.py`

**Interfaces:**
- Consumes: `matte`, `interior_white`, `resample_indices`, `apply_style`, `ground`, `STYLES`.
- Produces: `WINDOW_SECONDS = HOLD_SECONDS` (1.40), `frame_count(fps: int) -> int`, `bake_one(gif: Path, out_dir: Path, *, style: str, size: int, fps: int) -> int`. Writes `frame-%03d.png` plus `meta.json`. Task 8 reads that layout.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sticker_art.py`:

```python
import json as _json

from scripts.bake_stickers import WINDOW_SECONDS, bake_one, frame_count


def test_the_window_is_exactly_the_span_the_chain_lets_through():
    """The chain trims to HOLD_SECONDS and gates `enable` to the same span,
    so a bake longer than that would have its tail cut and never finish."""
    from engine.assembly import stickers as stk
    assert WINDOW_SECONDS == pytest.approx(stk.HOLD_SECONDS)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_art.py -k "window or frame_count or baking or baked_frame or trapped" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.bake_stickers'`.

- [ ] **Step 3: Write the bake script**

Create `scripts/bake_stickers.py`:

```python
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
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly.sticker_art import (        # noqa: E402
    STYLES, apply_style, ground, has_trapped_background, interior_white,
    matte, resample_indices)
from engine.assembly.stickers import (           # noqa: E402
    HOLD_SECONDS, load_triggers, sticker_canvas, sticker_size)

ART_DIR = Path(__file__).resolve().parent.parent / "assets" / "lordicon"
BAKED_DIR = (Path(__file__).resolve().parent.parent / "engine" / "data"
             / "stickers")

# Exactly the visible life the emoji sticker already had, so this change
# alters what is on screen and never how long. Every timing test that passes
# today keeps passing.
#
# HOLD_SECONDS, not POP_SECONDS + HOLD_SECONDS: sticker_chain emits
# `trim=duration=HOLD_SECONDS` and gates `enable` to the same span, so 1.40s
# is the whole of it. Baking longer would put frames after the trim, and the
# animation would never reach its last one -- which is the exact failure
# this file exists to remove.
WINDOW_SECONDS = HOLD_SECONDS

LICENCE = "Animated icons by Lordicon.com"


def frame_count(fps: int) -> int:
    """How many PNGs one baked sequence holds at ``fps``."""
    return max(1, int(round(WINDOW_SECONDS * fps)))


def bake_one(gif: Path, out_dir: Path, *, style: str, size: int,
             fps: int) -> int:
    """Bake one source GIF into one style's frames. Returns the count."""
    frames_out = frame_count(fps)
    canvas = sticker_canvas(size)

    with Image.open(gif) as src:
        n_src = getattr(src, "n_frames", 1)
        sources = []
        for index in range(n_src):
            src.seek(index)
            sources.append(src.convert("RGB"))

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
    if has_trapped_background(probe):
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
            matted[src_index] = matte(sources[src_index])
        art = ground(apply_style(matted[src_index], style), style)
        art = art.resize((size, size), Image.LANCZOS)
        # Centred on the same square the emoji pop uses, so the overlay can
        # keep pinning a fixed x/y and geometry tests keep their meaning.
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        inset = (canvas - size) // 2
        frame.alpha_composite(art, (inset, inset))
        frame.save(out_dir / f"frame-{out_index:03d}.png")

    (out_dir / "meta.json").write_text(json.dumps({
        "source": gif.name, "style": style, "frames": frames_out,
        "fps": fps, "size": size, "canvas": canvas, "licence": LICENCE,
    }, indent=2), encoding="utf-8")
    return frames_out


def bake_all(*, width: int = 1080, fps: int = 30,
             scale: float = 0.17) -> int:
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
    parser.add_argument("--scale", type=float, default=0.17)
    args = parser.parse_args()
    bake_all(width=args.width, fps=args.fps, scale=args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_art.py -v`
Expected: PASS, 25 tests.

- [ ] **Step 5: Bake the real art**

Run: `python scripts/bake_stickers.py`
Expected: ten lines — five triggers × two styles — each reporting 42 frames. Confirm `engine/data/stickers/death/dark/frame-000.png` exists.

- [ ] **Step 6: Look at one, with your own eyes**

Run:

```bash
python -c "
from PIL import Image
bg = Image.new('RGBA', (900, 260), (18, 20, 26, 255))
for i, name in enumerate(['death','question','science','time','witness']):
    f = Image.open(f'engine/data/stickers/{name}/dark/frame-021.png')
    bg.alpha_composite(f, (20 + i*176, 20))
bg.convert('RGB').save('outputs/_stickers/baked_dark.png')
print('wrote outputs/_stickers/baked_dark.png')
"
```

Open it. The five should look like one set, not five downloads. If they do not, the grade constants in `sticker_art.py` are what to change — not this plan's later tasks.

- [ ] **Step 7: Commit**

```bash
git add scripts/bake_stickers.py tests/test_sticker_art.py engine/data/stickers
git commit -m "feat: bake designed art into per-style sticker frame sequences"
```

---

### Task 8: Use the baked frames

**Files:**
- Modify: `engine/assembly/stickers.py`
- Test: `tests/test_stickers.py`

**Interfaces:**
- Consumes: the baked layout from Task 7.
- Produces: `ROLE_STYLES: dict[str, str]`, `style_for_role(role: str | None) -> str`, `baked_sequence(name: str, style: str, *, fps: int, size: int, root: Path | None = None) -> tuple[str, int, int] | None`, and two new `Sticker` fields, `style: str` and `baked: bool`. Task 9 reads `Sticker.frames`; Task 10 reads `Sticker.baked`.

**Why `size` is part of the lookup:** the frames are baked at one pixel size, and `canvas_origin`/`sticker_box` derive the on-screen geometry from the canvas that size implies. A bake made for a 1080-wide render is the wrong art for a 360-wide one, and silently compositing it would put the sticker off its own anchor. Mismatches fall back to the emoji rather than render crooked.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stickers.py`, before the "real renders" banner:

```python
# --- baked art -------------------------------------------------------------

def test_reveal_and_twist_are_dark_everything_else_is_punchy():
    assert stk.style_for_role("reveal") == "dark"
    assert stk.style_for_role("twist") == "dark"
    assert stk.style_for_role("hook") == "punchy"
    assert stk.style_for_role("cta") == "punchy"
    assert stk.style_for_role("setup") == "punchy"
    assert stk.style_for_role("") == "punchy"


SHIPPED_SIZE = stk.sticker_size(1080, 0.17)      # 184


def test_a_shipped_trigger_resolves_to_its_baked_frames():
    found = stk.baked_sequence("death", "dark", fps=30, size=SHIPPED_SIZE)
    assert found is not None
    pattern, frames, canvas = found
    assert frames == 42
    assert canvas == stk.sticker_canvas(SHIPPED_SIZE)
    assert Path(pattern % 0).exists()


def test_a_trigger_without_art_has_no_baked_frames():
    assert stk.baked_sequence("ghost", "dark", fps=30,
                              size=SHIPPED_SIZE) is None


def _fake_bake(root, *, frames, meta_frames, fps=30, size=48):
    folder = root / "death" / "dark"
    folder.mkdir(parents=True)
    for index in range(frames):
        Image.new("RGBA", (8, 8)).save(folder / f"frame-{index:03d}.png")
    (folder / "meta.json").write_text(json.dumps(
        {"frames": meta_frames, "fps": fps, "size": size, "canvas": 8}),
        encoding="utf-8")
    return folder


def test_a_short_bake_is_refused_so_it_cannot_render_truncated(tmp_path):
    _fake_bake(tmp_path, frames=5, meta_frames=48)   # 5 on disk, 48 claimed
    assert stk.baked_sequence("death", "dark", fps=30, size=48,
                              root=tmp_path) is None


def test_a_bake_for_another_fps_is_not_reused(tmp_path):
    _fake_bake(tmp_path, frames=48, meta_frames=48, fps=30)
    assert stk.baked_sequence("death", "dark", fps=60, size=48,
                              root=tmp_path) is None


def test_a_bake_for_another_size_is_not_reused(tmp_path):
    _fake_bake(tmp_path, frames=48, meta_frames=48, size=184)
    assert stk.baked_sequence("death", "dark", fps=30, size=60,
                              root=tmp_path) is None


def test_prepared_stickers_carry_the_style_of_their_beat(tmp_path):
    plan = _timed(beats=4, captions=[
        "Roopkund jheel mein paanch sau kankaal mile",
        "Koi nahi jaanta ye log kaun the",
        "Sab ek hi waqt par khatam hue",
        "Tumhe kya lagta hai sach kya hai"])
    for beat, role in zip(plan.script.beats,
                          ["hook", "setup", "reveal", "cta"]):
        beat.role = role
    settings = shipped_settings()
    settings.work_dir = tmp_path

    prepared = stk.prepare(plan, settings)

    assert prepared, "expected at least one sticker"
    for sticker in prepared:
        role = plan.script.beats[sticker.beat_index].role
        assert sticker.style == stk.style_for_role(role)
```

Add `from PIL import Image` to the imports at the top of `tests/test_stickers.py` if it is not already there at module level.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stickers.py -k "role or baked or carry_the_style" -v`
Expected: FAIL with `AttributeError: module 'engine.assembly.stickers' has no attribute 'style_for_role'`.

- [ ] **Step 3: Add the style resolution and the baked lookup**

In `engine/assembly/stickers.py`, after the `CACHE_DIRNAME` constant:

```python
# Where scripts/bake_stickers.py leaves its output.
BAKED_DIRNAME = "stickers"
BAKED_ROOT = Path(__file__).resolve().parent.parent / "data" / BAKED_DIRNAME

# Which grade each beat role gets. Reveal and twist are where the video
# turns, and they carry the footage that a bright cartoon fights; hook and
# cta are where loudness earns its place. Anything unmapped gets punchy,
# because a sticker that is hard to see is worse than one that is loud.
ROLE_STYLES = {"reveal": "dark", "twist": "dark"}
DEFAULT_STYLE = "punchy"


def style_for_role(role: str | None) -> str:
    """The grade a beat of this role gets."""
    return ROLE_STYLES.get((role or "").strip().lower(), DEFAULT_STYLE)


def baked_sequence(name: str, style: str, *, fps: int, size: int,
                   root: Path | None = None
                   ) -> tuple[str, int, int] | None:
    """``(pattern, frames, canvas)`` for baked art, or None to fall back.

    Returns None rather than raising for every kind of absence -- no art for
    this trigger, a bake made at another fps or another size, a bake that is
    short a frame. A half-written sequence would composite a truncated
    animation, and a wrong-size one would sit off its own anchor because
    ``canvas_origin`` derives the geometry from the canvas; both are worse
    failures than the emoji this falls back to, and harder to notice.
    """
    folder = (root or BAKED_ROOT) / name / style
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        frames = int(meta["frames"])
        canvas = int(meta["canvas"])
        baked_fps = int(meta["fps"])
        baked_size = int(meta["size"])
    except (OSError, ValueError, KeyError):
        return None
    if baked_fps != fps or baked_size != size or frames <= 0:
        return None
    if len(list(folder.glob("frame-*.png"))) != frames:
        return None
    return str(folder / "frame-%03d.png"), frames, canvas
```

- [ ] **Step 4: Carry the style and the source on the Sticker**

Add both fields to the `Sticker` dataclass, after `slot`:

```python
    style: str       # which grade the baked frames were made with
    baked: bool      # True when designed art rendered, False for the emoji
```

`baked` exists so Task 10 can tell whether the render owes Lordicon a credit
without repeating the lookup — the credit is then derived from what actually
rendered rather than from a flag someone has to remember to set.

- [ ] **Step 5: Choose baked art in `prepare()`**

In `prepare()`, replace the body of the `for slot, cue in enumerate(cues):` loop:

```python
    for slot, cue in enumerate(cues):
        beat = plan.script.beats[cue.beat_index]
        style = style_for_role(getattr(beat, "role", None))
        found = baked_sequence(cue.name, style, fps=fps, size=size)
        if found is not None:
            pattern, frames, canvas = found
        else:
            try:
                pattern, frames, canvas = render_pop_frames(
                    cue.emoji, root, size=size, fps=fps, font_path=font)
            except StickerUnavailable as exc:
                print(f"[stickers] disabled for this render: {exc}",
                      file=sys.stderr, flush=True)
                return []
        prepared.append(Sticker(
            name=cue.name, emoji=cue.emoji, word=cue.word,
            beat_index=cue.beat_index, start=cue.start, slot=slot,
            style=style, baked=found is not None, size=size, canvas=canvas,
            frames=frames, pattern=pattern, png=pattern % (frames - 1)))
    return prepared
```

- [ ] **Step 6: Run the whole sticker suite**

Run: `python -m pytest tests/test_stickers.py -v`
Expected: PASS. If `test_a_missing_emoji_font_disables_stickers_instead_of_failing` now fails, that is correct and expected — baked art no longer needs the font. Change that test to assert the *emoji* path is what the missing font disables, by pointing it at a trigger with no art:

```python
def test_a_missing_emoji_font_disables_the_emoji_path(tmp_path):
    # "paisa" hits `money`, which ships no art, so this exercises the
    # glyph path the font is actually needed for.
    plan = _timed(beats=4, captions=["Us gaon mein paisa gaadha tha"] * 4)
    settings = shipped_settings()
    settings.work_dir = tmp_path
    settings.sticker_font = str(tmp_path / "nope.ttf")
    assert stk.prepare(plan, settings) == []
```

- [ ] **Step 7: Commit**

```bash
git add engine/assembly/stickers.py tests/test_stickers.py
git commit -m "feat: composite baked art, graded to the beat's role"
```

---

### Task 9: Delete the static hold

**Files:**
- Modify: `engine/assembly/stickers.py` (`sticker_chain`)
- Test: `tests/test_stickers.py`

**Interfaces:**
- Consumes: `Sticker.frames` from Task 8.
- Produces: no new names. `sticker_chain` keeps its signature.

- [ ] **Step 1: Write the failing test**

This is the regression that protects the headline fix. Add it below the "real renders" banner in `tests/test_stickers.py`, following the pattern the existing real-render tests use:

First add this counter beside the existing `_changed_pixels` helper, which
compares whole frames and would also see the captions highlighting:

```python
def _changed_in_box(left, right, box, width, threshold=24):
    """Changed pixels inside ``box`` only, on two rgb24 buffers.

    The captions animate word by word near the bottom of the frame, so a
    whole-frame diff cannot tell a moving sticker from a moving caption.
    The sticker's own box can.
    """
    x0, y0, w, h = box
    count = 0
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            i = (y * width + x) * 3
            if max(abs(left[i + c] - right[i + c])
                   for c in range(3)) > threshold:
                count += 1
    return count
```

Then the test itself, below the "real renders" banner:

```python
def test_the_baked_sticker_actually_moves_while_it_is_on_screen(tmp_path,
                                                                monkeypatch):
    """The whole point of this feature.

    The old sticker popped in over its first 7 frames and then held a single
    unchanging picture for 0.987 of its 1.40 visible seconds. A graph that
    composites a frozen frame and one that composites an animation produce
    the *same* filtergraph string, so this reads pixels out of a real
    render.

    The bake is done here, at the render's own size, rather than reusing the
    committed 184px one: `_render_settings` renders 360 wide, and
    `baked_sequence` refuses a bake made for another size on purpose.
    """
    from scripts.bake_stickers import bake_one

    settings = _render_settings(tmp_path)
    plan = _sticker_plan(tmp_path, settings)

    # `_sticker_plan`'s beats are role "setup" -> punchy, and its first
    # caption carries "kankaal" -> the `death` trigger, which ships art.
    size = stk.sticker_size(settings.width, settings.sticker_scale)
    baked_root = tmp_path / "baked"
    source = Path("assets/lordicon/death.gif")
    if not source.exists():                      # pragma: no cover - env
        pytest.skip("run scripts/fetch_sticker_art.py first")
    bake_one(source, baked_root / "death" / "punchy", style="punchy",
             size=size, fps=int(settings.fps))
    monkeypatch.setattr(stk, "BAKED_ROOT", baked_root)

    prepared = stk.prepare(plan, settings)
    baked = [s for s in prepared if s.baked]
    assert baked, "expected the death sticker to resolve to baked art"
    sticker = baked[0]

    out = tmp_path / "moving.mp4"
    render(plan, settings, out)

    box = stk.sticker_box(sticker.slot, settings.width, settings.height,
                          sticker.size)
    shots = [_frame_rgb(settings.ffmpeg, out, sticker.start + offset)
             for offset in (0.30, 0.70, 1.10)]

    first = _changed_in_box(shots[0], shots[1], box, settings.width)
    second = _changed_in_box(shots[1], shots[2], box, settings.width)
    assert first > 50, "the sticker is frozen between 0.30s and 0.70s"
    assert second > 50, "the sticker is frozen between 0.70s and 1.10s"
```

The three probe times all sit inside the window and before the fade begins
at 1.22s, so what they measure is the animation and not the exit.

This test bakes 48 frames before it renders, so expect it to take noticeably
longer than its neighbours. That is the cost of proving the thing with
pixels rather than with a string.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_stickers.py -k baked_sticker_actually_moves -v`
Expected: FAIL on the first assertion — the chain still holds the last frame, so the crops are identical.

- [ ] **Step 3: Remove the hold from the chain**

In `sticker_chain`, replace the first `parts.append(...)`:

```python
        parts.append(
            f"[{index}:v]format=rgba,"
            f"trim=duration={HOLD_SECONDS:.3f},setpts=PTS-STARTPTS,"
            f"fade=t=out:st={HOLD_SECONDS - FADE_OUT_SECONDS:.3f}:"
            f"d={FADE_OUT_SECONDS:g}:alpha=1,"
            f"setpts=PTS+{sticker.start:.3f}/TB,"
            f"settb=1/{fps}[stk{offset}]")
```

The `loop=loop=-1:size=1:start=...` filter is gone: the sequence now covers the whole window, so there is no last frame to hold. Everything else in the fragment is unchanged, including `settb=1/{fps}` on the label and the `overlay` fragment below it.

Update the docstring's first bullet to match:

```
    * ``trim`` bounds the window; the input then ends, and with
      ``repeatlast=0:eof_action=pass`` that is what makes ``overlay`` go
      back to passing the main stream through. There is no ``loop``: the
      baked sequence already spans the whole window, because a sticker that
      holds a single frame for three quarters of its life is what this
      replaced.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_stickers.py -k baked_sticker_actually_moves -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: PASS. `test_no_stickers_means_a_graph_identical_to_the_old_one` must still pass — with stickers off nothing in this task runs.

- [ ] **Step 6: Commit**

```bash
git add engine/assembly/stickers.py tests/test_stickers.py
git commit -m "feat: play the whole sticker animation instead of freezing it"
```

---

### Task 10: Credit Lordicon in the metadata

**Files:**
- Modify: `engine/assembly/stickers.py` (a small helper)
- Modify: `engine/publish/payloads.py:43-79` (`youtube_payload`) and `:81-100` (`instagram_payload`)
- Modify: `SETUP.md`
- Modify: `engine/app.py:1008-1012` (the only caller of both payloads)
- Test: `tests/test_stickers.py` and `tests/test_app.py:200-220` (where the payloads are already covered)

**Interfaces:**
- Consumes: `Sticker.baked` from Task 8.
- Produces: `ATTRIBUTION: str` and `attribution_for(stickers: list[Sticker]) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stickers.py`:

```python
def test_the_credit_string_is_exactly_what_the_licence_requires():
    assert stk.ATTRIBUTION == "Animated icons by Lordicon.com"


def test_the_credit_is_owed_only_when_designed_art_rendered():
    def fake(baked):
        return stk.Sticker(
            name="death", emoji="\U0001f480", word="kankaal", beat_index=0,
            start=1.0, slot=0, style="punchy", baked=baked, size=60,
            canvas=76, frames=48, pattern="x-%03d.png", png="x-047.png")

    assert stk.attribution_for([fake(True)]) == stk.ATTRIBUTION
    assert stk.attribution_for([fake(False)]) is None
    assert stk.attribution_for([fake(False), fake(True)]) == stk.ATTRIBUTION
    assert stk.attribution_for([]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stickers.py -k credit -v`
Expected: FAIL with `AttributeError: ... has no attribute 'attribution_for'`.

- [ ] **Step 3: Implement it**

Append to `engine/assembly/stickers.py`:

```python
# Lordicon's free licence requires a visible credit wherever the icons are
# used. It is derived from the stickers that actually rendered, not set as a
# flag someone has to remember, so the credit and the thing it credits
# cannot drift apart.
ATTRIBUTION = "Animated icons by Lordicon.com"


def attribution_for(stickers: list["Sticker"]) -> str | None:
    """The credit line this render owes, or None if it owes none."""
    return ATTRIBUTION if any(s.baked for s in stickers) else None
```

- [ ] **Step 4: Put it in both payloads**

`engine/publish/payloads.py` builds two descriptions. Both need the line,
because the reel goes to both platforms.

In `youtube_payload`, after the `tags_line` block and before the `return`:

```python
    credit = stickers_mod.attribution_for(stickers or [])
    if credit:
        description_parts.append(credit)
```

In `instagram_payload`, after the `tags_line` block:

```python
    credit = stickers_mod.attribution_for(stickers or [])
    if credit:
        caption = f"{caption}\n\n{credit}"
```

Both functions gain a keyword argument, defaulted so every existing caller
and test keeps working unchanged:

```python
def youtube_payload(plan: ReelPlan, video_path: str, *,
                    stickers: list | None = None) -> dict:
def instagram_payload(plan: ReelPlan, video_url: str, *,
                      stickers: list | None = None) -> dict:
```

Add the import at the top of `payloads.py`:

```python
from engine.assembly import stickers as stickers_mod
```

`engine/app.py:1008-1012` is the only caller. It already has the plan and the
rendered video in scope; pass the same prepared sticker list the render used:

```python
            "youtube": youtube_payload(plan, video, stickers=prepared),
            "instagram": instagram_payload(
                ..., stickers=prepared),
```

If the prepared list is not in scope at that point, recompute it with
`stickers_mod.prepare(plan, settings)` — it is pure lookup once the bake
exists and costs nothing.

- [ ] **Step 5: Test both payloads carry it**

Add to `tests/test_app.py`, beside the existing payload tests at lines
200-220 (reuse whatever plan fixture those tests already build):

```python
def test_both_payloads_carry_the_lordicon_credit_when_art_rendered():
    from engine.assembly import stickers as stk
    baked = [stk.Sticker(
        name="death", emoji="\U0001f480", word="kankaal", beat_index=0,
        start=1.0, slot=0, style="punchy", baked=True, size=60, canvas=76,
        frames=48, pattern="x-%03d.png", png="x-047.png")]
    plan = ...                          # the same plan the tests at
                                        # tests/test_app.py:200-220 build

    yt = payloads.youtube_payload(plan, "out.mp4", stickers=baked)
    ig = payloads.instagram_payload(plan, "https://x/v.mp4", stickers=baked)
    assert stk.ATTRIBUTION in yt["snippet"]["description"]
    assert stk.ATTRIBUTION in ig["caption"]

    plain = payloads.youtube_payload(plan, "out.mp4")
    assert stk.ATTRIBUTION not in plain["snippet"]["description"]
```

Run: `python -m pytest tests/test_stickers.py -k credit -v` and the publish
test file.
Expected: PASS.

- [ ] **Step 6: Document the art workflow**

Add to `SETUP.md`, under whatever section covers first-run assets:

```markdown
### Sticker art

The designed stickers are committed as baked PNG sequences, so a normal
render needs nothing here. Only when the art or the grade changes:

    python scripts/fetch_sticker_art.py    # download source GIFs
    python scripts/bake_stickers.py        # bake both styles

The art comes from Lordicon under its free licence, which requires a visible
credit. The panel puts `Animated icons by Lordicon.com` in the metadata
whenever a designed sticker rendered — paste it into the post description.
```

- [ ] **Step 7: Run the whole suite and commit**

Run: `python -m pytest tests/ -v`
Expected: PASS.

```bash
git add engine/assembly/stickers.py tests/test_stickers.py SETUP.md engine/
git commit -m "feat: credit Lordicon in the metadata when designed art renders"
```

---

### Task 11: Look at a real reel

**Files:** none — this is the acceptance check the spec's last risk names.

- [ ] **Step 1: Render with the real pipeline**

Run: `python scripts/sticker_frames.py --beats 10 --seconds 5 --clips --grade`

- [ ] **Step 2: Watch it**

Open the MP4 in `outputs/_stickers/`. Check, in this order:

1. Does the sticker move for its whole time on screen, or does it still settle?
2. Compressed to 1.6s, does the motion read as deliberate or as frantic? The spec names this as the retiming risk; the honest fixes are a longer `WINDOW_SECONDS` or a slower icon.
3. Over real footage under real captions, does the dark grade separate or disappear?
4. Do the five read as one set?

- [ ] **Step 3: Report, do not quietly tune**

Write down what is wrong before changing constants. `WINDOW_SECONDS`, `_DARK_SATURATION`, `_DARK_TINT` and `_SHADOW` are the four dials, and each has a test that will tell you what else moves.

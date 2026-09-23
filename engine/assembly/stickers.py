"""Emoji sticker pop-ups: a small emoji that lands on the trigger word.

Why this lives in ``engine/assembly/`` and not ``engine/media/``
---------------------------------------------------------------
``engine/media/`` acquires source material from outside the plan -- voice
from Piper, stills from an image model, footage from Pexels. Nothing here
does that. A sticker is derived *entirely* from the plan the brain already
emitted: the caption text says which emoji, ``Beat.words`` says when, and
the output is a filtergraph fragment that only the renderer consumes. That
is exactly the job ``captions.py`` does one file over -- read the plan, emit
something ffmpeg burns on top -- so this sits beside it.

What it matches, and why
------------------------
It matches ``Beat.caption_text``, not ``Beat.voice_text``.

That is forced, not preferred. ``Beat.words`` is built by
``engine.media.voice.caption_timings``, which distributes the *caption's*
Roman tokens across the beat's measured span; the Devanagari narration has a
different word count and no timings at all (Piper reports none, and edge-tts
7.2.8 emits sentence boundaries only). Matching Devanagari would give a
concept with no clock attached to it. The trigger map still carries
Devanagari aliases, because Hinglish captions do sometimes keep a word in
Devanagari, and because ``settings.captions_source`` can be flipped to
``voice_text``.

Restraint
---------
``DEFAULT_CAP`` is three. ``engine/prompts/script.txt`` already limits
``on_screen_text`` to "3-4 beats only, at the biggest moments" for the same
reason, and a sticker is a louder interruption than a word of text is. Three
over a 50-second Short is one punctuation mark every ~17 seconds. On top of
the cap: at most one sticker per beat, and ``MIN_GAP_SECONDS`` between any
two, so they read as three separate beats of emphasis rather than a burst.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from engine.contract import ReelPlan

TRIGGER_PATH = (Path(__file__).resolve().parent.parent / "data"
                / "stickers.json")

# Windows ships a full COLR colour emoji font. Verified on this machine:
# Pillow + ImageFont.truetype + draw.text(embedded_color=True) produces a
# proper full-colour glyph on transparency, so no sticker pack is needed.
DEFAULT_FONT = r"C:\Windows\Fonts\seguiemj.ttf"

# --- restraint --------------------------------------------------------------
DEFAULT_CAP = 3
# Minimum spacing between two stickers. Deliberately longer than
# HOLD_SECONDS below, which means two of them can never be on screen at
# once -- so the "one per beat" rule and this one together guarantee the
# viewer only ever has one of these to look at.
MIN_GAP_SECONDS = 2.5

# --- the pop ----------------------------------------------------------------
# 0.2s in total: up past the final size, then back down onto it. The
# overshoot is the whole point -- a monotonic ramp reads as a fade-in, and a
# fade-in reads as a lower third, not as a reaction.
POP_SECONDS = 0.20
PEAK_SECONDS = 0.12
OVERSHOOT = 1.25
# How long it stays before it leaves, and how long the exit takes. Short:
# it is punctuation, not a lower third.
HOLD_SECONDS = 1.40
FADE_OUT_SECONDS = 0.18

# --- geometry ---------------------------------------------------------------
# Size as a fraction of frame width. 0.17 of 1080 is ~184px: big enough to
# read on a phone at arm's length, small enough not to become the shot.
SIZE_FRACTION = 0.17
# The safe band. Captions are Alignment 2 with MarginV 300 and wrap to two
# or three lines of 72px, so they own everything below ~y=1360; the Punch
# style is Alignment 5, dead centre. That leaves the upper third, and 0.22
# of frame height sits under the platform chrome and above both.
ANCHOR_Y_FRACTION = 0.22
# Cycled so two stickers in a row do not land in the same hole.
ANCHOR_X_FRACTIONS = (0.30, 0.70, 0.50)

# The master glyph is drawn at least this big, so even a small frame size
# has a real glyph to downsample from rather than an upscaled one.
MIN_PNG_PX = 96
CACHE_DIRNAME = "_stickers"

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
    # The bake must span exactly the window ``sticker_chain`` lets through:
    # it trims to HOLD_SECONDS and gates ``enable`` to the same span. A
    # longer sequence has its tail cut and never reaches its final frame --
    # the mid-animation truncation this whole change exists to remove, and
    # the subject of Ruling 1. Checked here rather than trusted, because the
    # committed 42-frame bakes would stay silently accepted if HOLD_SECONDS
    # ever moved and nothing re-baked.
    if frames != round(HOLD_SECONDS * fps):
        return None
    if len(list(folder.glob("frame-*.png"))) != frames:
        return None
    return str(folder / "frame-%03d.png"), frames, canvas


_PUNCTUATION = "\"'`.,!?;:()[]{}<>\u2014\u2013-\u2018\u2019\u201c\u201d" \
               "\u0964\u0965\u2026"


class StickerUnavailable(RuntimeError):
    """No colour emoji font, or no Pillow. Stickers are decoration: the
    render must still happen without them."""


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


@dataclass(frozen=True)
class Cue:
    """A matched trigger word, placed on the render timeline."""

    name: str
    emoji: str
    word: str
    beat_index: int
    start: float           # absolute seconds from the first frame
    weight: int


@dataclass(frozen=True)
class Sticker:
    """A cue with its pop already drawn, and a slot on screen."""

    name: str
    emoji: str
    word: str
    beat_index: int
    start: float
    slot: int
    style: str       # which grade the baked frames were made with
    baked: bool      # True when designed art rendered, False for the emoji
    size: int        # resting width of the glyph
    canvas: int      # the square every pop frame is drawn on
    frames: int      # how many PNGs the pop is
    pattern: str     # ffmpeg image2 pattern for those PNGs
    png: str         # the resting frame, handy to look at


# --- the trigger map --------------------------------------------------------

def load_triggers(path: str | Path | None = None) -> list[Trigger]:
    """Read the trigger map. Data, so a new concept needs no code change."""
    path = Path(path) if path else TRIGGER_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    triggers: list[Trigger] = []
    for entry in raw.get("triggers", []):
        art = entry.get("art")
        triggers.append(Trigger(
            name=entry["name"], emoji=entry["emoji"],
            weight=int(entry.get("weight", 5)),
            match=tuple(a.strip().lower() for a in entry["match"] if a),
            art=dict(art) if art else None))
    return triggers


def normalise(token: str) -> str:
    """Lowercase and strip the punctuation a caption word arrives wearing."""
    return token.strip().strip(_PUNCTUATION).lower()


def _hits(token: str, trigger: Trigger) -> bool:
    for alias in trigger.match:
        if alias.endswith("*"):
            stem = alias[:-1]
            # A one- or two-letter stem would match half the language.
            if len(stem) >= 3 and token.startswith(stem):
                return True
        elif token == alias:
            return True
    return False


# --- picking the moments ----------------------------------------------------

def _candidates(plan: ReelPlan, triggers: list[Trigger],
                total: float) -> list[Cue]:
    cues: list[Cue] = []
    offset = 0.0
    for index, beat in enumerate(plan.script.beats):
        # No word timings means no clock, and a sticker without a clock is
        # worse than no sticker. Beats before the voice stage have none.
        for timing in beat.words:
            token = normalise(timing.word)
            if not token:
                continue
            for trigger in triggers:
                if not _hits(token, trigger):
                    continue
                start = offset + timing.start
                # It needs room to finish popping before the file ends.
                if start + POP_SECONDS > total:
                    continue
                cues.append(Cue(name=trigger.name, emoji=trigger.emoji,
                                word=timing.word, beat_index=index,
                                start=start, weight=trigger.weight))
                break
        offset += beat.seconds()
    return cues


def find_cues(plan: ReelPlan, triggers: list[Trigger] | None = None, *,
              cap: int = DEFAULT_CAP,
              min_gap: float = MIN_GAP_SECONDS) -> list[Cue]:
    """The moments that earn a sticker, in timeline order.

    Strongest first, then spread out: the cap is spent on the biggest
    moments rather than on whichever trigger happened to come first.
    """
    if cap <= 0:
        return []
    triggers = load_triggers() if triggers is None else triggers
    total = sum(beat.seconds() for beat in plan.script.beats)

    ranked = sorted(_candidates(plan, triggers, total),
                    key=lambda c: (-c.weight, c.start))
    chosen: list[Cue] = []
    for cue in ranked:
        if len(chosen) >= cap:
            break
        # One per beat: two stickers inside one line of narration is the
        # "sticker on every other word" failure the brief warns about.
        if any(c.beat_index == cue.beat_index for c in chosen):
            continue
        if any(abs(c.start - cue.start) < min_gap for c in chosen):
            continue
        chosen.append(cue)
    return sorted(chosen, key=lambda c: c.start)


# --- geometry ---------------------------------------------------------------

def sticker_size(width: int, fraction: float = SIZE_FRACTION) -> int:
    """The resting size on screen, in pixels. Even, because an odd overlay
    dimension is not representable in the yuv420p the graph runs in."""
    return max(16, int(round(width * fraction)) // 2 * 2)


def sticker_anchor(slot: int, width: int, height: int) -> tuple[int, int]:
    """The centre the sticker scales around, cycled across the safe band."""
    x = ANCHOR_X_FRACTIONS[slot % len(ANCHOR_X_FRACTIONS)]
    return int(round(width * x)), int(round(height * ANCHOR_Y_FRACTION))


def canvas_origin(slot: int, width: int, height: int,
                  canvas: int) -> tuple[int, int]:
    """Where the pop frame's square is pinned. Even, for yuv420 overlay."""
    cx, cy = sticker_anchor(slot, width, height)
    return int((cx - canvas / 2) / 2) * 2, int((cy - canvas / 2) / 2) * 2


def sticker_box(slot: int, width: int, height: int,
                size: int) -> tuple[int, int, int, int]:
    """Where the glyph actually sits at rest, as ``(x, y, w, h)``.

    Derived from ``canvas_origin`` and the same centring
    ``render_pop_frames`` uses, not from the anchor directly. The pop frame
    is a square bigger than the glyph, and its own even-rounding moves the
    glyph by a pixel; two independent roundings of "centred on the anchor"
    is how a test ends up reporting the sticker a pixel outside its own
    box.
    """
    canvas = sticker_canvas(size)
    origin_x, origin_y = canvas_origin(slot, width, height, canvas)
    inset = (canvas - size) // 2
    return origin_x + inset, origin_y + inset, size, size


# --- the pop curve ----------------------------------------------------------

def pop_scale(local_t: float) -> float:
    """The size multiplier ``local_t`` seconds after the trigger.

    The Python twin of ``pop_expr``: both are built from the same four
    constants, and the on-screen overshoot is asserted on pixels in
    ``tests/test_stickers.py`` rather than trusted to this function.
    """
    if local_t <= 0:
        return 0.0
    if local_t < PEAK_SECONDS:
        return OVERSHOOT * local_t / PEAK_SECONDS
    if local_t < POP_SECONDS:
        return OVERSHOOT - (OVERSHOOT - 1.0) * (
            (local_t - PEAK_SECONDS) / (POP_SECONDS - PEAK_SECONDS))
    return 1.0


def sticker_canvas(size: int) -> int:
    """The square every pop frame is drawn on.

    Constant across the animation on purpose: it is what lets the overlay
    sit at a fixed x/y with no per-frame expression, and the glyph grows
    and shrinks *inside* it. Big enough to hold the overshoot, and even,
    because the overlay runs in yuv420.
    """
    return (int(math.ceil(size * OVERSHOOT)) + 3) // 2 * 2


def pop_frame_count(fps: int) -> int:
    """How many PNGs the pop is, including the resting frame."""
    return max(1, int(round(POP_SECONDS * fps))) + 1


# --- the PNGs ---------------------------------------------------------------

def _codepoints(emoji: str) -> str:
    """A filesystem-safe name for an emoji, e.g. 26a0-fe0f."""
    return "-".join(f"{ord(ch):x}" for ch in emoji)


def _cache_name(emoji: str, px: int, font_path: str) -> str:
    return f"{_codepoints(emoji)}-{px}-{Path(font_path).stem}.png"


def render_sticker_png(emoji: str, cache_dir: str | Path, *,
                       px: int = 256,
                       font_path: str | None = None) -> str:
    """Draw one emoji to a square transparent PNG, cached on disk.

    No sticker pack, no downloads: Segoe UI Emoji is a colour font and
    Pillow renders it with ``embedded_color=True``.
    """
    font_path = font_path or DEFAULT_FONT
    if not Path(font_path).exists():
        raise StickerUnavailable(f"no emoji font at {font_path}")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:                   # pragma: no cover - install
        raise StickerUnavailable(f"Pillow unavailable: {exc}") from exc

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _cache_name(emoji, px, font_path)
    if target.exists():
        return str(target)

    try:
        font = ImageFont.truetype(font_path, int(px * 0.9))
    except OSError as exc:
        raise StickerUnavailable(f"{font_path} is not a usable font: "
                                 f"{exc}") from exc

    # Generous canvas, drawn well inside it: a colour glyph does not respect
    # the text anchor the way an outline glyph does, and can overshoot the
    # origin in both directions.
    pad = px * 3
    canvas = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((px, px), emoji, font=font,
                                embedded_color=True)
    box = canvas.getbbox()
    if box is None:
        raise StickerUnavailable(f"{font_path} drew nothing for {emoji!r}")
    glyph = canvas.crop(box)

    # Square it, so every frame of the pop is one number wide and the
    # sticker cannot distort as it scales.
    side = max(glyph.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(glyph, ((side - glyph.width) // 2,
                         (side - glyph.height) // 2))
    square = square.resize((px, px), Image.LANCZOS)
    square.save(target, "PNG")
    return str(target)


# --- putting it together ----------------------------------------------------

def render_pop_frames(emoji: str, cache_dir: str | Path, *, size: int,
                      fps: int, font_path: str | None = None
                      ) -> tuple[str, int, int]:
    """Draw the whole pop as a small PNG sequence, cached on disk.

    Baking the animation here instead of expressing it as
    ``scale=...:eval=frame`` in the filtergraph is a measured decision, not
    a stylistic one. ``scale`` with ``eval=frame`` rebuilds its swscale
    context on every frame it sees, which costs ~20ms a frame whatever the
    picture size: on a 1080x1920 12-second render, two animated stickers
    took it from 17.0s to 36.1s, +113%. Pre-drawn, the same two cost tenths
    of a second, because the graph is then doing nothing per frame but
    compositing a fixed-size overlay.

    Returns ``(pattern, frames, canvas)`` -- an ffmpeg image2 pattern, how
    many frames it holds, and the square they are drawn on.
    """
    canvas = sticker_canvas(size)
    frames = pop_frame_count(fps)
    cache_dir = Path(cache_dir)
    folder = cache_dir / f"pop-{_codepoints(emoji)}-{size}-{fps}"
    pattern = str(folder / "pop-%03d.png")

    existing = sorted(folder.glob("pop-*.png")) if folder.exists() else []
    if len(existing) == frames:
        return pattern, frames, canvas

    # The master glyph is drawn once at the biggest size it will ever be
    # shown, so every frame of the animation is a reduction and nothing is
    # ever upscaled.
    master = render_sticker_png(emoji, cache_dir,
                                px=max(MIN_PNG_PX, canvas),
                                font_path=font_path)

    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    glyph = Image.open(master).convert("RGBA")
    for index in range(frames):
        scale = pop_scale(index / fps)
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        side = int(round(size * scale)) // 2 * 2
        if side >= 2:
            frame.paste(glyph.resize((side, side), Image.LANCZOS),
                        ((canvas - side) // 2, (canvas - side) // 2))
        frame.save(folder / f"pop-{index:03d}.png")
    return pattern, frames, canvas


def prepare(plan: ReelPlan, settings, *,
            cache_dir: str | Path | None = None) -> list[Sticker]:
    """Every sticker this plan should get, its pop drawn and cached.

    Returns an empty list when stickers are off and when nothing triggered.
    A cue that needs the emoji fallback on a machine with no colour emoji
    font is skipped on its own -- it prints and carries on, because a
    missing decoration must never cost a render, and must not cost the
    other stickers either. Baked art needs no font, so on a box without one
    a plan still gets every designed sticker it triggered.
    """
    if not getattr(settings, "stickers", False):
        return []
    cap = int(getattr(settings, "sticker_max", DEFAULT_CAP))
    cues = find_cues(plan, cap=cap)
    if not cues:
        return []

    width = getattr(settings, "width", 1080)
    fps = int(getattr(settings, "fps", 30))
    size = sticker_size(width, getattr(settings, "sticker_scale",
                                       SIZE_FRACTION))
    root = Path(cache_dir) if cache_dir else \
        Path(getattr(settings, "work_dir", ".")) / CACHE_DIRNAME
    font = getattr(settings, "sticker_font", DEFAULT_FONT) or DEFAULT_FONT

    prepared: list[Sticker] = []
    # Counted over the stickers that survive, not over the cues, because a
    # cue can drop out below and ``canvas_origin`` cycles x-positions on the
    # slot number. Skipping a slot would leave a gap in that cycle and could
    # sit two consecutive stickers in the same place
    # (test_consecutive_stickers_do_not_reuse_the_same_spot).
    slot = 0
    for cue in cues:
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
                # Only this cue goes. The emoji fallback is the rung *below*
                # the baked art, so a machine with no colour emoji font --
                # the normal case on the Linux VPS engine/config.py
                # contemplates -- used to lose the designed stickers too,
                # which need no font at all. A missing decoration must cost
                # nothing beyond itself.
                print(f"[stickers] {cue.name} skipped, no emoji fallback "
                      f"available: {exc}", file=sys.stderr, flush=True)
                continue
        prepared.append(Sticker(
            name=cue.name, emoji=cue.emoji, word=cue.word,
            beat_index=cue.beat_index, start=cue.start, slot=slot,
            style=style, baked=found is not None, size=size, canvas=canvas,
            frames=frames, pattern=pattern, png=pattern % (frames - 1)))
        slot += 1
    return prepared


def sticker_inputs(stickers: list[Sticker], fps: int) -> list[str]:
    """ffmpeg input arguments: one image2 sequence per sticker.

    No ``-loop`` and no ``-t``. The sequence is only the pop itself -- seven
    frames at 30fps -- and the graph holds its last frame for as long as the
    sticker stays, then lets the input end, which is what tells ``overlay``
    to stop compositing.
    """
    args: list[str] = []
    for sticker in stickers:
        args += ["-framerate", str(fps), "-i", sticker.pattern]
    return args


def sticker_chain(stickers: list[Sticker], video_label: str,
                  first_index: int, *, fps: int = 30, width: int = 1080,
                  height: int = 1920) -> tuple[list[str], str]:
    """The filtergraph fragments that pop each sticker onto ``video_label``.

    Returns ``(parts, out_label)``. Per sticker:

    * ``loop`` holds the final frame and ``trim`` bounds the window; the input
      then ends, and with ``repeatlast=0:eof_action=pass`` that is what makes
      ``overlay`` go back to passing the main stream through.

      ``loop`` looks dead now and is not. A baked sequence is
      ``round(HOLD_SECONDS * fps)`` frames, so it fills the trim window on its
      own and ``loop`` never holds anything. The emoji path is seven frames --
      0.233s against a 1.400s window -- and ``loop`` is the only reason those
      stickers stay on screen for the other 1.167s. Ten triggers still take
      that path, ``water`` most of all.
    * ``setpts`` moves the whole thing onto its cue. The delay is
      deliberately NOT ``tpad`` (measured on this ffmpeg: it silently fails
      to shift the stream) and NOT ``-itsoffset`` (measured: the overlay
      then never draws at all).
    * ``enable`` gates the composite to the sticker's own window, so every
      frame outside it skips the filter entirely. That, plus a pop that is
      already drawn, is why three stickers cost ~2s on a 12-second
      1080x1920 render instead of ~19s.

    Nothing here touches the MAIN branch's timestamps -- ``overlay`` passes
    its first input's frames and PTS through unchanged -- so the narration
    invariant ``segment_lengths`` maintains is untouched. Every label the
    fragment emits re-declares ``settb=1/fps``, for the same reason every
    other label in this graph does.
    """
    parts: list[str] = []
    current = video_label
    for offset, sticker in enumerate(stickers):
        index = first_index + offset
        x, y = canvas_origin(sticker.slot, width, height,
                             sticker.canvas)
        end = sticker.start + HOLD_SECONDS
        parts.append(
            f"[{index}:v]format=rgba,"
            f"loop=loop=-1:size=1:start={sticker.frames - 1},"
            f"trim=duration={HOLD_SECONDS:.3f},setpts=PTS-STARTPTS,"
            f"fade=t=out:st={HOLD_SECONDS - FADE_OUT_SECONDS:.3f}:"
            f"d={FADE_OUT_SECONDS:g}:alpha=1,"
            f"setpts=PTS+{sticker.start:.3f}/TB,"
            f"settb=1/{fps}[stk{offset}]")
        parts.append(
            f"[{current}][stk{offset}]overlay=x={x}:y={y}:"
            f"format=yuv420:shortest=0:repeatlast=0:eof_action=pass:"
            f"enable='between(t,{sticker.start:.3f},{end:.3f})',"
            f"settb=1/{fps}[sk{offset}]")
        current = f"sk{offset}"
    return parts, current


# --- attribution -------------------------------------------------------

# Lordicon's free licence requires a visible credit wherever the icons are
# used. It is derived from the stickers that actually rendered, not set as a
# flag someone has to remember, so the credit and the thing it credits
# cannot drift apart.
ATTRIBUTION = "Animated icons by Lordicon.com"


def attribution_for(stickers: list["Sticker"]) -> str | None:
    """The credit line this render owes, or None if it owes none."""
    return ATTRIBUTION if any(s.baked for s in stickers) else None

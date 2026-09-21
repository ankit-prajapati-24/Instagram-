"""Settings for the Rahasya engine.

Everything is overridable by environment variable so the same code runs on this
machine and on a VPS later. Defaults match the Global Constraints in
docs/superpowers/plans/2026-09-17-rahasya-engine.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from engine.gates.qc import DURATION_MAX, DURATION_MIN

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _resolve_ffmpeg() -> str:
    """Prefer the wheel-bundled binary; it is the one we verified has libass."""
    explicit = os.getenv("RAHASYA_FFMPEG")
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - install problem, not logic
        # Deliberately not falling back to a bare "ffmpeg" on PATH. Caption
        # rendering needs a build with libass + libharfbuzz, and silently
        # using whatever is on PATH produces videos with broken or missing
        # subtitles instead of an error.
        raise RuntimeError(
            "no ffmpeg available: imageio-ffmpeg failed to load "
            f"({exc}). Install it with `pip install imageio-ffmpeg`, or set "
            "RAHASYA_FFMPEG to a build with libass and libharfbuzz."
        ) from exc


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw) if raw else default


@dataclass
class Settings:
    # --- OmniRoute ---------------------------------------------------------
    omniroute_base: str = field(
        default_factory=lambda: os.getenv(
            # 127.0.0.1, not localhost: on Windows localhost resolves to
            # ::1 first, and a gateway bound only to IPv4 then looks
            # "down" while curl on 127.0.0.1 answers fine.
            "OMNIROUTE_BASE", "http://127.0.0.1:20128/v1"))
    omniroute_key: str = field(
        default_factory=lambda: os.getenv("OMNIROUTE_KEY", "omniroute"))
    # Pinned, not auto/*. Measured: auto/best-chat walks a pool of dead
    # keyless providers, exhausts its retry limit and never reaches the
    # provider that actually works. These two were verified completing.
    model_strong: str = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_MODEL_STRONG", "antigravity/claude-sonnet-5"))
    model_cheap: str = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_MODEL_CHEAP", "antigravity/gemini-3.1-flash-lite"))
    model_image: str = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_MODEL_IMAGE", "antigravity/gemini-3.1-flash-image"))
    # Tier 2 of the image chain: a keyless public endpoint, used only when
    # the gateway has no image provider. Set RAHASYA_KEYLESS_IMAGES=0 to go
    # straight from the gateway to the placeholder.
    # Scenes are generated concurrently; each is an independent HTTP call
    # that mostly waits. Kept low because tier 2 is a free endpoint that
    # answers 500 under load.
    image_workers: int = field(
        default_factory=lambda: int(os.getenv("RAHASYA_IMAGE_WORKERS", "4")))
    keyless_images: bool = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_KEYLESS_IMAGES", "1").strip().lower()
        not in {"0", "false", "no", "off"})
    browser_image_api: str = field(
        default_factory=lambda: os.getenv("RAHASYA_BROWSER_IMAGE_API", "").strip())
    model_embed: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_EMBED", ""))
    pexels_api_key: str = field(
        default_factory=lambda: os.getenv("PEXELS_API_KEY", "").strip())

    # --- Voice (deliberately NOT through OmniRoute; see spec 5.1) ----------
    # Piper, chosen by ear over twelve edge-tts voices. It is also offline
    # and MIT-licensed, which retires the open question about edge-tts's
    # commercial terms. edge remains as the fallback engine.
    voice_engine: str = field(
        default_factory=lambda: os.getenv("RAHASYA_VOICE_ENGINE", "piper"))
    voice_process: bool = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_VOICE_PROCESS", "1").strip().lower()
        not in {"0", "false", "no", "off"})

    piper_voice: str = field(
        default_factory=lambda: os.getenv("RAHASYA_PIPER_VOICE", "pratham"))
    piper_models_dir: Path = field(
        default_factory=lambda: _env_path("RAHASYA_PIPER_DIR",
                                          BASE_DIR / "models" / "piper"))
    # Piper reads roughly 40% faster than edge-tts. At 1.0 a twelve-beat
    # script lands near 33s and fails the 38-52s duration check, so the
    # default is slowed — which also suits the niche.
    piper_length_scale: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_PIPER_LENGTH", "1.12")))
    piper_noise_scale: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_PIPER_NOISE", "0.667")))
    # Variation in phoneme duration: the knob that most affects whether the
    # rhythm ticks like a metronome.
    piper_noise_w: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_PIPER_NOISE_W", "0.9")))
    piper_sentence_silence: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_PIPER_SILENCE", "0.25")))

    # edge-tts, used when voice_engine is "edge" or Piper is unavailable.
    voice: str = field(
        default_factory=lambda: os.getenv("RAHASYA_VOICE",
                                          "hi-IN-MadhurNeural"))
    voice_rate: str = field(
        default_factory=lambda: os.getenv("RAHASYA_VOICE_RATE", "-8%"))
    voice_pitch: str = field(
        default_factory=lambda: os.getenv("RAHASYA_VOICE_PITCH", "-6Hz"))

    # --- Video -------------------------------------------------------------
    ffmpeg: str = field(default_factory=_resolve_ffmpeg)
    width: int = 1080
    height: int = 1920
    fps: int = 30
    transition_duration: float = 0.5
    target_seconds: float = 45.0
    # Measured on the first fully real run of this pipeline: 152 Devanagari
    # words came out as 66.5s of Piper audio (pratham, length_scale 1.12),
    # i.e. 2.29 words/sec. The script prompt is given a word budget derived
    # from this, because word count is what actually decides runtime —
    # asking for "9-13 beats" let the model write 54 words (18s) or 182
    # (60s) and still satisfy the instruction.
    #
    # It was 3.03 before, which is what scripts/measure_speech_rate.py still
    # reports (2.94 here) — and that script is why the number was wrong. Its
    # five sample lines are clean conversational Hindi with no numerals, no
    # dates and no acronyms. Piper says "1965" as "unnees sau painsath":
    # one word, eleven syllables. A line like
    # "अक्टूबर १९६५। भयंकर बर्फीला तूफान आया।" is six words in 5.1 seconds,
    # 1.18 w/s, and real scripts about real events are full of them. The
    # same measurement over the twelve-beat sample script gives 2.60 w/s and
    # over numeral-carrying lines 1.83 w/s; 2.29 is where production landed
    # between them.
    #
    # Re-measure with scripts/measure_speech_rate.py if the engine changes,
    # but treat what it prints as a ceiling, not the answer.
    #
    # Whatever it is set to, it is one number standing in for a spread: the
    # rate belongs to the content, and the measured spread across this
    # repo's evidence runs 1.83-2.94 w/s. That is why the pre-render gate
    # carries a margin over the publishing window instead of sitting exactly
    # on it — see engine.gates.qc.PRE_RENDER_MARGIN.
    words_per_second: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_WORDS_PER_SEC", "2.29")))
    # The one duration window: QC scores the rendered file against it, and
    # the pre-render length gate scores the narration against it widened by
    # qc.PRE_RENDER_MARGIN — the gate refuses scripts that are obviously
    # wrong, QC judges the ones that are merely off. Both are defined in
    # engine.gates.qc so there is only ever one pair of numbers.
    duration_min: float = DURATION_MIN
    duration_max: float = DURATION_MAX
    # One grade over every frame, so twenty-odd Pexels clips from as many
    # different creators read as one video rather than a template. Mirrors
    # voice_process: on by default, off with RAHASYA_VIDEO_GRADE=0. Turning
    # it off also removes the grain, because the grain is part of the grade.
    video_grade: bool = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_VIDEO_GRADE", "1").strip().lower()
        not in {"0", "false", "no", "off"})
    # Film grain strength, separate because it is the most taste-dependent
    # part of the look and the most likely thing to be retuned. 0 keeps the
    # colour grade and drops the grain.
    video_grain: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_VIDEO_GRAIN", "9")))

    # --- Captions ----------------------------------------------------------
    captions_source: str = field(
        default_factory=lambda: os.getenv("RAHASYA_CAPTIONS", "caption_text"))
    caption_font: str = field(
        default_factory=lambda: os.getenv("RAHASYA_FONT", "Arial"))
    caption_font_devanagari: str = field(
        default_factory=lambda: os.getenv("RAHASYA_FONT_DEVA",
                                          "Nirmala UI"))
    # 96 overflowed 1080px on real Hinglish lines; 72 wraps to
    # two comfortable lines instead.
    caption_size: int = 72

    # --- Paths -------------------------------------------------------------
    work_dir: Path = field(
        default_factory=lambda: _env_path("RAHASYA_WORK", BASE_DIR / "work"))
    out_dir: Path = field(
        default_factory=lambda: _env_path("RAHASYA_OUT", BASE_DIR / "outputs"))
    db_path: Path = field(
        default_factory=lambda: _env_path("RAHASYA_DB",
                                          BASE_DIR / "engine.db"))
    music_dir: Path = field(
        default_factory=lambda: _env_path("RAHASYA_MUSIC",
                                          BASE_DIR / "assets" / "music"))

    # --- Guardrails --------------------------------------------------------
    daily_usd_ceiling: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_DAILY_USD", "2.0")))
    dedup_trigram: float = 0.6
    dedup_cosine: float = 0.88
    entity_cooldown_days: int = 45
    beats_min: int = 9
    beats_max: int = 13

    def ensure_dirs(self) -> None:
        for d in (self.work_dir, self.out_dir, self.music_dir,
                  self.piper_models_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()


def word_budget(for_settings: Settings | None = None) -> int:
    """The script agent's word budget, formed in exactly one place.

    Word count is what decides runtime, so the budget is
    ``target_seconds * words_per_second``. Both callers — ``plan_stage`` and
    ``run_script``'s own default — come through here, because the last two
    bugs in this area were both a copy of this product going stale against
    the rate it was computed from.
    """
    active = for_settings or settings
    return int(active.target_seconds * active.words_per_second)

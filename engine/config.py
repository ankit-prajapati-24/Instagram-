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
    # Measured for Piper pratham at length_scale 1.12: 3.03 words/sec across
    # 9-, 12- and 20-word Hindi lines. The script prompt is given a word
    # budget derived from this, because word count is what actually decides
    # runtime — asking for "9-13 beats" let the model write 54 words (18s) or
    # 182 (60s) and still satisfy the instruction.
    # Re-measure with scripts/measure_speech_rate.py if the engine changes.
    words_per_second: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_WORDS_PER_SEC", "3.03")))
    duration_min: float = 38.0
    duration_max: float = 52.0

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

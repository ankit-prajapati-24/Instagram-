"""Settings for the Rahasya engine.

Everything is overridable by environment variable so the same code runs on this
machine and on a VPS later. Defaults match the Global Constraints in
docs/superpowers/plans/2026-09-17-rahasya-engine.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_ffmpeg() -> str:
    """Prefer the wheel-bundled binary; it is the one we verified has libass."""
    explicit = os.getenv("RAHASYA_FFMPEG")
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw) if raw else default


@dataclass
class Settings:
    # --- OmniRoute ---------------------------------------------------------
    omniroute_base: str = field(
        default_factory=lambda: os.getenv(
            "OMNIROUTE_BASE", "http://localhost:20128/v1"))
    omniroute_key: str = field(
        default_factory=lambda: os.getenv("OMNIROUTE_KEY", "omniroute"))
    model_strong: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_STRONG", ""))
    model_cheap: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_CHEAP", ""))
    model_image: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_IMAGE", ""))
    model_embed: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_EMBED", ""))

    # --- Voice (deliberately NOT through OmniRoute; see spec 5.1) ----------
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
    caption_size: int = 96

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
        for d in (self.work_dir, self.out_dir, self.music_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

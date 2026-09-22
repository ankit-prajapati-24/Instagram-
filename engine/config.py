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

from engine.gates.qc import duration_window

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
    # The trim pass's editor. It defaults to model_cheap -- shortening ten
    # lines that already exist is not work the strong model is needed for --
    # but it is separately settable, because this one call has a harder
    # requirement than research or metadata do: it must come back as strict
    # JSON. Measured on this gateway, agy/gemini-3.7-flash-medium leaks its
    # reasoning into the message content on roughly one round in three
    # ("Wait, what about रोशनी? ... Tokens: 1. अमावस") and the answer is
    # then unparseable; the same model behind the gateway's "no-think/"
    # alias returned clean JSON on every probe. run_script escalates to the
    # strong model after an unusable round either way, so this setting is
    # how you stop paying for that escalation, not how you avoid a failure.
    model_trim: str = field(
        default_factory=lambda: os.getenv("RAHASYA_MODEL_TRIM", "").strip())
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
    target_seconds: float = 50.0
    # Was 45.0. Real runs of the script agent write closer to 122 words for
    # a 10-beat mystery script -- 53.3s at Piper's 2.29 w/s. At a 45-second
    # target the +/-15% word-budget band is 87-118 words, and 122 falls
    # outside it, so real scripts kept failing the script gate before ever
    # reaching synthesis. At 50 the band is 96-131 words and 122 lands
    # inside it. 53s is well inside both platforms' limits -- YouTube
    # Shorts allows minutes, Instagram Reels 90s -- so 45 was this
    # pipeline's own constraint, not theirs.
    #
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
    # wrong, QC judges the ones that are merely off.
    #
    # Derived from target_seconds and qc.DURATION_TOLERANCE — the same
    # +/-15% run_script already allows on word count, carried through to
    # seconds — in __post_init__ below, not restated as a literal here. A
    # fixed 38.0-52.0 was exactly what 45 +/-15% worked out to, and it did
    # not move the day target_seconds did: move the target to 60 and the
    # 137-word budget it implies speaks for ~59.8s, outside a window a
    # literal default would never have widened.
    #
    # RAHASYA_DURATION_MIN/RAHASYA_DURATION_MAX still widen (or narrow) the
    # window independently of the target, for whoever wants that; leave
    # them unset to let it track target_seconds instead. None here is a
    # sentinel resolved in __post_init__, where target_seconds is known —
    # a plain dataclass default can't see a sibling field's value.
    duration_min: float | None = None
    duration_max: float | None = None
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

    # --- Emoji stickers ----------------------------------------------------
    # A small emoji that pops onto the frame on the word that earns it.
    # Mirrors video_grade exactly: on by default, off with
    # RAHASYA_STICKERS=0, and off is a complete off -- no inputs, no
    # overlay, a filtergraph byte-identical to the one before this feature.
    stickers: bool = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_STICKERS", "1").strip().lower()
        not in {"0", "false", "no", "off"})
    # How many may fire in one video. Three, for the same reason
    # engine/prompts/script.txt limits on_screen_text to "3-4 beats only, at
    # the biggest moments": past that they stop being emphasis and become
    # the texture of the video. Raise it if you want, but the failure mode
    # of this feature is noise, not scarcity.
    sticker_max: int = field(
        default_factory=lambda: int(os.getenv("RAHASYA_STICKER_MAX", "3")))
    # The colour emoji font the sticker PNGs are drawn from. Windows ships
    # one; a Linux VPS wants something like
    # /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf. If it is missing
    # the render still happens, without stickers.
    sticker_font: str = field(
        default_factory=lambda: os.getenv(
            "RAHASYA_STICKER_FONT",
            r"C:\Windows\Fonts\seguiemj.ttf"))
    # Resting size as a fraction of frame width. 0.17 of 1080 is ~184px.
    sticker_scale: float = field(
        default_factory=lambda: float(
            os.getenv("RAHASYA_STICKER_SCALE", "0.17")))

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
    # The script agent's target beat count. A beat used to be the unit of
    # visual change -- one beat, one still image -- so cutting the count
    # meant a motionless video. It no longer is: the media stage fills each
    # beat with several Pexels clips (a real run put 33 clips across 13
    # beats, a new shot roughly every 2s), so pacing comes from the clip
    # layer and this is free to track what word_budget() can actually fill.
    #
    # It was 12 against a 103-word budget once -- 8.6 words/beat -- and the
    # model wrote 161 words instead, failing even after the repair retry.
    # 10 keeps word_budget() / script_beats at ~10.3 words/beat, close to
    # the 11.3 the model handled fine before this budget was recalibrated
    # down from 136. See test_words_per_beat_stays_in_a_band_the_model_will_write
    # in tests/test_agents.py for the band this is checked against.
    script_beats: int = 10
    # How many times the trim pass may edit an off-budget script before the
    # stage fails. One pass moves the count reliably; landing it sometimes
    # takes a second. Three is a cap, not a plan -- past it the numbers are
    # raised rather than a wrong-length script accepted, because every round
    # is another cheap-model call and an unbounded loop here would burn the
    # daily ceiling on one video.
    trim_rounds: int = field(
        default_factory=lambda: int(os.getenv("RAHASYA_TRIM_ROUNDS", "3")))

    def __post_init__(self) -> None:
        # duration_min/duration_max default to the publishing window this
        # target_seconds implies, not a value copied at class-definition
        # time — a dataclass field default can't read a sibling field, so
        # the derivation has to happen here, once target_seconds is known.
        # RAHASYA_DURATION_MIN/RAHASYA_DURATION_MAX, if set, win either way.
        default_min, default_max = duration_window(self.target_seconds)
        if self.duration_min is None:
            self.duration_min = float(
                os.getenv("RAHASYA_DURATION_MIN", default_min))
        if self.duration_max is None:
            self.duration_max = float(
                os.getenv("RAHASYA_DURATION_MAX", default_max))

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


def speech_rate(for_settings: Settings | None = None) -> float:
    """The voice's measured words-per-second, formed in exactly one place.

    The script prompt has to state this out loud. Piper at
    ``piper_length_scale`` 1.12 reads well under conversational Hindi
    (~3.8 w/s), and a model given a duration and left to convert it itself
    uses the conversational figure — which is how a 4.4-second beat became
    17 words instead of 10. Every seconds figure the prompt shows is this
    rate applied to a word count, never the other way round.
    """
    active = for_settings or settings
    return active.words_per_second


# The floor under the average. Below this the "vary the line lengths"
# instruction has nothing left to vary.
MIN_WORDS_PER_BEAT = 4
# The narrowest a beat may get, whatever the average is.
MIN_BEAT_WORDS = 3
# Half-width of the per-beat range, as a fraction of the average. 0.6
# reproduces the hand-calibrated "4 to 18" at the 11.3 words/beat it was
# calibrated for, and tracks the average from there instead of standing
# still while the budget moves under it.
BEAT_WORD_SPREAD = 0.6


def words_per_beat(word_target: int | None = None, beats: int | None = None,
                   for_settings: Settings | None = None) -> int:
    """The average the prompt asks for: ``word_budget / beat_count``.

    Takes explicit overrides because ``run_script`` may be handed a budget
    and a beat count that are not the configured ones, and the numbers in
    the prompt must describe the pair actually in force.
    """
    active = for_settings or settings
    target = word_budget(active) if word_target is None else word_target
    count = beat_count(active) if beats is None else beats
    return max(round(target / count), MIN_WORDS_PER_BEAT)


def beat_word_range(average: int | None = None,
                    for_settings: Settings | None = None) -> tuple[int, int]:
    """The per-beat word range, symmetric around ``average``.

    It used to be the literal "4 to 18" in engine/prompts/script.txt. That
    was calibrated against 136 words over 12 beats — 11.3 a beat, which
    4-18 straddles evenly. The budget then became 103 over 10 (10.3) and
    the range did not move, so its top, 18, sat 75% above the average and
    its midpoint, 11, quietly contradicted it. The model wrote to the top
    of the range and overshot by 70%. Deriving the range keeps its midpoint
    on the average, which is the only way the two numbers can agree.
    """
    avg = words_per_beat(for_settings=for_settings) if average is None \
        else average
    spread = max(1, round(BEAT_WORD_SPREAD * avg))
    return max(MIN_BEAT_WORDS, avg - spread), avg + spread


def spoken_seconds(words: float, for_settings: Settings | None = None
                   ) -> float:
    """How long ``words`` takes in this pipeline's voice, to a tenth.

    The only sanctioned way to put a number of seconds in front of the
    model: seconds are derived from words at ``speech_rate``, so they can
    never imply a different word count than the budget does.
    """
    return round(words / speech_rate(for_settings), 1)


def trim_model(for_settings: Settings | None = None) -> str:
    """Which model edits an off-budget script, formed in one place.

    ``model_trim`` when it is set, ``model_cheap`` otherwise. Two copies of
    that fallback is how the other numbers in this module went stale.
    """
    active = for_settings or settings
    return active.model_trim or active.model_cheap


def trim_round_cap(for_settings: Settings | None = None) -> int:
    """How many trim rounds the script stage may spend, in one place.

    Lives next to ``word_budget`` and ``beat_count`` for the same reason
    they live next to each other: it is read by ``run_script``'s default and
    by anything that wants to reason about the stage's worst-case cost, and
    two copies of it would drift.
    """
    active = for_settings or settings
    return max(1, active.trim_rounds)


def beat_count(for_settings: Settings | None = None) -> int:
    """The script agent's beat count, formed in exactly one place.

    Kept next to ``word_budget`` on purpose: the two numbers together are
    what decide words-per-beat, and recalibrating one without the other is
    the bug that shipped a script the model refused to write at 8.6
    words/beat. Both ``plan_stage`` and ``run_script``'s own default come
    through here so they can never drift apart again.
    """
    active = for_settings or settings
    return active.script_beats

"""The ReelPlan contract.

One JSON object is the only thing the brain emits, and everything downstream
consumes it. If a field is not here, no stage may invent it.

The central design decision lives on ``Beat``: every beat carries
``voice_text`` in Devanagari (required for correct TTS pronunciation) and
``caption_text`` in Roman Hinglish (what gets burned on screen, because that is
the actual convention in Indian Reels and it carries no text-shaping risk).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Motion = Literal["zoom_in", "zoom_out", "move_left", "move_right"]
Transition = Literal["fade", "slide_left", "slide_right", "zoom", "blur"]
Role = Literal["hook", "setup", "escalation", "reveal", "twist",
               "cliffhanger", "cta"]
HookStyle = Literal["question", "claim", "number", "contradiction", "threat"]
Confidence = Literal["high", "medium", "low"]


# The slug becomes part of the output filename, and Windows refuses a path
# over 260 characters. Five topics pasted at once produced a 290-character
# name and ffmpeg failed with a bare "Invalid argument".
SLUG_MAX = 80


def slugify(raw: str) -> str:
    """Lowercase, strip punctuation, collapse to hyphens, cap the length.

    Devanagari is transliteration-free here: the codepoints are kept when the
    string has no ASCII content, so a Hindi-only topic still gets a stable,
    non-empty slug.

    Truncation is deterministic, so the same topic still yields the same slug
    and the same dedupe hash. Two topics sharing an 80-character prefix would
    collide — and those are near-duplicates the gate should catch anyway.
    """
    text = unicodedata.normalize("NFKC", raw).strip().lower()
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if ascii_slug:
        return ascii_slug[:SLUG_MAX].rstrip("-")
    devanagari = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-")
    return (devanagari[:SLUG_MAX].rstrip("-")
            or hashlib.sha256(raw.encode()).hexdigest()[:16])


class Coercing(BaseModel):
    """Base for models filled from LLM output.

    Models return ids as bare integers (``beat_id: 1``) and numbers as
    strings (``"4.5"``) often enough that rejecting them throws away a whole
    multi-call run over a type that is trivially convertible. The *shape* is
    still enforced — only these near-misses are absorbed.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_scalars(cls, value, info):
        field = cls.model_fields.get(info.field_name)
        if field is None or value is None:
            return value
        annotation = str(field.annotation)
        if "str" in annotation and isinstance(value, (int, float)):
            return str(value)
        if "float" in annotation and isinstance(value, str):
            try:
                return float(value.strip().rstrip("s").strip())
            except ValueError:
                return value
        return value


class WordTiming(BaseModel):
    word: str
    start: float
    end: float


class Clip(Coercing):
    """One stock-footage slot inside a beat.

    ``duration`` is the slot this clip fills on the narration timeline, not
    the length of the source file. Source footage is trimmed or looped to
    match; its own length never moves the timeline.
    """

    path: str
    query: str
    provider: str
    duration: float
    source_url: str | None = None
    pexels_id: int | None = None
    author: str | None = None
    licence: str | None = None


class Topic(Coercing):
    raw: str
    slug: str
    entities: list[str] = Field(default_factory=list)
    dedupe_hash: str

    @classmethod
    def make(cls, raw: str, entities: list[str] | None = None) -> "Topic":
        slug = slugify(raw)
        return cls(raw=raw.strip(), slug=slug,
                   entities=entities or [],
                   dedupe_hash=hashlib.sha256(slug.encode()).hexdigest())


class Hook(Coercing):
    variant_id: str
    voice_text: str
    caption_text: str
    style: HookStyle
    seconds: float = 3.0


class Beat(Coercing):
    beat_id: str
    role: Role
    voice_text: str
    caption_text: str
    on_screen_text: str | None = None
    target_seconds: float
    visual_prompt: str
    motion: Motion
    transition: Transition

    # Filled by the media stage, never by the model.
    image_path: str | None = None
    image_provider: str | None = None
    audio_path: str | None = None
    # Which TTS engine actually produced this beat. Mirrors image_provider:
    # the fallback used to be silent, so a run that quietly used edge-tts was
    # indistinguishable from one that used Piper until someone listened.
    voice_engine: str | None = None
    measured_seconds: float | None = None
    # Caption-aligned timings (Roman), not the Devanagari narration's.
    words: list[WordTiming] = Field(default_factory=list)
    # Stock footage filling this beat's span. Empty means the render falls
    # back to image_path for the whole beat.
    clips: list[Clip] = Field(default_factory=list)
    # How many boundary spans the TTS service reported, for diagnostics.
    spoken_words: int | None = None

    def seconds(self) -> float:
        return (self.measured_seconds
                if self.measured_seconds is not None
                else self.target_seconds)


class Script(Coercing):
    total_seconds: float
    chosen_hook: str
    beats: list[Beat] = Field(default_factory=list)


class Metadata(Coercing):
    yt_title: str = ""
    yt_description: str = ""
    ig_caption: str = ""
    pinned_comment: str = ""
    hashtags: list[str] = Field(default_factory=list)
    thumbnail_prompt: str = ""


class Claim(Coercing):
    beat_id: str | None = None
    text: str
    source_url: str | None = None
    confidence: Confidence = "medium"


class Provenance(Coercing):
    claims: list[Claim] = Field(default_factory=list)
    searched_queries: list[str] = Field(default_factory=list)
    # Named people, places and organisations. These drive the 45-day cooldown
    # layer of the dedup gate, which is inert without them.
    entities: list[str] = Field(default_factory=list)


class Safety(BaseModel):
    moderation_passed: bool = False
    flags: list[str] = Field(default_factory=list)
    # Set when the moderation endpoint could not be reached at all. That is a
    # setup gap, not a verdict, and it must stay visible rather than passing
    # silently as "clean".
    moderation_unavailable: str | None = None


class Cost(BaseModel):
    usd: float = 0.0
    by_provider: dict[str, float] = Field(default_factory=dict)
    fallback_attempts: int = 0


class ReelPlan(BaseModel):
    schema_version: str = "1.0"
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    topic: Topic
    hooks: list[Hook] = Field(default_factory=list)
    script: Script
    metadata: Metadata | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    safety: Safety = Field(default_factory=Safety)
    cost: Cost = Field(default_factory=Cost)

    def duration(self) -> float:
        return sum(beat.seconds() for beat in self.script.beats)

    def chosen(self) -> Hook | None:
        for hook in self.hooks:
            if hook.variant_id == self.script.chosen_hook:
                return hook
        return self.hooks[0] if self.hooks else None

    def all_text(self) -> str:
        """Everything a moderation or banned-phrase pass needs to see."""
        parts: list[str] = []
        for beat in self.script.beats:
            parts.extend([beat.voice_text, beat.caption_text])
            if beat.on_screen_text:
                parts.append(beat.on_screen_text)
        if self.metadata:
            parts.extend([self.metadata.yt_title, self.metadata.ig_caption,
                          self.metadata.pinned_comment])
        return "\n".join(p for p in parts if p)

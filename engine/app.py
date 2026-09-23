"""FastAPI app: the local control room.

Four routes matter beyond CRUD, and two of them are human gates:

  ``POST /api/plan/{id}/approve``        gate one. Nothing is produced until
                                        this has been called with a chosen
                                        hook.
  ``POST /api/plan/{id}/produce``        voice, length and clips; then either
                                        straight on to the render, or a stop
                                        at one of the two later gates when
                                        ``review_voice`` or ``review_clips``
                                        is set.
  ``POST /api/plan/{id}/voice/approve``  gate two. The plan sits in
                                        ``awaiting_voice_review`` until this
                                        releases it. Voice comes before
                                        clips because every number the clip
                                        stage uses — how many clips a beat
                                        gets, how long each one is — is
                                        derived from the measured narration.
  ``PATCH /api/plan/{id}/voice/{beat}`` correct one beat's words and say
                                        that beat again. Sits at gate two
                                        because it is the last point at
                                        which wording is free to change:
                                        after CLIPS the footage has been
                                        cut to the length the old wording
                                        measured.
  ``POST /api/plan/{id}/clips/approve``  gate three. The plan sits in
                                        ``awaiting_clip_review`` until this
                                        releases it, and only this route can
                                        start the render half of a reviewed
                                        run.
  ``GET  /api/events/{id}``              server-sent progress, so a 45-second
                                        render is watchable rather than a
                                        spinner.

There is no publish route. Payload builders are exposed for copy-out only.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import get_args

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.agents import latin_words
from engine.assembly import sticker_catalog
from engine.assembly import sticker_choices as sticker_choices_mod
from engine.assembly import stickers as stickers_mod
from engine.assembly.render import VIDEO_SUFFIXES
from engine.config import (Settings, beat_count, beat_word_range,
                           speech_rate, spoken_seconds, word_budget,
                           words_per_beat)
from engine.contract import (Beat, Claim, CleanupInfo, Clip, Metadata, Motion,
                             Role, Transition)
from engine.gates.qc import pre_render_range
from engine.media.align import build_aligner
from engine.media.voice import (MIN_UPLOAD_SECONDS, UPLOAD_ENGINE,
                                UploadRejected, apply_beat_audio,
                                beat_audio_path, ingest_narration,
                                speak_beat)
from engine.omniroute import OmniRouteClient
from engine.pipeline import (BudgetError, GateError, ManualScriptError,
                             PipelineEvent, Stage, budget_report,
                             clip_providers, clips_stage, default_roles,
                             manual_plan_stage, plan_stage, produce_stage,
                             render_stage, voice_engines, voice_stage)
from engine.publish.payloads import (instagram_payload, publish_checklist,
                                     youtube_payload)
from engine.store import Store

UI_DIR = Path(__file__).resolve().parent / "ui"


def _poster_path(clip_path: Path) -> Path:
    """Where a video clip's cached poster frame lives: next to the clip.

    The scene strip requests every beat's frame on each page load, so this
    has to be stable across requests rather than a temp file.
    """
    return clip_path.with_name(clip_path.name + ".poster.jpg")


def _under_roots(path: Path, roots: list[Path]) -> bool:
    """Is ``path`` (resolved) inside one of ``roots``? Mirrors ``/media``'s
    own containment check (``root not in target.parents``), generalised to
    more than one allowed root: a beat's clip lives under ``work_dir`` and
    its legacy still can live under either ``work_dir`` or ``out_dir``."""
    resolved = path.resolve()
    return any(root.resolve() in resolved.parents for root in roots)


def _extract_poster(clip_path: Path, poster_path: Path,
                    settings: Settings) -> None:
    """Grab a single frame from ``clip_path`` and write it to ``poster_path``.

    Extraction lands in a private temp file next to the cache path first and
    is renamed onto ``poster_path`` only once ffmpeg exits 0 *and* the file
    it wrote is non-empty. Without this, an ffmpeg that exits 0 but writes a
    truncated/undecodable jpg (or that exits non-zero after already writing
    a partial file, plausible with ``-y`` plus a mid-stream decode failure)
    would leave something sitting at the cache path -- and because the route
    only re-extracts when the cache path is *absent*, that corrupt leftover
    would then be served, unchanged, to every request after the first.

    Same subprocess shape as ``engine.media.piper_voice``: run, check the
    return code and the output, and raise with ffmpeg's own stderr rather
    than swallowing the failure.
    """
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=poster_path.name + ".",
                                   suffix=".tmp",
                                   dir=str(poster_path.parent))
    os.close(fd)
    tmp_path = Path(raw_tmp)
    try:
        result = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(clip_path), "-frames:v", "1", "-q:v", "3",
             # ffmpeg otherwise picks the muxer from the output filename's
             # extension, and the temp file's real extension is ".tmp" (it
             # ends in ".jpg.<random>.tmp", not ".jpg") -- explicit -f
             # mjpeg makes that independent of the temp name's shape.
             "-f", "mjpeg", str(tmp_path)],
            capture_output=True)
        if (result.returncode != 0 or not tmp_path.is_file()
                or tmp_path.stat().st_size == 0):
            detail = result.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError(
                f"ffmpeg could not extract a frame from {clip_path}: "
                f"{detail}")
        os.replace(tmp_path, poster_path)
    finally:
        # A no-op once os.replace has moved it away; cleans up after a
        # failure, which is exactly when something would otherwise be left
        # behind at a path other than poster_path.
        tmp_path.unlink(missing_ok=True)


def _serve_visual(path: Path, roots: list[Path],
                  settings: Settings) -> FileResponse | None:
    """One clip's browser-safe picture, or ``None`` if there isn't one.

    A still is served as-is; a video gets the poster frame extracted and
    cached beside it. Factored out of the frame route so that asking for
    one *slot* and asking for a beat's first usable visual run exactly the
    same containment checks and the same cache rules — a second copy of
    this is how the route would grow a path that skips ``_under_roots``.
    """
    if not path.is_file() or not _under_roots(path, roots):
        return None
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return FileResponse(path)
    poster_path = _poster_path(path)
    if not _under_roots(poster_path, roots):
        return None
    stale = (poster_path.is_file()
             and poster_path.stat().st_mtime < path.stat().st_mtime)
    if not poster_path.is_file() or stale:
        try:
            _extract_poster(path, poster_path, settings)
        except RuntimeError:
            return None
    if poster_path.is_file():
        return FileResponse(poster_path)
    return None


# --- clip replacement uploads ----------------------------------------------
#
# This is the one route on this server that takes arbitrary bytes from a
# browser, writes them to disk and then hands the path to ffmpeg, so every
# guard on it is written out here rather than spread through the handler.
#
# The rules, and why each one:
#
#   where        ``work_dir/<plan_id>/uploads/``. Under ``work_dir`` because
#                that is already one of the two roots ``_under_roots``
#                admits, so the thumbnail route can serve an upload without
#                widening its containment check; under the plan id because a
#                replacement belongs to one plan and is thrown away with it.
#   the name     built here from the beat id and the slot number, both of
#                which this server validated against the stored plan before
#                anything was written. The browser's filename is validated
#                (below) and then *discarded* — it never reaches a path.
#   the type     decided by sniffing the first bytes, never by the
#                extension and never by the declared Content-Type. The
#                declared type only has to agree about image-vs-video; the
#                stored extension comes from the sniff, and that extension
#                is what decides the still branch in ``plan_inputs``.
#   really media the sniff is eight bytes, so it is also asked to decode:
#                ffmpeg must read a video frame out of the file before it is
#                accepted. A PNG header in front of 512 zero bytes sniffs
#                perfectly and is not a picture.
#   how big      ``settings.upload_max_mb``, enforced while the body is
#                streaming, so an oversized upload stops at the cap instead
#                of being written out in full and measured afterwards.

UPLOAD_KINDS: dict[str, tuple[str, str]] = {
    "image/png": (".png", "image"),
    "image/jpeg": (".jpg", "image"),
    "image/webp": (".webp", "image"),
    "video/mp4": (".mp4", "video"),
    "video/quicktime": (".mov", "video"),
    "video/webm": (".webm", "video"),
}

# Browsers and operating systems disagree about these. The declared type is
# only ever used to agree with the bytes, never to name the file, so mapping
# the common spellings costs nothing and stops a legitimate .jpg being
# refused because Windows called it image/pjpeg.
CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "video/x-m4v": "video/mp4",
    "video/x-quicktime": "video/quicktime",
    "video/x-matroska": "video/webm",
    "application/mp4": "video/mp4",
}

# The provider written onto a replaced clip. Sits alongside "pexels",
# "keyless" and "placeholder" for exactly the same reason those exist: a
# clip a human chose must never be indistinguishable from one the search
# found, in the plan, on the review board or on the publish checklist.
UPLOAD_PROVIDER = "upload"

# The later gates' parking statuses, alongside "awaiting_approval". A gate
# is a row in the store, not a disabled button: a reload, a retry or a curl
# all meet the same refusal.
CLIP_REVIEW_STATUS = "awaiting_clip_review"
VOICE_REVIEW_STATUS = "awaiting_voice_review"

# --- uploaded narration ----------------------------------------------------
#
# Deliberately shorter than the clip rules above, because the checks that
# matter for audio happen somewhere else. ``ingest_narration`` re-encodes
# every upload into Piper's exact format (24 kHz mono mp3) and normalises
# it into Piper's loudness band, and to do that it has to fully decode the
# file and measure it. That is a far stronger proof than the clip route's
# "can ffmpeg read one frame", so this layer only has to answer a cheaper
# question: are these bytes plausibly a media container at all, so an
# obvious mistake — a PDF, a screenshot — is refused before ffmpeg is
# started on it.
#
# Two things the clip route does are therefore left out on purpose:
#
#   * the stored extension is not decided here. Every upload becomes
#     ``.mp3`` because every upload is transcoded, so there is nothing for
#     a sniffed extension to choose.
#   * the declared Content-Type is not cross-checked against the bytes.
#     Browsers type .m4a, .opus and .flac inconsistently enough that the
#     check would refuse real files, and it was never the security
#     boundary — the server-built filename, the containment check, the
#     size cap and the decode are.
#
# Video containers are accepted: somebody handing over the .mp4 their phone
# recorded means the sound in it, and ``ingest_narration`` passes ``-vn``.
# A container with no audio stream in it is refused there, by measurement.
AUDIO_CONTAINERS = {
    "audio/mpeg", "audio/wav", "audio/mp4", "audio/ogg", "audio/flac",
    "audio/webm", "video/mp4", "video/quicktime", "video/webm",
}

# The extension the byte-identical raw copy is written with (Global
# Constraint 3: the raw upload is never destroyed). One entry for every
# value ``_sniff_audio`` can actually return, so the raw file's own name
# says what container it is rather than lying with ``.mp3`` -- the cleaned
# file next to it is always mp3 because it is always transcoded, but the
# raw copy is neither, and naming it ``.mp3`` would make it look decodable
# by a tool that trusts extensions.
RAW_UPLOAD_SUFFIX = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "video/quicktime": ".mov",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _sniff_audio(head: bytes) -> str | None:
    """The container these bytes claim to be, or ``None``.

    Signatures only, and only enough of them to separate "a media file" from
    "not a media file". ffmpeg decides everything after that.
    """
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"                       # MPEG audio frame sync
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"OggS"):
        return "audio/ogg"                        # vorbis and opus
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"                       # EBML: webm and mkv
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime"
        # M4A, M4B, mp42, isom: one container, and ffmpeg reads the audio
        # out of all of them.
        return "audio/mp4"
    return None

# A path separator, a drive colon, a control character or a traversal
# segment. None of these belong in something a browser called a filename.
_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\:]")


def _normalise_type(raw: str | None) -> str:
    """The declared Content-Type, lowercased and stripped of parameters."""
    value = (raw or "").split(";")[0].strip().lower()
    return CONTENT_TYPE_ALIASES.get(value, value)


def _sniff_media(head: bytes) -> str | None:
    """The media type the bytes themselves claim, or ``None``.

    Signatures only — this is the first of two checks, not the whole of it.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        # ISO base media: mp4, m4v and QuickTime all share it, and the
        # major brand is what separates them.
        brand = head[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"         # EBML: webm and mkv
    return None


def _safe_client_filename(raw: str | None) -> str:
    """Refuse an attacker-controlled filename that is shaped like a path.

    Nothing downstream uses the result to build a path — the stored name is
    made from the beat id and the slot — so this is not sanitisation in the
    sense of cleaning something up for use. It is a refusal: a browser
    sending ``../../../../evil.png`` is not a browser picking a file, and
    quietly basenaming it would hide that.
    """
    if raw is None:
        return ""
    name = raw.strip()
    if not name:
        return ""
    if (len(name) > 255 or _FILENAME_BAD.search(name) or ".." in name
            or name in {".", ".."}):
        raise HTTPException(
            400, "that filename carries a path separator, a traversal "
                 "segment or a control character. Uploads are stored under "
                 "a name this server builds, so nothing was going to use "
                 "yours — but a name shaped like a path is refused outright "
                 "rather than quietly cleaned.")
    return name


def _decodes_as_media(path: Path, settings: Settings) -> bool:
    """Can ffmpeg actually read a video frame out of this file?

    The second half of the media check. A signature is eight bytes and
    anything can carry one; this is the only question that matters, because
    the render is going to ask ffmpeg exactly this and a file that fails it
    would fail the render instead — twelve minutes later, with a filtergraph
    error nobody can read. A still image is a one-frame video stream to
    ffmpeg, so the same probe covers both branches.
    """
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-i", str(path),
         "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
        capture_output=True)
    return result.returncode == 0


def _finite_or_none(value: float) -> float | None:
    """``value``, unless it is ``-inf``/``+inf``/``nan``, in which case
    ``None``.

    loudnorm reports ``-inf`` for anything under its ~400ms gating block,
    and that literal float serialises to JSON ``null`` under this
    project's pydantic version, which then fails to validate back into a
    required ``float`` field on the very next read. ``None`` is the
    version of "not measurable" that survives the round trip (final
    review, Minor 4).
    """
    return value if math.isfinite(value) else None


def _cleanup_info(report) -> CleanupInfo:
    """Copy a ``CleanupReport`` (a plain dataclass, private to one ingest
    call) onto the pydantic shape that is allowed to sit on a beat and
    reach the store. Shared by the upload route and the cleanup-toggle
    route so the two never describe the same dataclass two different ways.
    """
    return CleanupInfo(
        seconds_before=report.seconds_before,
        seconds_after=report.seconds_after,
        loudness_before=_finite_or_none(report.loudness_before),
        loudness_after=_finite_or_none(report.loudness_after),
        filters_applied=report.filters_applied,
        cleanup_abandoned=report.cleanup_abandoned,
    )


def _cleaned(info: CleanupInfo | None) -> bool | None:
    """Did what's currently written for this beat actually go through the
    cleanup chain?

    ``None`` when there is nothing to report on. Otherwise: filters were
    requested *and* they were not abandoned -- an abandoned cleanup (the
    signal it emptied) falls back to normalising the raw signal instead, so
    what got written was not cleaned even though cleanup was asked for.
    This is the distinction the brief calls out by name: "cleanup was off"
    and "cleanup was attempted and abandoned" must not collapse into the
    same answer. Both report ``cleaned=False`` here, but the full
    ``cleanup`` object alongside it still carries ``cleanup_abandoned`` to
    tell them apart.
    """
    if info is None:
        return None
    return bool(info.filters_applied) and not info.cleanup_abandoned


def _cleanup_response(report) -> dict:
    """The CleanupReport fields, flattened into a response dict. Shared by
    the upload route and the cleanup-toggle route (Global Constraint 4)."""
    return {
        "seconds_before": round(report.seconds_before, 2),
        "seconds_after": round(report.seconds_after, 2),
        "loudness_before": round(report.loudness_before, 1),
        "loudness_after": round(report.loudness_after, 1),
        "filters_applied": report.filters_applied,
        "cleanup_abandoned": report.cleanup_abandoned,
    }


class PlanRequest(BaseModel):
    topic: str
    use_fake: bool = False


class BeatEdit(BaseModel):
    """A human's edit to one beat.

    motion and transition are the contract's Literals, not plain strings.
    They used to be `str`, and pydantic v2's `model_copy(update=...)` does not
    validate, so posting {"motion": "spin"} stored an unparseable plan: every
    later read of it raised ValidationError, giving a 500 on that plan forever
    with no repair path.
    """

    beat_id: str
    voice_text: str
    caption_text: str
    on_screen_text: str | None = None
    visual_prompt: str
    motion: Motion
    transition: Transition


class ManualBeat(BaseModel):
    """One row of the blank authoring form.

    Same Literal types as ``BeatEdit``, and for the same reason: a bad
    motion posted here would be persisted unvalidated by any later
    ``model_copy`` and brick the plan. FastAPI refuses it with a 422 before
    it reaches the builder.
    """

    role: Role
    voice_text: str
    caption_text: str
    on_screen_text: str | None = None
    visual_prompt: str
    motion: Motion = "zoom_in"
    transition: Transition = "fade"


class ManualPlanRequest(BaseModel):
    """A script a human wrote, instead of one a model wrote.

    ``sources`` are the contract's own ``Claim`` objects, not a parallel
    shape — they land in ``plan.provenance.claims`` untouched, which is what
    QC and the publish payloads already read. ``metadata`` is likewise the
    contract's ``Metadata``: omitted or left blank, the plan simply has none.
    """

    topic: str
    beats: list[ManualBeat]
    entities: list[str] = Field(default_factory=list)
    sources: list[Claim] = Field(default_factory=list)
    acknowledge_unsourced: bool = False
    metadata: Metadata | None = None


class ApproveRequest(BaseModel):
    chosen_hook: str
    beats: list[BeatEdit] | None = None


class ProduceRequest(BaseModel):
    captions_source: str = "caption_text"
    use_fake: bool = False
    # Stop after VOICE and let every beat be listened to, and replaced with
    # narration the user supplies. Earlier than the clip gate because the
    # clip stage reads what this one may change: a beat's clip count is
    # ``ceil(measured / 2.5)`` and its slot lengths divide the measured
    # span, so footage fetched before the audio is final is footage cut to
    # the wrong length.
    #
    # Both flags may be set. The run then stops here first; whether it also
    # stops at the clip gate is decided again when this one is released,
    # because by then the user has heard the beats and may have changed
    # their mind.
    review_voice: bool = False
    # The clip gate, expressed as a flag on the request that starts the
    # work rather than as a separate "produce half" route.
    #
    # Why a flag and not two endpoints for the first half: the caller's
    # choice is not *which stages to run* — VOICE, LENGTH and CLIPS run
    # either way — it is only whether the pipeline stops afterwards. Two
    # near-identical routes for that would be two places to keep the stage
    # order right, and the stage order going stale in one of two copies is
    # this repo's most-repeated bug.
    #
    # Off by default, because the one-shot path has to stay: a user who
    # does not want to look at twenty clips must not be made to.
    review_clips: bool = False


class ReleaseRequest(BaseModel):
    """What the clip gate accepts. Everything optional: releasing the
    review is a decision, not a form."""

    captions_source: str | None = None


class BeatTextEdit(BaseModel):
    """A correction to one beat's words, made at the voice gate.

    Both optional and both independent. Sending only ``voice_text`` fixes
    a mispronunciation without touching what the viewer reads; sending
    only ``caption_text`` fixes the burned text without saying anything
    again. Omitting both is refused rather than read as "say it again".
    """

    voice_text: str | None = None
    caption_text: str | None = None


class CleanupToggleRequest(BaseModel):
    """What the cleanup-toggle route accepts: cleanup on or off, nothing
    else. Re-ingesting is driven entirely by the beat's stored raw and the
    settings already on the server."""

    enabled: bool


class VoiceReleaseRequest(ReleaseRequest):
    """What the voice gate accepts.

    ``review_clips`` is asked again here rather than remembered from the
    original produce request. Releasing a gate is a fresh decision: a user
    who has just spent ten minutes replacing narration by hand is in a
    different position than they were when they ticked a box, and carrying
    the old answer forward would quietly decide for them.

    ``use_fake`` is here and not on the clip gate because this gate is
    followed by CLIPS, which does call a model to turn a beat into a search
    phrase. The clip gate is followed only by ffmpeg.
    """

    review_clips: bool = False
    use_fake: bool = False


class StickerChoice(BaseModel):
    """What the sticker-choose route accepts: one catalogue slug.

    Module level like every other request model here, not local to the
    route -- this file has ``from __future__ import annotations`` at the
    top, so every parameter annotation is a string until something resolves
    it. FastAPI resolves them with ``get_type_hints``, which reads the
    function's ``__globals__``; a class defined inside ``create_app`` lives
    in that function's locals instead, so the lookup fails and FastAPI
    quietly falls back to treating ``body`` as an unrecognised query
    parameter rather than the request body. Confirmed on this branch: the
    route 422s with "Field required" on a body that was actually sent.
    """

    slug: str = Field(min_length=1, max_length=120)


def create_app(db_path: str | Path | None = None,
               settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if db_path is not None:
        settings.db_path = Path(db_path)
    settings.ensure_dirs()

    store = Store(settings.db_path)
    store.init()

    app = FastAPI(title="Rahasya Engine")
    channels: dict[str, queue.Queue] = {}
    lock = threading.Lock()
    # Health probes the gateway, which is slow to admit it has no provider.
    # Cache the verdict so opening the panel is instant after the first look.
    health_cache: dict[str, object] = {"at": 0.0, "value": None}
    HEALTH_TTL = 45.0

    def client_for(use_fake: bool):
        if use_fake:
            from engine.fake_client import FakeOmniRoute
            return FakeOmniRoute()
        return OmniRouteClient(
            base=settings.omniroute_base, key=settings.omniroute_key,
            chat_model=settings.model_strong or "auto/best-chat",
            embed_model=settings.model_embed or "auto/best-embedding",
            image_model=settings.model_image or "auto/best-image")

    def channel(plan_id: str) -> queue.Queue:
        with lock:
            if plan_id not in channels:
                channels[plan_id] = queue.Queue()
            return channels[plan_id]

    def emitter(plan_id: str):
        outbox = channel(plan_id)

        def emit(event: PipelineEvent) -> None:
            outbox.put(event.to_dict())
        return emit

    # -- UI ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = UI_DIR / "index.html"
        if not page.exists():
            return "<h1>Rahasya Engine</h1><p>UI file missing.</p>"
        return page.read_text(encoding="utf-8")

    if (UI_DIR).exists():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")

    # -- health -----------------------------------------------------------
    @app.get("/api/health")
    def health(refresh: bool = False) -> dict:
        ffmpeg_ok = Path(settings.ffmpeg).exists()
        now = time.monotonic()
        cached = health_cache["value"]
        fresh = now - float(health_cache["at"]) < HEALTH_TTL
        if cached is not None and fresh and not refresh:
            gateway = cached
        else:
            try:
                # Probe with the cheap model, not the strong one: a
                # reasoning model can take 15s+ and would report a healthy
                # gateway as down.
                probe = OmniRouteClient(
                    base=settings.omniroute_base, key=settings.omniroute_key,
                    chat_model=settings.model_cheap or "auto/best-chat")
                with probe:
                    gateway = probe.health(timeout=20.0)
            except Exception as exc:
                gateway = {"state": "down", "models": 0,
                           "detail": str(exc)[:200]}
            health_cache["value"] = gateway
            health_cache["at"] = now
        return {
            "omniroute": gateway["state"],
            "omniroute_detail": gateway["detail"],
            "models": gateway["models"],
            "base": settings.omniroute_base,
            "ffmpeg": ffmpeg_ok,
            "ffmpeg_path": settings.ffmpeg,
            # Report the engine actually in use. This said "hi-IN-Madhur"
            # while Piper was doing the work, because it read the edge-tts
            # fallback setting rather than the active one.
            "voice_engine": settings.voice_engine,
            "voice": (settings.piper_voice
                      if settings.voice_engine == "piper"
                      else settings.voice),
            "today_usd": store.today_usd(),
            "daily_ceiling": settings.daily_usd_ceiling,
            "stages": list(Stage.ORDER),
        }

    # -- plans ------------------------------------------------------------
    @app.get("/api/plans")
    def plans(limit: int = 50) -> list[dict]:
        return store.list_plans(limit)

    @app.get("/api/plan/{plan_id}")
    def get_plan(plan_id: str) -> dict:
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        return json.loads(plan.model_dump_json())

    # One mystery per video. A whole list pasted in at once produced a
    # 290-character filename that ffmpeg could not open, and a script that
    # tried to cover five unrelated stories at the same time.
    TOPIC_MAX = 160

    def check_topic(topic: str) -> str:
        topic = topic.strip()
        if not topic:
            raise HTTPException(400, "topic is empty")
        if len(topic) > TOPIC_MAX:
            raise HTTPException(
                400, f"that is {len(topic)} characters, which looks like more "
                     f"than one topic. One mystery per video — paste a single "
                     f"line under {TOPIC_MAX} characters.")
        return topic

    @app.post("/api/plan")
    def create_plan(request: PlanRequest) -> dict:
        check_topic(request.topic)
        client = client_for(request.use_fake)
        try:
            plan = plan_stage(request.topic, client, store, settings,
                              emit=lambda e: None)
        except GateError as exc:
            raise HTTPException(409, f"{exc.gate}: {exc.detail}") from exc
        except BudgetError as exc:
            raise HTTPException(402, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        finally:
            client.close()
        return json.loads(plan.model_dump_json())

    @app.post("/api/plan/async")
    def create_plan_async(request: PlanRequest) -> dict:
        """Same as /api/plan but streams progress over /api/events/{id}."""
        check_topic(request.topic)
        # A provisional id so the browser can subscribe before work starts.
        import uuid
        job_id = str(uuid.uuid4())
        emit = emitter(job_id)

        def work() -> None:
            # Bound before the try: `finally: client.close()` raised
            # UnboundLocalError when client_for itself failed, so no
            # "complete" event was emitted and the stream hung.
            client = None
            try:
                client = client_for(request.use_fake)
                plan = plan_stage(request.topic, client, store, settings,
                                  emit=emit)
                emit(PipelineEvent("complete", "done", plan.plan_id,
                                   {"plan_id": plan.plan_id}))
            except (GateError, BudgetError) as exc:
                emit(PipelineEvent("complete", "failed", str(exc)))
            except Exception as exc:
                emit(PipelineEvent("complete", "failed",
                                   f"{type(exc).__name__}: {exc}",
                                   {"trace": traceback.format_exc()[-800:]}))
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True).start()
        return {"job_id": job_id}

    @app.get("/api/authoring")
    def authoring() -> dict:
        """Every number the blank authoring form needs, from the server.

        The panel must not carry a copy of the beat count, the word budget
        or the speech rate — stale copies of exactly these numbers caused
        two separate bugs, which is why ``engine.config`` holds one
        definition each. So the panel holds none: it fetches this and
        renders the form, the live budget meter and the duration windows
        from what comes back.
        """
        budget = word_budget(settings)
        count = beat_count(settings)
        per_beat = words_per_beat(budget, count, settings)
        beat_words_min, beat_words_max = beat_word_range(per_beat, settings)
        gate_min, gate_max = pre_render_range(settings.duration_min,
                                              settings.duration_max)
        from engine.agents import LATIN_LETTERS_RE, WORD_TOLERANCE

        return {
            "beat_count": count,
            "word_budget": budget,
            "word_tolerance": WORD_TOLERANCE,
            "word_min": round(budget * (1 - WORD_TOLERANCE)),
            "word_max": round(budget * (1 + WORD_TOLERANCE)),
            "words_per_beat": per_beat,
            "beat_words_min": beat_words_min,
            "beat_words_max": beat_words_max,
            "speech_rate": speech_rate(settings),
            "predicted_seconds": spoken_seconds(budget, settings),
            "target_seconds": settings.target_seconds,
            "duration_min": settings.duration_min,
            "duration_max": settings.duration_max,
            "gate_min": gate_min,
            "gate_max": gate_max,
            "default_roles": default_roles(count, settings),
            "roles": list(get_args(Role)),
            "motions": list(get_args(Motion)),
            "transitions": list(get_args(Transition)),
            # The Devanagari rule's own regex, so the panel's live warning
            # cannot disagree with the server's refusal.
            "latin_pattern": LATIN_LETTERS_RE.pattern,
            "topic_max": TOPIC_MAX,
        }

    @app.post("/api/plan/manual")
    def create_manual_plan(request: ManualPlanRequest) -> dict:
        """The second entry path: a script the user wrote.

        Synchronous, unlike ``/api/plan/async``, because there is nothing to
        wait for — no model is called, so the whole thing is validation and
        one SQLite write. The stage events come back in the response so the
        panel can paint the same evidence log without an SSE channel.
        """
        check_topic(request.topic)
        events: list[dict] = []
        try:
            plan = manual_plan_stage(
                request.topic, [b.model_dump() for b in request.beats],
                store, settings,
                entities=request.entities,
                claims=request.sources,
                metadata=request.metadata,
                acknowledge_unsourced=request.acknowledge_unsourced,
                emit=lambda event: events.append(event.to_dict()))
        except ManualScriptError as exc:
            # 400, not 409: this is the form being wrong, not a gate
            # refusing an otherwise valid plan.
            raise HTTPException(400, "\n".join(exc.problems)) from exc
        except GateError as exc:
            raise HTTPException(409, f"{exc.gate}: {exc.detail}") from exc
        return {
            "plan": json.loads(plan.model_dump_json()),
            "budget": budget_report(plan.script, settings),
            "events": events,
        }

    @app.post("/api/plan/{plan_id}/approve")
    def approve(plan_id: str, request: ApproveRequest) -> dict:
        """The human gate. Persists edits and marks the plan renderable."""
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")

        if not any(h.variant_id == request.chosen_hook for h in plan.hooks):
            raise HTTPException(400, f"unknown hook {request.chosen_hook}")
        plan.script.chosen_hook = request.chosen_hook

        # Order matters. The chosen hook seeds beat 1 FIRST, then the
        # human's edits land on top. The other way round silently overwrote
        # the one beat a human most wants to tune: the 0-3s hook.
        chosen = plan.chosen()
        if chosen and plan.script.beats:
            plan.script.beats[0] = Beat.model_validate({
                **plan.script.beats[0].model_dump(),
                "voice_text": chosen.voice_text,
                "caption_text": chosen.caption_text,
                "measured_seconds": None, "words": []})

        if request.beats:
            edits = {b.beat_id: b for b in request.beats}
            rebuilt: list[Beat] = []
            for beat in plan.script.beats:
                edit = edits.get(beat.beat_id)
                if edit is None:
                    rebuilt.append(beat)
                    continue
                # model_validate, not model_copy: pydantic v2 skips validation
                # on model_copy, which is how an invalid motion got persisted.
                rebuilt.append(Beat.model_validate({
                    **beat.model_dump(),
                    "voice_text": edit.voice_text,
                    "caption_text": edit.caption_text,
                    "on_screen_text": edit.on_screen_text or None,
                    "visual_prompt": edit.visual_prompt,
                    "motion": edit.motion,
                    "transition": edit.transition,
                    # Text changed, so the old measurements are stale.
                    "measured_seconds": None,
                    "words": [],
                }))
            plan.script.beats = rebuilt

        store.save_plan(plan, status="approved")
        store.record_entities(plan.plan_id, plan.topic.entities)
        return {"plan_id": plan.plan_id, "status": "approved",
                "beats": len(plan.script.beats)}

    @app.post("/api/plan/{plan_id}/produce")
    def produce(plan_id: str, request: ProduceRequest) -> dict:
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")

        # Both human gates are constraints, so they are enforced here
        # rather than only in the browser. Without this, any script, retry
        # or curl could render and package a plan nobody had looked at.
        status = store.plan_status(plan_id)
        if status == VOICE_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is awaiting voice review, so it cannot be "
                     f"produced from here. Listen to the beats (GET "
                     f"/api/plan/{plan_id}/voice), replace any you want to "
                     f"say yourself, then POST "
                     f"/api/plan/{plan_id}/voice/approve to release it. "
                     f"Re-running produce would re-synthesise over the "
                     f"narration you just uploaded.")
        if status == CLIP_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is awaiting clip review, so it cannot be "
                     f"rendered from here. Look at the clips (GET "
                     f"/api/plan/{plan_id}/clips), replace any that do not "
                     f"match, then POST /api/plan/{plan_id}/clips/approve "
                     f"to release it. Re-running produce would discard the "
                     f"footage you are reviewing and fetch it again.")
        if status not in {"approved", "produced", "qc_failed"}:
            raise HTTPException(
                409, f"plan is '{status}', not approved. POST "
                     f"/api/plan/{plan_id}/approve with a chosen hook first.")

        emit = emitter(plan_id)

        def work() -> None:
            client = None
            try:
                client = client_for(request.use_fake)
                if request.review_voice or request.review_clips:
                    # The gated path, walked one part at a time so a gate
                    # can be put between them. ``produce_stage`` is the
                    # same three parts in a row; it is not called here
                    # because the run has to be able to stop.
                    voice_stage(plan, store, settings, emit=emit)
                    if request.review_voice:
                        # Saved with the audio on it: the review board
                        # reads the plan back out of the store, and the
                        # status is what stops anything producing it in
                        # the meantime.
                        store.save_plan(plan, status=VOICE_REVIEW_STATUS)
                        emit(PipelineEvent(
                            "complete", "voice_review",
                            f"{len(plan.script.beats)} beats ready to hear",
                            {"plan_id": plan_id, "awaiting_review": True,
                             "gate": "voice",
                             "engines": voice_engines(plan),
                             "narration_seconds": round(plan.duration(), 2),
                             "voice": f"/api/plan/{plan_id}/voice"}))
                        return
                    counts = clips_stage(plan, client, store, settings,
                                         emit=emit)
                    store.save_plan(plan, status=CLIP_REVIEW_STATUS)
                    total = sum(counts.values()) if counts else 0
                    emit(PipelineEvent(
                        "complete", "review",
                        f"{total} clips ready to review",
                        {"plan_id": plan_id, "awaiting_review": True,
                         "gate": "clips", "providers": counts,
                         "clips": f"/api/plan/{plan_id}/clips"}))
                    return
                result = produce_stage(
                    plan, client, store, settings, emit=emit,
                    captions_source=request.captions_source)
                emit(PipelineEvent("complete", "done",
                                   Path(result["video"]).name, result))
            except BudgetError as exc:
                emit(PipelineEvent("complete", "failed", str(exc)))
            except Exception as exc:
                emit(PipelineEvent("complete", "failed",
                                   f"{type(exc).__name__}: {exc}",
                                   {"trace": traceback.format_exc()[-800:]}))
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True).start()
        return {"plan_id": plan_id, "review_voice": request.review_voice,
                "review_clips": request.review_clips,
                "streaming": f"/api/events/{plan_id}"}

    # -- gate two: the voice review ---------------------------------------
    @app.post("/api/plan/{plan_id}/voice/approve")
    def approve_voice(plan_id: str,
                      request: VoiceReleaseRequest | None = None) -> dict:
        """Release a heard plan and run LENGTH -> CLIPS, then on or stop.

        The only way a plan in ``awaiting_voice_review`` moves. ``/produce``
        refuses it, so a plan parked here stays parked until a human says
        otherwise.

        Unlike the clip gate this one does build a client: CLIPS is
        downstream of it, and turning a beat into an English search phrase
        is a model call. What it must not do is synthesise again — and it
        cannot, because ``voice_stage`` is not called from here. Narration
        the user uploaded survives this route by never being regenerated.

        Declared above the per-beat upload route on purpose: both are POST
        under ``/voice/``, and FastAPI matches in declaration order, so
        ``approve`` has to be seen before ``{beat_id}`` can swallow it.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != VOICE_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting voice review. Only "
                     f"a plan produced with review_voice=true stops here.")

        request = request or VoiceReleaseRequest()
        store.save_plan(plan, status="voice_approved")
        emit = emitter(plan_id)
        captions_source = request.captions_source
        review_clips = request.review_clips
        use_fake = request.use_fake

        def work() -> None:
            client = None
            try:
                client = client_for(use_fake)
                counts = clips_stage(plan, client, store, settings, emit=emit)
                if review_clips:
                    store.save_plan(plan, status=CLIP_REVIEW_STATUS)
                    total = sum(counts.values()) if counts else 0
                    emit(PipelineEvent(
                        "complete", "review",
                        f"{total} clips ready to review",
                        {"plan_id": plan_id, "awaiting_review": True,
                         "gate": "clips", "providers": counts,
                         "clips": f"/api/plan/{plan_id}/clips"}))
                    return
                result = render_stage(plan, store, settings, emit=emit,
                                      captions_source=captions_source,
                                      providers=counts)
                emit(PipelineEvent("complete", "done",
                                   Path(result["video"]).name, result))
            except Exception as exc:
                # Back to the gate rather than stranded in a status nothing
                # accepts. The audio is still on disk and still correct, so
                # the user can fix one more beat and release again.
                store.save_plan(plan, status=VOICE_REVIEW_STATUS)
                emit(PipelineEvent("complete", "failed",
                                   f"{type(exc).__name__}: {exc}",
                                   {"trace": traceback.format_exc()[-800:]}))
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True).start()
        return {"plan_id": plan_id, "status": "voice_approved",
                "review_clips": review_clips,
                "streaming": f"/api/events/{plan_id}"}

    # -- the listening board ----------------------------------------------
    @app.get("/api/plan/{plan_id}/voice")
    def voice_review(plan_id: str) -> dict:
        """Every beat's narration, one row each, with the text that made it.

        Carries ``voice_text`` deliberately. The expected use of this gate
        is to paste a beat into some other voice tool, generate it there
        and bring the file back, so the Devanagari the beat is supposed to
        say has to be on the board next to the upload control — not one
        screen back up.

        The duration numbers are here for the same reason the LENGTH gate
        exists: replacing narration by hand is the easiest way to walk a
        plan out of its publishing window, and finding that out from QC
        twelve minutes later is exactly the failure this gate is against.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        roots = [Path(settings.work_dir), Path(settings.out_dir)]
        # Narrower than ``roots``: the upload route only ever writes a raw
        # copy under ``work_dir`` (never ``out_dir``), and ``toggle_cleanup``
        # only ever accepts a raw path there too. Checked against the same
        # single root here so the board never advertises a
        # ``cleanup_toggle`` URL for a raw file the toggle route itself
        # would 404 on (final review, Minor 5).
        raw_roots = [Path(settings.work_dir)]

        rows: list[dict] = []
        for beat in plan.script.beats:
            path = Path(beat.audio_path) if beat.audio_path else None
            raw_path = Path(beat.raw_audio_path) if beat.raw_audio_path \
                else None
            replaced = beat.voice_engine == UPLOAD_ENGINE
            # Gated on ``replaced``, not only on a raw file being on disk:
            # a beat that was uploaded and then re-spoken still has a raw
            # file (Global Constraint 3 never destroys it) and a stale
            # ``cleanup`` report describing that old upload, but the
            # upload is no longer what the beat says -- ``toggle_cleanup``
            # refuses it for exactly this reason, so the board must not
            # advertise a revert control it will be refused for.
            raw_exists = bool(raw_path and raw_path.is_file()
                              and _under_roots(raw_path, raw_roots)
                              and replaced)
            rows.append({
                "beat_id": beat.beat_id,
                "role": beat.role,
                "seconds": round(beat.seconds(), 2),
                "engine": beat.voice_engine,
                "replaced": replaced,
                "word_timing_source": beat.word_timing_source,
                # What this beat is supposed to say, and what gets burned
                # over it. Both, because they differ: the voice is
                # Devanagari and the caption is Roman Hinglish.
                "voice_text": beat.voice_text,
                "caption_text": beat.caption_text,
                "exists": bool(path and path.is_file()
                               and _under_roots(path, roots)),
                "audio": f"/api/audio/{plan_id}/{beat.beat_id}",
                "replace": f"/api/plan/{plan_id}/voice/{beat.beat_id}",
                # A beat nothing was ever uploaded for -- a synthesised
                # beat, one uploaded before this existed, or one that has
                # since been re-spoken -- reports no raw rather than
                # erroring or advertising a stale one: ``None`` all the
                # way down.
                "raw_audio": (f"/api/audio/{plan_id}/{beat.beat_id}?raw=1"
                             if raw_exists else None),
                # Whether *what's currently written* went through the
                # cleanup chain -- distinct from whether cleanup was
                # merely asked for. See ``_cleaned``.
                "cleaned": _cleaned(beat.cleanup) if raw_exists else None,
                "cleanup": (beat.cleanup.model_dump()
                           if raw_exists and beat.cleanup else None),
                "cleanup_toggle": (f"/api/plan/{plan_id}/voice/"
                                  f"{beat.beat_id}/cleanup"
                                  if raw_exists else None),
            })

        narration = plan.duration()
        gate_min, gate_max = pre_render_range(settings.duration_min,
                                              settings.duration_max)
        return {
            "plan_id": plan_id,
            "status": status,
            "awaiting_review": status == VOICE_REVIEW_STATUS,
            "total": len(rows),
            "narration_seconds": round(narration, 2),
            "engines": voice_engines(plan),
            # Both windows, because they are different questions: the gate
            # decides whether the run continues at all, QC decides whether
            # the finished video is publishable. A plan can pass the first
            # and fail the second, and the board should not hide that.
            "gate_min": round(gate_min, 1), "gate_max": round(gate_max, 1),
            "duration_min": settings.duration_min,
            "duration_max": settings.duration_max,
            "in_gate": gate_min <= narration <= gate_max,
            "in_window": (settings.duration_min <= narration
                          <= settings.duration_max),
            "min_seconds": MIN_UPLOAD_SECONDS,
            "max_bytes": int(settings.upload_max_mb * 1024 * 1024),
            "beats": rows,
        }

    @app.post("/api/plan/{plan_id}/voice/{beat_id}")
    async def replace_voice(plan_id: str, beat_id: str,
                            request: Request) -> dict:
        """Replace one beat's narration with a file the user supplied.

        The body is the file itself, not multipart, for the same reason the
        clip route streams: a multipart parser buffers the whole body
        before the handler sees a byte, so the size cap would be enforced
        after the disk write it exists to prevent.

        What lands on disk is never the uploaded bytes.
        ``ingest_narration`` re-encodes into Piper's exact format and
        loudness band and writes *that*, which is why this route does not
        have to decide an extension: every beat is an mp3 either way.

        The synthesised original is left where it is, under the beat's own
        name, and the upload is written alongside it. Nothing reads it any
        more, but a destroyed original cannot be compared against and
        cannot be put back, and this gate exists for people who are still
        making up their minds.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != VOICE_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting voice review. "
                     f"Narration is replaceable at that gate, which is the "
                     f"one moment nothing has been derived from it yet — "
                     f"after CLIPS the footage has already been cut to the "
                     f"length the old audio measured.")

        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                    None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")

        # Refused before a byte is read: an attacker-shaped filename is a
        # fact about the request, not about the file.
        _safe_client_filename(request.headers.get("x-upload-filename"))

        limit = int(settings.upload_max_mb * 1024 * 1024)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            raise HTTPException(
                413, f"that file is {int(length) / 1048576:.1f} MB and the "
                     f"cap is {settings.upload_max_mb:g} MB "
                     f"(RAHASYA_UPLOAD_MAX_MB).")

        # Server-built, from values already validated against the stored
        # plan. Beat ids are generated, but one arriving from a model's
        # output must still not be able to shape a path.
        safe_beat = re.sub(r"[^A-Za-z0-9_-]", "_", beat_id)[:40] or "beat"
        safe_plan = re.sub(r"[^A-Za-z0-9_-]", "_", plan_id)[:64] or "plan"
        audio_dir = Path(settings.work_dir) / safe_plan / "audio"
        # Checked BEFORE the mkdir: a containment check that runs once the
        # directory exists has already let the filesystem be touched
        # outside work_dir, which is the whole thing it is for.
        if not _under_roots(audio_dir, [Path(settings.work_dir)]):
            raise HTTPException(500, "audio directory escaped work_dir")
        audio_dir.mkdir(parents=True, exist_ok=True)
        partial = audio_dir / f"{safe_beat}-upload.part"

        written = 0
        try:
            with partial.open("wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            413, f"the upload passed the "
                                 f"{settings.upload_max_mb:g} MB cap "
                                 f"(RAHASYA_UPLOAD_MAX_MB) and was stopped "
                                 f"there; nothing was kept.")
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(400, "the upload was empty")

            with partial.open("rb") as handle:
                head = handle.read(32)
            sniffed = _sniff_audio(head)
            if sniffed is None:
                raise HTTPException(
                    415, "those bytes are not any media container this "
                         "gate recognises. The extension and the content "
                         "type are not trusted here; the file's own "
                         "signature is.")

            destination = audio_dir / f"{safe_beat}-upload.mp3"
            if not _under_roots(destination, [Path(settings.work_dir)]):
                raise HTTPException(500, "upload path escaped work_dir")
            # The exact bytes handed over, kept beside the cleaned file
            # (Global Constraint 3). Named from the sniffed container, not
            # ``.mp3`` -- unlike the cleaned file this one is never
            # transcoded, so a ``.mp3`` name would lie about what it is.
            raw_suffix = RAW_UPLOAD_SUFFIX.get(sniffed, ".bin")
            raw_target = audio_dir / f"{safe_beat}-upload.raw{raw_suffix}"
            if not _under_roots(raw_target, [Path(settings.work_dir)]):
                raise HTTPException(500, "raw upload path escaped work_dir")
            try:
                seconds, report = ingest_narration(
                    partial, destination, settings, raw_target=raw_target)
            except UploadRejected as exc:
                # A 415 rather than a 400: these are all judgements about
                # the media itself — it does not decode, it is silent, it
                # is too short to be a beat.
                raise HTTPException(415, str(exc)) from exc
        finally:
            Path(partial).unlink(missing_ok=True)

        before = beat.seconds()
        apply_beat_audio(beat, destination, settings,
                         engine=UPLOAD_ENGINE, aligner=build_aligner(settings))
        beat.raw_audio_path = str(raw_target)
        beat.cleanup = _cleanup_info(report)
        store.save_plan(plan, status=VOICE_REVIEW_STATUS)

        narration = plan.duration()
        gate_min, gate_max = pre_render_range(settings.duration_min,
                                              settings.duration_max)
        return {
            "plan_id": plan_id, "beat_id": beat_id,
            "engine": UPLOAD_ENGINE,
            "seconds": round(seconds, 2),
            "was_seconds": round(before, 2),
            "word_timing_source": beat.word_timing_source,
            "bytes": written,
            # The totals, recomputed. Replacing one beat moves the whole
            # video's length, and the number the board was showing a
            # moment ago is now wrong.
            "narration_seconds": round(narration, 2),
            "in_gate": gate_min <= narration <= gate_max,
            "in_window": (settings.duration_min <= narration
                          <= settings.duration_max),
            "audio": f"/api/audio/{plan_id}/{beat_id}",
            "raw_audio": f"/api/audio/{plan_id}/{beat_id}?raw=1",
            "cleaned": _cleaned(beat.cleanup),
            **_cleanup_response(report),
        }

    @app.patch("/api/plan/{plan_id}/voice/{beat_id}")
    def respeak_beat(plan_id: str, beat_id: str,
                     request: BeatTextEdit) -> dict:
        """Correct one beat's words and say it again — only that beat.

        The case this is for is small and constant: the voice mispronounces
        a word, and the fix is a character or two in that beat's
        Devanagari. Before this the only way to change it was to go back to
        the script screen and produce again, which re-speaks all ten beats
        and discards everything else settled at this gate.

        ``voice_text`` and ``caption_text`` move independently, and that is
        the point rather than a convenience. The voice line is what Piper
        reads; the caption line is what gets burned on screen. A
        mispronunciation is fixed by respelling the word *phonetically in
        the voice line only* — the viewer keeps seeing the real spelling
        and the listener hears the right sound.

        A caption-only edit does not re-speak anything. The audio did not
        change, so there is nothing to say again; only the word timings
        have to be laid out against it afresh.

        PATCH rather than POST because POST on this same path is the
        upload. One resource, two verbs, no third URL to keep in step.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != VOICE_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting voice review. A "
                     f"beat's words are correctable at that gate, which is "
                     f"the one moment nothing has been derived from them "
                     f"yet — after CLIPS the footage has already been cut "
                     f"to the length the old wording measured.")

        index = next((i for i, b in enumerate(plan.script.beats)
                      if b.beat_id == beat_id), None)
        if index is None:
            raise HTTPException(404, f"no such beat: {beat_id}")
        beat = plan.script.beats[index]

        voice_text = request.voice_text
        caption_text = request.caption_text
        if voice_text is None and caption_text is None:
            raise HTTPException(
                400, "send voice_text, caption_text, or both. An empty "
                     "edit is refused rather than treated as a request to "
                     "say the same thing again.")

        if voice_text is not None:
            voice_text = voice_text.strip()
            if not voice_text:
                raise HTTPException(
                    400, "a beat has to say something. To drop a beat, "
                         "edit the script and produce again — a beat with "
                         "no words still owns a span of the timeline.")
            # The same rule the script gate enforces, through the same
            # function rather than a second copy of the regex: Piper
            # mispronounces Latin script whoever typed it, and a rule that
            # exists twice is a rule that will disagree with itself.
            latin = latin_words(voice_text)
            if latin:
                raise HTTPException(
                    400, f"the voice line has to be Devanagari, and this "
                         f"one still has Latin script in it: "
                         f"{', '.join(latin[:8])}. That is not a style "
                         f"rule — the voice reads Latin letters as English "
                         f"and mispronounces them. Write the sound in "
                         f"Devanagari; the Roman spelling belongs in the "
                         f"caption line, which is burned on screen and "
                         f"never spoken.")

        if caption_text is not None:
            caption_text = caption_text.strip()
            if not caption_text:
                raise HTTPException(
                    400, "the caption line is what gets burned on screen; "
                         "it cannot be empty.")

        was_seconds = beat.seconds()
        # model_validate, not model_copy: pydantic v2 skips validation on
        # model_copy, which is how an invalid motion once got persisted and
        # made every later read of that plan a 500.
        beat = Beat.model_validate({
            **beat.model_dump(),
            "voice_text": voice_text if voice_text is not None
            else beat.voice_text,
            "caption_text": caption_text if caption_text is not None
            else beat.caption_text,
        })
        plan.script.beats[index] = beat

        respeak = voice_text is not None
        if respeak:
            # The path synthesis already uses, through the one definition
            # of it, so a corrected beat overwrites the take it replaces
            # instead of leaving a second file nothing reads. Built from
            # the stored ids, then contained: the ids are generated, but
            # one arriving from a model's output must still not shape a
            # path.
            target = beat_audio_path(plan.plan_id, beat.beat_id,
                                     settings.work_dir)
            if not _under_roots(target, [Path(settings.work_dir)]):
                raise HTTPException(500, "audio path escaped work_dir")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Spoken onto a scratch path and moved into place only once it
            # is whole. Piper writes its mp3 straight onto the target with
            # ``-y``, which was harmless while synthesis only ever ran on a
            # beat with nothing there yet — re-speaking runs it on a beat
            # that already has a take, and the loop this feature exists for
            # is "try a spelling, listen, try another". A half-written file
            # over a good one is a beat that can no longer be played,
            # re-spoken from, or rendered.
            scratch = target.with_name(target.stem + ".respeak.mp3")
            try:
                engine, spans, _note = speak_beat(beat.voice_text, scratch,
                                                  settings)
                if not scratch.is_file() or scratch.stat().st_size == 0:
                    raise RuntimeError("the engine wrote nothing")
                os.replace(scratch, target)
            except HTTPException:
                raise
            except Exception as exc:
                # Every failure of this block means one thing to whoever
                # pressed the button — it could not be said — so they all
                # get the same answer rather than a 500 that reads like a
                # bug in the panel. The beat is untouched: the edit is
                # only in memory and has not been saved.
                raise HTTPException(
                    503, f"the voice could not say that: "
                         f"{type(exc).__name__}: {exc}. The beat is "
                         f"unchanged — its previous take is still there.")
            finally:
                scratch.unlink(missing_ok=True)
            source = target
        else:
            # Nothing was said again, so the beat keeps the audio it has —
            # including narration the user uploaded. Only the caption
            # timings are laid out against it afresh.
            if not beat.audio_path:
                raise HTTPException(
                    409, f"beat {beat_id!r} has no audio to re-time. Send "
                         f"voice_text to have it spoken.")
            source = Path(beat.audio_path)
            # Carried through unchanged, ``None`` included: nothing spoke,
            # so nothing about which engine spoke has changed. Coercing a
            # missing engine to "" here would turn "not spoken" into a
            # engine name of its own on the board and in the QC tally.
            engine, spans = beat.voice_engine, None

        apply_beat_audio(beat, source, settings, engine=engine,
                         aligner=build_aligner(settings))
        if spans is not None:
            # ``apply_beat_audio`` clears this because a swapped file is
            # not what the old engine described. Here it is: a fresh count
            # from the engine that just spoke.
            beat.spoken_words = spans
        store.save_plan(plan, status=VOICE_REVIEW_STATUS)

        narration = plan.duration()
        gate_min, gate_max = pre_render_range(settings.duration_min,
                                              settings.duration_max)
        return {
            "plan_id": plan_id, "beat_id": beat_id,
            "respoken": respeak,
            "engine": beat.voice_engine,
            "voice_text": beat.voice_text,
            "caption_text": beat.caption_text,
            "seconds": round(beat.seconds(), 2),
            "was_seconds": round(was_seconds, 2),
            "word_timing_source": beat.word_timing_source,
            "narration_seconds": round(narration, 2),
            "in_gate": gate_min <= narration <= gate_max,
            "in_window": (settings.duration_min <= narration
                          <= settings.duration_max),
            "audio": f"/api/audio/{plan_id}/{beat_id}",
        }

    @app.post("/api/plan/{plan_id}/voice/{beat_id}/cleanup")
    def toggle_cleanup(plan_id: str, beat_id: str,
                       request: CleanupToggleRequest) -> dict:
        """Re-ingest a beat's stored raw upload with cleanup on or off.

        Never re-ingests from ``audio_path`` -- only from ``raw_audio_path``.
        Cleaning an already-cleaned file would compound the processing
        (a second highpass, a second denoise, a second pass of
        ``silenceremove`` eating into what the first pass already trimmed),
        which is exactly what keeping the raw beside the cleaned file is
        for.

        Idempotent in the sense that matters here: calling this twice with
        the same ``enabled`` re-runs the same deterministic ffmpeg pipeline
        over the same raw bytes and lands in the same state both times,
        rather than erroring on the second call.

        Same guards as the upload route: the voice gate, a beat that
        exists, and every path checked against ``work_dir`` before it is
        written to.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != VOICE_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting voice review. "
                     f"Cleanup is toggled at that gate, the same place "
                     f"narration is replaceable.")

        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                    None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")

        if not beat.raw_audio_path:
            raise HTTPException(
                404, f"beat {beat_id!r} has no raw upload stored to "
                     f"re-ingest. Only a beat whose narration was uploaded "
                     f"through this gate keeps one.")
        if beat.voice_engine != UPLOAD_ENGINE:
            # The raw upload is still there (Global Constraint 3 never
            # destroys it), but it is no longer what this beat says. A
            # respeak through the PATCH route carries raw_audio_path and
            # cleanup forward unchanged -- correctly, since the file itself
            # is still real -- but re-ingesting it here would call
            # apply_beat_audio with engine=UPLOAD_ENGINE and silently
            # overwrite the correction that respeak just made. Refused
            # rather than risked: this is the same shape of bug
            # image_provider already shipped once, a field read long after
            # nothing wrote it for the beat's current state.
            raise HTTPException(
                409, f"beat {beat_id!r} is currently saying "
                     f"{beat.voice_engine!r}'s take, not its uploaded one. "
                     f"Toggling cleanup would silently discard whatever "
                     f"replaced the upload. The raw file is still on disk "
                     f"-- upload it again (POST to the beat's own voice "
                     f"URL) to make it active before toggling cleanup on "
                     f"it.")
        raw_path = Path(beat.raw_audio_path)
        if not raw_path.is_file() or not _under_roots(
                raw_path, [Path(settings.work_dir)]):
            raise HTTPException(
                404, f"beat {beat_id!r}'s raw upload is no longer on disk")

        # Server-built, exactly as the upload route builds it -- this is
        # the same destination that route writes to, so toggling cleanup
        # overwrites the beat's upload take rather than adding a second
        # file nothing reads.
        safe_beat = re.sub(r"[^A-Za-z0-9_-]", "_", beat_id)[:40] or "beat"
        safe_plan = re.sub(r"[^A-Za-z0-9_-]", "_", plan_id)[:64] or "plan"
        audio_dir = Path(settings.work_dir) / safe_plan / "audio"
        if not _under_roots(audio_dir, [Path(settings.work_dir)]):
            raise HTTPException(500, "audio directory escaped work_dir")
        audio_dir.mkdir(parents=True, exist_ok=True)
        destination = audio_dir / f"{safe_beat}-upload.mp3"
        if not _under_roots(destination, [Path(settings.work_dir)]):
            raise HTTPException(500, "upload path escaped work_dir")

        try:
            seconds, report = ingest_narration(
                raw_path, destination, settings, clean=request.enabled)
        except UploadRejected as exc:
            raise HTTPException(415, str(exc)) from exc

        before = beat.seconds()
        apply_beat_audio(beat, destination, settings,
                         engine=UPLOAD_ENGINE, aligner=build_aligner(settings))
        beat.cleanup = _cleanup_info(report)
        store.save_plan(plan, status=VOICE_REVIEW_STATUS)

        narration = plan.duration()
        gate_min, gate_max = pre_render_range(settings.duration_min,
                                              settings.duration_max)
        return {
            "plan_id": plan_id, "beat_id": beat_id,
            "enabled": request.enabled,
            "engine": UPLOAD_ENGINE,
            "seconds": round(seconds, 2),
            "was_seconds": round(before, 2),
            "word_timing_source": beat.word_timing_source,
            "narration_seconds": round(narration, 2),
            "in_gate": gate_min <= narration <= gate_max,
            "in_window": (settings.duration_min <= narration
                          <= settings.duration_max),
            "audio": f"/api/audio/{plan_id}/{beat_id}",
            "raw_audio": f"/api/audio/{plan_id}/{beat_id}?raw=1",
            "cleaned": _cleaned(beat.cleanup),
            **_cleanup_response(report),
        }

    @app.get("/api/audio/{plan_id}/{beat_id}")
    def beat_audio(plan_id: str, beat_id: str,
                  raw: bool = False) -> FileResponse:
        """One beat's narration, for the player on the review board.

        Every path here is server-controlled — synthesis writes
        deterministic names under ``work_dir`` and the upload route builds
        its own — but it is still keyed by URL input and read off disk, so
        it gets the same containment check as ``/media`` and ``/api/frame``:
        nothing outside ``work_dir``/``out_dir`` is ever served.

        ``?raw=1`` serves the beat's stored raw upload instead of whatever
        is currently active. There is deliberately no fall-back to the
        cleaned file when no raw is stored: a player quietly serving a
        different take than the one asked for is exactly the failure this
        gate exists to catch, so that is a 404 instead.

        Gated on ``voice_engine`` the same way the board and
        ``toggle_cleanup`` already are: a beat that was uploaded and then
        re-spoken keeps its raw file on disk (Global Constraint 3), but it
        is archived history, not the active take, and this was the one
        reader of ``raw_audio_path`` that did not apply that rule (final
        review, Minor 6).
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                    None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")

        if raw:
            if not beat.raw_audio_path or beat.voice_engine != UPLOAD_ENGINE:
                raise HTTPException(
                    404, f"beat {beat_id!r} has no raw upload stored")
            raw_path = Path(beat.raw_audio_path)
            # Narrower than the cleaned-audio check below: a raw upload is
            # only ever written under ``work_dir`` (see the upload route),
            # and the board and ``toggle_cleanup`` check it against that
            # one root -- agreeing with them here rather than accepting a
            # wider set this route would never actually see a file under.
            if not raw_path.is_file() or not _under_roots(
                    raw_path, [Path(settings.work_dir)]):
                raise HTTPException(
                    404, f"beat {beat_id!r}'s raw upload is not on disk "
                         f"any more")
            # No forced media_type, unlike the branch below: the raw file
            # is whatever container the browser sent (wav, m4a, mov, ...),
            # never transcoded, so its own extension says what it is.
            return FileResponse(raw_path)

        if not beat.audio_path:
            raise HTTPException(
                404, f"beat {beat_id!r} has no audio yet — voice has not "
                     f"run for this plan")
        path = Path(beat.audio_path)
        if not path.is_file() or not _under_roots(
                path, [Path(settings.work_dir), Path(settings.out_dir)]):
            raise HTTPException(
                404, f"beat {beat_id!r} has no usable audio on disk")
        return FileResponse(path, media_type="audio/mpeg")

    # -- gate three: the clip review --------------------------------------
    @app.post("/api/plan/{plan_id}/clips/approve")
    def approve_clips(plan_id: str,
                      request: ReleaseRequest | None = None) -> dict:
        """Release a reviewed plan and run CAPTIONS -> RENDER -> QC.

        The only way a plan in ``awaiting_clip_review`` reaches the render.
        ``/produce`` refuses it (above), so a plan parked at this gate stays
        parked until a human says otherwise.

        No ``client`` is built here, and ``render_stage`` takes none: after
        the review nothing can call a model or re-fetch the footage the
        human just finished correcting.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != CLIP_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting clip review. Only a "
                     f"plan produced with review_clips=true stops here.")

        store.save_plan(plan, status="clips_approved")
        emit = emitter(plan_id)
        captions_source = request.captions_source if request else None

        def work() -> None:
            try:
                result = render_stage(plan, store, settings, emit=emit,
                                      captions_source=captions_source)
                emit(PipelineEvent("complete", "done",
                                   Path(result["video"]).name, result))
            except Exception as exc:
                # Back to the gate rather than stranded in a status nothing
                # accepts: the clips are still on disk and still correct, so
                # the user can fix one more and release again.
                store.save_plan(plan, status=CLIP_REVIEW_STATUS)
                emit(PipelineEvent("complete", "failed",
                                   f"{type(exc).__name__}: {exc}",
                                   {"trace": traceback.format_exc()[-800:]}))

        threading.Thread(target=work, daemon=True).start()
        return {"plan_id": plan_id, "status": "clips_approved",
                "streaming": f"/api/events/{plan_id}"}

    # -- the review board -------------------------------------------------
    @app.get("/api/plan/{plan_id}/clips")
    def clip_review(plan_id: str) -> dict:
        """Every clip that will appear in the video, one row each.

        Per clip, not per beat: a beat holds several, and a strip of
        per-beat thumbnails is exactly what let a wrong clip through — it
        showed the first slot of each beat and nothing else.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        roots = [Path(settings.work_dir), Path(settings.out_dir)]

        rows: list[dict] = []
        for beat in plan.script.beats:
            for slot, clip in enumerate(beat.clips):
                path = Path(clip.path) if clip.path else None
                rows.append({
                    "beat_id": beat.beat_id,
                    "slot": slot,
                    "duration": round(clip.duration, 3),
                    "provider": clip.provider,
                    # Why this clip was chosen. Without it an irrelevant
                    # clip is a mystery; with it, it is usually obvious.
                    "query": clip.query,
                    "visual_prompt": beat.visual_prompt,
                    "motion": beat.motion,
                    "role": beat.role,
                    "source_url": clip.source_url,
                    "replaced": clip.provider == UPLOAD_PROVIDER,
                    "kind": ("video" if path and path.suffix.lower()
                             in VIDEO_SUFFIXES else "image"),
                    "exists": bool(path and path.is_file()
                                   and _under_roots(path, roots)),
                    "thumb": f"/api/frame/{plan_id}/{beat.beat_id}"
                             f"?slot={slot}",
                    "replace": f"/api/plan/{plan_id}/clip/"
                               f"{beat.beat_id}/{slot}",
                })

        return {
            "plan_id": plan_id,
            "status": status,
            "awaiting_review": status == CLIP_REVIEW_STATUS,
            "total": len(rows),
            "narration_seconds": round(plan.duration(), 2),
            "providers": clip_providers(plan),
            "accept": sorted(UPLOAD_KINDS),
            "max_bytes": int(settings.upload_max_mb * 1024 * 1024),
            "clips": rows,
        }

    @app.post("/api/plan/{plan_id}/clip/{beat_id}/{slot}")
    async def replace_clip(plan_id: str, beat_id: str, slot: int,
                           request: Request) -> dict:
        """Replace one slot's footage with a file the user picked.

        The body is the file itself, not multipart. That is deliberate: a
        multipart parser has to buffer or spool the whole body before the
        handler sees a byte of it, so the size cap would be enforced after
        the disk write it exists to prevent — and it would add a dependency
        (``python-multipart``) for a form with one field in it. Streaming
        the raw body lets the cap be enforced at the cap.

        ``duration`` is not touched. The slot is fixed by the narration
        timeline and the render trims or loops the source to it (see
        ``engine.assembly.render``), so a 12-second holiday video and a
        single JPEG both come out at exactly the 2.1 seconds the voice
        needs. An image lands on the ``zoompan`` branch and gets the Ken
        Burns move; a video does not. Which branch it takes is decided by
        the extension this route stores, which comes from the sniffed
        bytes.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != CLIP_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting clip review. Clips "
                     f"are replaceable at the review gate, which is the one "
                     f"moment the render has not read them yet.")

        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                    None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")
        if not 0 <= slot < len(beat.clips):
            raise HTTPException(
                404, f"beat {beat_id!r} has {len(beat.clips)} clip slots, "
                     f"so there is no slot {slot}")

        # Refused before a byte is read: an attacker-shaped filename is a
        # fact about the request, not about the file.
        _safe_client_filename(request.headers.get("x-upload-filename"))

        declared = _normalise_type(request.headers.get("content-type"))
        if declared not in UPLOAD_KINDS:
            raise HTTPException(
                415, f"{declared or 'no content type'} is not something this "
                     f"slot accepts. Send one of: "
                     f"{', '.join(sorted(UPLOAD_KINDS))}.")

        limit = int(settings.upload_max_mb * 1024 * 1024)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            raise HTTPException(
                413, f"that file is {int(length) / 1048576:.1f} MB and the "
                     f"cap is {settings.upload_max_mb:g} MB "
                     f"(RAHASYA_UPLOAD_MAX_MB).")

        # Server-built, from values already validated against the stored
        # plan. The regex is belt and braces: beat ids are generated, but
        # one arriving from a model's output must still not be able to
        # shape a path.
        safe_beat = re.sub(r"[^A-Za-z0-9_-]", "_", beat_id)[:40] or "beat"
        safe_plan = re.sub(r"[^A-Za-z0-9_-]", "_", plan_id)[:64] or "plan"
        uploads = Path(settings.work_dir) / safe_plan / "uploads"
        # Checked BEFORE the mkdir, not after: a containment check that runs
        # once the directory already exists has already let the filesystem
        # be touched outside work_dir, which is the whole thing it is for.
        if not _under_roots(uploads, [Path(settings.work_dir)]):
            raise HTTPException(500, "upload directory escaped work_dir")
        uploads.mkdir(parents=True, exist_ok=True)
        partial = uploads / f"{safe_beat}-{slot:02d}.part"

        written = 0
        try:
            with partial.open("wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            413, f"the upload passed the "
                                 f"{settings.upload_max_mb:g} MB cap "
                                 f"(RAHASYA_UPLOAD_MAX_MB) and was stopped "
                                 f"there; nothing was kept.")
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(400, "the upload was empty")

            with partial.open("rb") as handle:
                head = handle.read(32)
            sniffed = _sniff_media(head)
            if sniffed is None:
                raise HTTPException(
                    415, "those bytes are not any media type this slot "
                         "accepts. The extension and the Content-Type are "
                         "not trusted here; the file's own signature is.")
            suffix, kind = UPLOAD_KINDS[sniffed]
            if kind != UPLOAD_KINDS[declared][1]:
                raise HTTPException(
                    415, f"the upload says it is {declared} but the bytes "
                         f"are {sniffed}. Send the file you meant to send.")
            if not _decodes_as_media(partial, settings):
                raise HTTPException(
                    415, f"ffmpeg could not read a frame out of that file. "
                         f"It carries a {sniffed} signature but does not "
                         f"decode, and the render would have failed on it "
                         f"long after you had stopped watching.")

            destination = uploads / f"{safe_beat}-{slot:02d}{suffix}"
            if not _under_roots(destination, [Path(settings.work_dir)]):
                raise HTTPException(500, "upload path escaped work_dir")
            # A previous upload into this slot with a different extension
            # would otherwise sit there orphaned, and its cached poster
            # with it.
            for stale in uploads.glob(f"{safe_beat}-{slot:02d}.*"):
                if stale != destination and stale != partial:
                    stale.unlink(missing_ok=True)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)

        # The poster cache is keyed on the clip path and only re-extracted
        # when the clip is newer, which it is — but a replacement that
        # lands inside the same second would tie. Drop it outright.
        _poster_path(destination).unlink(missing_ok=True)

        existing = beat.clips[slot]
        beat.clips[slot] = Clip.model_validate({
            **existing.model_dump(),
            "path": str(destination),
            "provider": UPLOAD_PROVIDER,
            # Cleared: whatever this came from, it did not come from there.
            "source_url": None, "pexels_id": None, "author": None,
            "licence": None,
            # duration is deliberately absent from this update.
        })
        store.save_plan(plan, status=CLIP_REVIEW_STATUS)

        return {
            "plan_id": plan_id, "beat_id": beat_id, "slot": slot,
            "provider": UPLOAD_PROVIDER,
            "duration": beat.clips[slot].duration,
            "kind": kind, "bytes": written, "media_type": sniffed,
            "thumb": f"/api/frame/{plan_id}/{beat_id}?slot={slot}",
        }

    # -- the sticker picker -------------------------------------------------
    # Same shape as the clip picker just above: candidates, a choose action,
    # a gate. Here the gate is the catalogue check in choose_sticker, not a
    # plan status -- a chosen icon does not block anything downstream, so
    # there is no review stage to hold it at.
    @app.get("/api/plan/{plan_id}/stickers")
    def sticker_candidates(plan_id: str) -> dict:
        """Every sticker that fired, with icons that could replace it.

        Per fired cue, not per trigger: a trigger that did not fire has
        nothing to show, and showing it anyway would invite choosing art for
        a sticker this reel will never render.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")

        cache = Path(settings.work_dir) / "_lordicon"
        note = ""
        slugs: tuple[str, ...] = ()
        try:
            sticker_catalog.refresh(cache)
            slugs = sticker_catalog.load(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            # A picker that cannot list icons is an inconvenience. The
            # committed art still renders, so this is a note, not an error.
            note = f"catalogue unavailable: {exc}"

        chosen = store.sticker_choices(plan_id)
        triggers = {t.name: t for t in stickers_mod.load_triggers()}

        rows = []
        for cue in stickers_mod.find_cues(plan,
                                          cap=int(settings.sticker_max)):
            trigger = triggers.get(cue.name)
            seen: list[str] = []
            for term in (trigger.search if trigger else ()):
                for slug in sticker_catalog.search(term, slugs):
                    if slug not in seen:
                        seen.append(slug)
            rows.append({
                "trigger": cue.name,
                "word": cue.word,
                "start": round(cue.start, 2),
                "beat_id": plan.script.beats[cue.beat_index].beat_id,
                "chosen": chosen.get(cue.name),
                "candidates": [
                    {"slug": slug,
                     "preview": f"/api/sticker-preview/{slug}"}
                    for slug in seen[:8]],
                "choose": f"/api/plan/{plan_id}/sticker/{cue.name}",
            })
        return {"plan_id": plan_id, "rows": rows, "note": note}

    @app.get("/api/sticker-preview/{slug}")
    def sticker_preview(slug: str) -> FileResponse:
        """One matted frame of an icon, for the picker to show."""
        cache = Path(settings.work_dir) / "_lordicon"
        slugs = sticker_catalog.load(cache)
        if slug not in slugs:
            raise HTTPException(404, "not a catalogue slug")
        try:
            png = sticker_choices_mod.preview_png(slug, root=cache)
        except Exception as exc:
            raise HTTPException(502, f"preview failed: {exc}") from exc
        return FileResponse(png, media_type="image/png")

    @app.post("/api/plan/{plan_id}/sticker/{trigger}")
    def choose_sticker(plan_id: str, trigger: str,
                       body: StickerChoice) -> dict:
        """Bake one icon for this reel and remember it.

        The slug is checked against the catalogue before anything is
        fetched. Without that check this route is an arbitrary URL fetcher
        with the panel's network access.
        """
        if store.get_plan(plan_id) is None:
            raise HTTPException(404, "no such plan")

        cache = Path(settings.work_dir) / "_lordicon"
        try:
            sticker_catalog.refresh(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            raise HTTPException(503, f"catalogue unavailable: {exc}") from exc
        if body.slug not in sticker_catalog.load(cache):
            raise HTTPException(400, "not a catalogue slug")

        size = stickers_mod.sticker_size(settings.width,
                                         settings.sticker_scale)
        try:
            sticker_choices_mod.ensure_baked(
                body.slug, root=cache, size=size, fps=int(settings.fps))
        except ValueError as exc:
            # The icon has a pocket of trapped white and would render with a
            # blob in it. Refusing keeps whatever was chosen before.
            raise HTTPException(422, str(exc)) from exc

        store.choose_sticker(plan_id, trigger, body.slug)
        return {"plan_id": plan_id, "trigger": trigger, "slug": body.slug,
                "preview": f"/api/sticker-preview/{body.slug}"}

    @app.get("/api/plan/{plan_id}/publish")
    def publish_preview(plan_id: str) -> dict:
        """Payloads and a checklist to copy out. Publishes nothing."""
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        if plan.metadata is None:
            raise HTTPException(409, "plan has no metadata yet")
        row = next((r for r in store.list_plans(200)
                    if r["plan_id"] == plan_id), {})
        video = row.get("video") or ""
        rendered = store.render_duration(plan_id)
        # Read back from the render that made this MP4, never recomputed.
        # prepare() answers "what would a render started right now use?",
        # which is a different question: RAHASYA_STICKERS, the sticker scale
        # and the contents of engine/data/stickers/ all move independently of
        # a file that was written days ago. Asking it here put the Lordicon
        # credit on six stored plans whose videos predate any baked art, and
        # would equally have stripped it from a reel that is full of it the
        # moment stickers were switched off. NULL means no credit, which is
        # exactly what a render from before this was recorded knows.
        credit = store.render_attribution(plan_id)
        return {
            "youtube": youtube_payload(plan, video, attribution=credit),
            "instagram": instagram_payload(
                plan, "https://REPLACE-WITH-PUBLIC-URL/video.mp4",
                attribution=credit),
            "checklist": publish_checklist(plan, video,
                                           actual_duration=rendered),
            "note": ("Nothing here has been published. Run the upload "
                     "yourself with the video in front of you."),
        }

    # -- progress ---------------------------------------------------------
    @app.get("/api/events/{plan_id}")
    def events(plan_id: str) -> StreamingResponse:
        outbox = channel(plan_id)

        def stream():
            yield ": connected\n\n"
            while True:
                try:
                    payload = outbox.get(timeout=20)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
                if payload.get("stage") == "complete":
                    # Drop the queue. One was created per plan AND per async
                    # job id, and nothing ever removed them.
                    with lock:
                        channels.pop(plan_id, None)
                    break

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- media ------------------------------------------------------------
    @app.get("/media/{name}")
    def media(name: str) -> FileResponse:
        # Serve only from out_dir, and only by basename, so a crafted name
        # cannot walk out of it.
        target = (Path(settings.out_dir) / Path(name).name).resolve()
        if not target.is_file() or Path(settings.out_dir).resolve() not in \
                target.parents:
            raise HTTPException(404, "not found")
        return FileResponse(target)

    @app.get("/api/frame/{plan_id}/{beat_id}")
    def frame(plan_id: str, beat_id: str,
              slot: int | None = None) -> FileResponse:
        """A thumbnail for the scene strip: the beat's visual, browser-safe.

        A beat's visual is now ``beat.clips`` (Pexels footage, or a still
        wrapped as a Clip when sourcing fell back). A video clip can't be
        put in an <img> directly, so it gets a poster frame extracted with
        ffmpeg and cached next to the clip, re-extracted if the clip file
        has been overwritten more recently than the cached poster (a
        re-produce of the same plan reuses the same filename in place).
        Beats saved before clips existed have no ``clips`` at all, so those
        still fall back to the legacy ``beat.image_path``.

        ``?slot=N`` narrows it to exactly one of the beat's clips, which is
        what the clip review board asks for: a beat holds several and the
        reviewer has to see each one. Extended here rather than added as a
        second route, because the containment checks, the poster cache and
        its staleness rule are the part worth having only one of — the
        route was found trusting a stored path once already.

        With a slot named there is deliberately no fallback: if that slot's
        file is missing, the answer is 404, not the next clip along. A
        thumbnail quietly showing a different clip is exactly the failure
        this gate exists to catch.

        Every path here is server-controlled today (deterministic filenames
        under ``work_dir``, no API lets a caller set a beat's clip/image
        path), but it is still keyed by URL input and read from disk, so it
        gets the same containment check as ``/media``: nothing outside
        ``work_dir``/``out_dir`` is ever served or written to.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                   None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")

        roots = [Path(settings.work_dir), Path(settings.out_dir)]

        if slot is not None:
            if not 0 <= slot < len(beat.clips):
                raise HTTPException(
                    404, f"beat {beat_id!r} has {len(beat.clips)} clip "
                         f"slots, so there is no slot {slot}")
            served = _serve_visual(Path(beat.clips[slot].path), roots,
                                   settings)
            if served is None:
                raise HTTPException(
                    404, f"slot {slot} of beat {beat_id!r} has no usable "
                         f"file on disk")
            return served

        for clip in beat.clips:
            served = _serve_visual(Path(clip.path), roots, settings)
            if served is not None:
                return served

        if beat.image_path:
            path = Path(beat.image_path)
            if path.is_file() and _under_roots(path, roots):
                return FileResponse(path)

        raise HTTPException(
            404, f"beat {beat_id!r} has no usable clip on disk and no "
                 f"legacy image_path")

    app.state.settings = settings
    app.state.store = store
    return app


app = create_app()


def main() -> None:
    import uvicorn
    print("Rahasya Engine -> http://127.0.0.1:8765")
    uvicorn.run("engine.app:app", host="127.0.0.1", port=8765,
                reload=False, log_level="warning")


if __name__ == "__main__":
    main()

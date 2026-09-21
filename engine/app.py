"""FastAPI app: the local control room.

Two routes matter beyond CRUD:

  ``POST /api/plan/{id}/approve``  the human gate. Nothing renders until this
                                  has been called with a chosen hook.
  ``GET  /api/events/{id}``        server-sent progress, so a 45-second render
                                  is watchable rather than a spinner.

There is no publish route. Payload builders are exposed for copy-out only.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.assembly.render import VIDEO_SUFFIXES
from engine.config import Settings
from engine.contract import Beat, Motion, Transition
from engine.omniroute import OmniRouteClient
from engine.pipeline import (BudgetError, GateError, PipelineEvent,
                             Stage, plan_stage, produce_stage)
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


def _extract_poster(clip_path: Path, poster_path: Path,
                    settings: Settings) -> None:
    """Grab a single frame from ``clip_path`` and write it to ``poster_path``.

    Same subprocess shape as ``engine.media.piper_voice``: run, check the
    return code and the output file, and raise with ffmpeg's own stderr
    rather than swallowing the failure.
    """
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(clip_path), "-frames:v", "1", "-q:v", "3",
         str(poster_path)],
        capture_output=True)
    if result.returncode != 0 or not poster_path.is_file():
        detail = result.stderr.decode("utf-8", "replace")[-300:]
        raise RuntimeError(
            f"ffmpeg could not extract a frame from {clip_path}: {detail}")


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


class ApproveRequest(BaseModel):
    chosen_hook: str
    beats: list[BeatEdit] | None = None


class ProduceRequest(BaseModel):
    captions_source: str = "caption_text"
    use_fake: bool = False


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

        # The human gate is a constraint, so it is enforced here rather than
        # only in the browser. Without this, any script, retry or curl could
        # render and package a plan nobody had looked at.
        status = store.plan_status(plan_id)
        if status not in {"approved", "produced", "qc_failed"}:
            raise HTTPException(
                409, f"plan is '{status}', not approved. POST "
                     f"/api/plan/{plan_id}/approve with a chosen hook first.")

        emit = emitter(plan_id)

        def work() -> None:
            client = None
            try:
                client = client_for(request.use_fake)
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
        return {"plan_id": plan_id, "streaming": f"/api/events/{plan_id}"}

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
        return {
            "youtube": youtube_payload(plan, video),
            "instagram": instagram_payload(
                plan, "https://REPLACE-WITH-PUBLIC-URL/video.mp4"),
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
    def frame(plan_id: str, beat_id: str) -> FileResponse:
        """A thumbnail for the scene strip: the beat's visual, browser-safe.

        A beat's visual is now ``beat.clips`` (Pexels footage, or a still
        wrapped as a Clip when sourcing fell back). A video clip can't be
        put in an <img> directly, so it gets a poster frame extracted with
        ffmpeg and cached next to the clip. Beats saved before clips
        existed have no ``clips`` at all, so those still fall back to the
        legacy ``beat.image_path``.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                   None)
        if beat is None:
            raise HTTPException(404, f"no such beat: {beat_id}")

        for clip in beat.clips:
            clip_path = Path(clip.path)
            if not clip_path.is_file():
                continue
            if clip_path.suffix.lower() not in VIDEO_SUFFIXES:
                return FileResponse(clip_path)
            poster_path = _poster_path(clip_path)
            if not poster_path.is_file():
                try:
                    _extract_poster(clip_path, poster_path, settings)
                except RuntimeError:
                    continue
            if poster_path.is_file():
                return FileResponse(poster_path)

        if beat.image_path:
            path = Path(beat.image_path)
            if path.is_file():
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

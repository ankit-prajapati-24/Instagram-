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
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.config import Settings
from engine.contract import Beat, ReelPlan
from engine.omniroute import OmniRouteClient
from engine.pipeline import (BudgetError, GateError, PipelineEvent,
                             Stage, plan_stage, produce_stage)
from engine.publish.payloads import (instagram_payload, publish_checklist,
                                     youtube_payload)
from engine.store import Store

UI_DIR = Path(__file__).resolve().parent / "ui"


class PlanRequest(BaseModel):
    topic: str
    use_fake: bool = False


class BeatEdit(BaseModel):
    beat_id: str
    voice_text: str
    caption_text: str
    on_screen_text: str | None = None
    visual_prompt: str
    motion: str
    transition: str


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
                with client_for(False) as probe:
                    gateway = probe.health(timeout=6.0)
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
            "voice": settings.voice,
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

    @app.post("/api/plan")
    def create_plan(request: PlanRequest) -> dict:
        if not request.topic.strip():
            raise HTTPException(400, "topic is empty")
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
        if not request.topic.strip():
            raise HTTPException(400, "topic is empty")
        # A provisional id so the browser can subscribe before work starts.
        import uuid
        job_id = str(uuid.uuid4())
        emit = emitter(job_id)

        def work() -> None:
            client = client_for(request.use_fake)
            try:
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

        if request.beats:
            edits = {b.beat_id: b for b in request.beats}
            rebuilt: list[Beat] = []
            for beat in plan.script.beats:
                edit = edits.get(beat.beat_id)
                if edit is None:
                    rebuilt.append(beat)
                    continue
                rebuilt.append(beat.model_copy(update={
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

        # Beat 1 always carries the chosen hook's words.
        chosen = plan.chosen()
        if chosen and plan.script.beats:
            plan.script.beats[0] = plan.script.beats[0].model_copy(update={
                "voice_text": chosen.voice_text,
                "caption_text": chosen.caption_text,
                "measured_seconds": None, "words": []})

        store.save_plan(plan, status="approved")
        store.record_entities(plan.plan_id, plan.topic.entities)
        return {"plan_id": plan.plan_id, "status": "approved",
                "beats": len(plan.script.beats)}

    @app.post("/api/plan/{plan_id}/produce")
    def produce(plan_id: str, request: ProduceRequest) -> dict:
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        emit = emitter(plan_id)

        def work() -> None:
            client = client_for(request.use_fake)
            try:
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
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        for beat in plan.script.beats:
            if beat.beat_id == beat_id and beat.image_path:
                path = Path(beat.image_path)
                if path.is_file():
                    return FileResponse(path)
        raise HTTPException(404, "no image for that beat")

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

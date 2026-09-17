"""Stage orchestration.

Split deliberately in two, either side of the human gate:

  ``plan_stage``    research -> hooks -> script -> metadata -> moderation ->
                    dedup. Cheap, text only, and stops before anything is
                    rendered.
  ``produce_stage`` images -> voice -> captions -> render -> QC. This is where
                    time and credits go, so it only ever runs on a plan a
                    human approved.

No function here publishes anything, and nothing calls into
``engine.publish``. That is a constraint from the spec, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from engine.agents import (AgentError, run_hooks, run_metadata, run_research,
                           run_script)
from engine.assembly.captions import write_ass
from engine.assembly.render import probe_video, render
from engine.contract import ReelPlan, Script, Topic
from engine.gates import dedup
from engine.gates.qc import run_qc
from engine.media.images import generate_plan_images
from engine.media.voice import synth_plan


class Stage:
    RESEARCH = "research"
    HOOKS = "hooks"
    SCRIPT = "script"
    METADATA = "metadata"
    MODERATION = "moderation"
    DEDUP = "dedup"
    IMAGES = "images"
    VOICE = "voice"
    CAPTIONS = "captions"
    RENDER = "render"
    QC = "qc"

    ORDER = (RESEARCH, HOOKS, SCRIPT, METADATA, MODERATION, DEDUP,
             IMAGES, VOICE, CAPTIONS, RENDER, QC)
    PLAN = (RESEARCH, HOOKS, SCRIPT, METADATA, MODERATION, DEDUP)
    PRODUCE = (IMAGES, VOICE, CAPTIONS, RENDER, QC)


@dataclass
class PipelineEvent:
    stage: str
    status: str            # "started" | "done" | "failed" | "info"
    detail: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "status": self.status,
                "detail": self.detail, "payload": self.payload}


Emit = Callable[[PipelineEvent], None]


class GateError(RuntimeError):
    """A gate refused the plan. Never retried — it goes to the human queue."""

    def __init__(self, gate: str, detail: str):
        super().__init__(f"{gate}: {detail}")
        self.gate = gate
        self.detail = detail


class BudgetError(RuntimeError):
    """The daily spend ceiling was hit before a paid call was made."""


def _noop(event: PipelineEvent) -> None:
    return None


def _check_budget(store, settings) -> None:
    spent = store.today_usd()
    if spent >= settings.daily_usd_ceiling:
        raise BudgetError(
            f"daily ceiling reached: ${spent:.4f} of "
            f"${settings.daily_usd_ceiling:.2f}. Raise RAHASYA_DAILY_USD or "
            f"wait for the next day.")


def plan_stage(topic_raw: str, client, store, settings, *,
               emit: Emit = _noop) -> ReelPlan:
    """Everything up to the human gate. Returns a draft ReelPlan."""
    _check_budget(store, settings)
    topic = Topic.make(topic_raw)

    # Cheap layers first, before a single token is spent.
    early = dedup.check(topic, store, None,
                        trigram_threshold=settings.dedup_trigram,
                        cooldown_days=settings.entity_cooldown_days)
    if not early.passed:
        emit(PipelineEvent(Stage.DEDUP, "failed", early.detail,
                           {"layer": early.layer}))
        raise GateError("dedup", f"[{early.layer}] {early.detail}")

    plan = ReelPlan(topic=topic,
                    script=Script(total_seconds=settings.target_seconds,
                                  chosen_hook="h1", beats=[]))

    try:
        emit(PipelineEvent(Stage.RESEARCH, "started"))
        provenance, cost = run_research(client, topic,
                                       model=settings.model_cheap or None)
        store.record_cost(plan.plan_id, Stage.RESEARCH, cost)
        plan.provenance = provenance
        # The cooldown layer is only as good as this list; research is the
        # first stage that actually knows what the topic is about.
        if provenance.entities:
            plan.topic.entities = provenance.entities
            topic = plan.topic
        emit(PipelineEvent(
            Stage.RESEARCH, "done",
            f"{len(provenance.claims)} sourced claims, "
            f"{len(provenance.entities)} entities"))

        emit(PipelineEvent(Stage.HOOKS, "started"))
        hooks, cost = run_hooks(client, topic, provenance,
                               model=settings.model_strong or None)
        store.record_cost(plan.plan_id, Stage.HOOKS, cost)
        plan.hooks = hooks
        emit(PipelineEvent(Stage.HOOKS, "done", f"{len(hooks)} variants",
                           {"hooks": [h.model_dump() for h in hooks]}))

        emit(PipelineEvent(Stage.SCRIPT, "started"))
        script, cost = run_script(client, topic, provenance, hooks[0],
                                  model=settings.model_strong or None)
        store.record_cost(plan.plan_id, Stage.SCRIPT, cost)
        script.chosen_hook = hooks[0].variant_id
        plan.script = script
        emit(PipelineEvent(Stage.SCRIPT, "done",
                           f"{len(script.beats)} beats, "
                           f"~{plan.duration():.0f}s estimated"))

        emit(PipelineEvent(Stage.METADATA, "started"))
        metadata, cost = run_metadata(client, topic, script, provenance,
                                      model=settings.model_cheap or None)
        store.record_cost(plan.plan_id, Stage.METADATA, cost)
        plan.metadata = metadata
        emit(PipelineEvent(Stage.METADATA, "done", metadata.yt_title))
    except AgentError as exc:
        emit(PipelineEvent(exc.stage, "failed", exc.detail))
        raise

    # Topic entities come out of the script, so cooldown is re-checked with
    # what the model actually wrote about.
    emit(PipelineEvent(Stage.MODERATION, "started"))
    verdict = client.moderate(plan.all_text())
    store.record_cost(plan.plan_id, Stage.MODERATION, verdict.cost)
    plan.safety.moderation_passed = not verdict.flagged
    plan.safety.flags = verdict.flags
    if verdict.flagged:
        emit(PipelineEvent(Stage.MODERATION, "failed", str(verdict.flags)))
        store.save_plan(plan, status="rejected_moderation")
        raise GateError("moderation", f"flags: {verdict.flags}")
    emit(PipelineEvent(Stage.MODERATION, "done", "clean"))

    emit(PipelineEvent(Stage.DEDUP, "started", "semantic layer"))
    result = dedup.check(topic, store, client,
                         embed_text=f"{topic.raw} :: {plan.metadata.yt_title}",
                         trigram_threshold=settings.dedup_trigram,
                         cosine_threshold=settings.dedup_cosine,
                         cooldown_days=settings.entity_cooldown_days)
    if not result.passed:
        emit(PipelineEvent(Stage.DEDUP, "failed", result.detail,
                           {"layer": result.layer}))
        store.save_plan(plan, status="rejected_dedup")
        raise GateError("dedup", f"[{result.layer}] {result.detail}")
    emit(PipelineEvent(Stage.DEDUP, "done", result.detail))

    plan.cost.usd = store.plan_cost(plan.plan_id)
    store.save_plan(plan, status="awaiting_approval")
    return plan


def produce_stage(plan: ReelPlan, client, store, settings, *,
                  emit: Emit = _noop,
                  captions_source: str | None = None,
                  music_path: str | None = None) -> dict:
    """Everything after the human gate. Returns the render result."""
    _check_budget(store, settings)
    settings.ensure_dirs()
    captions_source = captions_source or settings.captions_source

    emit(PipelineEvent(Stage.IMAGES, "started",
                       f"{len(plan.script.beats)} scenes"))
    counts = generate_plan_images(
        plan, client, settings.work_dir, store,
        model=settings.model_image or None,
        progress=lambda i, n, beat, provider: emit(PipelineEvent(
            Stage.IMAGES, "info", f"{i}/{n} {beat} via {provider}")))
    emit(PipelineEvent(Stage.IMAGES, "done", ", ".join(
        f"{k}:{v}" for k, v in counts.items()), {"providers": counts}))

    emit(PipelineEvent(Stage.VOICE, "started", settings.voice))
    synth_plan(plan, settings.work_dir, settings,
               progress=lambda i, n, beat, secs: emit(PipelineEvent(
                   Stage.VOICE, "info", f"{i}/{n} {beat} {secs:.1f}s")))
    emit(PipelineEvent(Stage.VOICE, "done",
                       f"{plan.duration():.1f}s measured"))

    emit(PipelineEvent(Stage.CAPTIONS, "started", captions_source))
    font = (settings.caption_font_devanagari
            if captions_source == "voice_text" else settings.caption_font)
    ass_path = write_ass(
        plan, Path(settings.work_dir) / plan.plan_id / "captions.ass",
        source=captions_source, font=font, font_size=settings.caption_size,
        width=settings.width, height=settings.height)
    emit(PipelineEvent(Stage.CAPTIONS, "done", Path(ass_path).name))

    emit(PipelineEvent(Stage.RENDER, "started"))
    out_path = Path(settings.out_dir) / f"{plan.topic.slug}-{plan.plan_id[:8]}.mp4"
    key = f"render:{plan.plan_id}:v1"
    try:
        render(plan, settings, out_path, ass_path=ass_path,
               music_path=music_path,
               progress=lambda frac: emit(PipelineEvent(
                   Stage.RENDER, "info", f"{frac * 100:.0f}%")))
    except Exception as exc:
        store.record_render(plan.plan_id, key, "failed",
                            error_log=str(exc)[:2000])
        emit(PipelineEvent(Stage.RENDER, "failed", str(exc)[:300]))
        raise
    info = probe_video(out_path, settings.ffmpeg)
    store.record_render(plan.plan_id, key, "done", output_path=str(out_path),
                        duration_s=info.get("duration"))
    emit(PipelineEvent(Stage.RENDER, "done",
                       f"{info.get('duration', 0):.1f}s, "
                       f"{info.get('bytes', 0) // 1024}KB", info))

    emit(PipelineEvent(Stage.QC, "started"))
    scorecard = run_qc(plan, duration_min=settings.duration_min,
                       duration_max=settings.duration_max,
                       actual_duration=info.get("duration"),
                       narration_seconds=plan.duration(),
                       expect_render=True)
    plan.cost.usd = store.plan_cost(plan.plan_id)
    store.save_plan(plan, status="produced" if scorecard.passed
                    else "qc_failed")
    emit(PipelineEvent(
        Stage.QC, "done" if scorecard.passed else "failed",
        "all checks passed" if scorecard.passed
        else f"hard failures: {[c.name for c in scorecard.hard_failures]}",
        scorecard.to_dict()))

    return {"video": str(out_path), "ass": ass_path, "probe": info,
            "scorecard": scorecard.to_dict(),
            "cost_usd": store.plan_cost(plan.plan_id),
            "image_providers": counts}

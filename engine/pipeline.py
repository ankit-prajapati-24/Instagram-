"""Stage orchestration.

Split deliberately in two, either side of the human gate:

  ``plan_stage``    research -> hooks -> script -> metadata -> moderation ->
                    dedup. Cheap, text only, and stops before anything is
                    rendered.
  ``produce_stage`` voice -> length -> clips -> captions -> render -> QC.
                    This is where time and credits go, so it only ever runs
                    on a plan a human approved. Voice runs before clips
                    because clip count is derived from
                    ``beat.measured_seconds``, which synthesis is what
                    writes — and the length gate runs immediately after it,
                    because that is the first moment the finished runtime is
                    known and the last one before the expensive stages.

No function here publishes anything, and nothing calls into
``engine.publish``. That is a constraint from the spec, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from engine.agents import (AgentError, run_hooks, run_metadata, run_research,
                           run_script)
from engine.assembly import audio
from engine.assembly.captions import write_ass
from engine.assembly.render import probe_video, render
from engine.config import beat_count, word_budget
from engine.contract import ReelPlan, Script, Topic
from engine.gates import dedup
from engine.gates.qc import (PRE_RENDER_MARGIN, duration_in_range,
                             pre_render_range, run_qc)
from engine.media.clips import generate_plan_clips, unavailable_reason
from engine.media.voice import synth_plan
from stock_agent import StockVideoMatcherAgent


class Stage:
    RESEARCH = "research"
    HOOKS = "hooks"
    SCRIPT = "script"
    METADATA = "metadata"
    MODERATION = "moderation"
    DEDUP = "dedup"
    CLIPS = "clips"
    VOICE = "voice"
    LENGTH = "length"
    CAPTIONS = "captions"
    RENDER = "render"
    QC = "qc"

    ORDER = (RESEARCH, HOOKS, SCRIPT, METADATA, MODERATION, DEDUP,
             VOICE, LENGTH, CLIPS, CAPTIONS, RENDER, QC)
    PLAN = (RESEARCH, HOOKS, SCRIPT, METADATA, MODERATION, DEDUP)
    PRODUCE = (VOICE, LENGTH, CLIPS, CAPTIONS, RENDER, QC)


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

        # Cooldown is checked here, as soon as the entities exist, rather than
        # after metadata. Waiting meant a rejection cost three more agent
        # calls (hooks, script, metadata) for a plan that was never viable.
        if provenance.entities:
            cooling = store.cooldown_detail(provenance.entities,
                                            settings.entity_cooldown_days)
            if cooling:
                listed = ", ".join(f"{name} (free in {days}d)"
                                   for name, days in cooling)
                emit(PipelineEvent(Stage.DEDUP, "failed",
                                   f"covered too recently: {listed}",
                                   {"layer": "cooldown"}))
                raise GateError("dedup", f"[cooldown] covered too "
                                         f"recently: {listed}")

        emit(PipelineEvent(Stage.HOOKS, "started"))
        hooks, cost = run_hooks(client, topic, provenance,
                               model=settings.model_strong or None)
        store.record_cost(plan.plan_id, Stage.HOOKS, cost)
        plan.hooks = hooks
        emit(PipelineEvent(Stage.HOOKS, "done", f"{len(hooks)} variants",
                           {"hooks": [h.model_dump() for h in hooks]}))

        emit(PipelineEvent(Stage.SCRIPT, "started"))
        script, cost = run_script(client, topic, provenance, hooks[0],
                                  model=settings.model_strong or None,
                                  word_target=word_budget(settings),
                                  beats=beat_count(settings))
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
    plan.safety.flags = verdict.flags
    plan.safety.moderation_unavailable = verdict.unavailable

    if verdict.flagged:
        # An actual flag is still a hard stop.
        plan.safety.moderation_passed = False
        emit(PipelineEvent(Stage.MODERATION, "failed", str(verdict.flags)))
        store.save_plan(plan, status="rejected_moderation")
        raise GateError("moderation", f"flags: {verdict.flags}")

    if not verdict.checked:
        # Unreachable checker. Stopping here would make the whole tool
        # unusable over a missing key, and passing it off as "clean" would
        # hide a real gap — so it is carried through as an explicit unchecked
        # state, surfaced in QC and on the publish checklist. The human
        # approval gate is the backstop.
        plan.safety.moderation_passed = False
        emit(PipelineEvent(Stage.MODERATION, "info",
                           f"NOT CHECKED - {verdict.unavailable}"))
    else:
        plan.safety.moderation_passed = True
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

    emit(PipelineEvent(Stage.VOICE, "started",
                       f"{settings.voice_engine}: "
                       f"{settings.piper_voice if settings.voice_engine == 'piper' else settings.voice}"))
    synth_plan(plan, settings.work_dir, settings,
               progress=lambda i, n, beat, secs, engine: emit(PipelineEvent(
                   Stage.VOICE, "info",
                   f"{i}/{n} {beat} {secs:.1f}s via {engine}")))
    emit(PipelineEvent(Stage.VOICE, "done",
                       f"{plan.duration():.1f}s measured"))

    # --- length gate ------------------------------------------------------
    #
    # Synthesis is the first stage that knows how long the video will be, and
    # the cheapest thing standing in front of the ones that do not care. On
    # the first real run, voice took about a minute and clips plus render took
    # the other twelve of 821 seconds — and QC then rejected the result for
    # being 66.5s against a 38-52s window, which was decided the moment the
    # script was written. Asking here costs nothing and saves the twelve
    # minutes; no stage after this one can shorten a script.
    #
    # The band comes from ``pre_render_range``, which is QC's own window plus
    # a deliberate margin — see PRE_RENDER_MARGIN for why the two differ and
    # why the difference is derived rather than written out a second time.
    # Deliberately wider than QC: refusing a near-miss here destroys the run
    # and produces nothing, where letting it through costs twelve minutes and
    # hands a human a video and a scorecard. The gate is for scripts that are
    # obviously wrong, not for the ones QC is there to judge.
    narration = plan.duration()
    gate_min, gate_max = pre_render_range(settings.duration_min,
                                          settings.duration_max)
    if not duration_in_range(narration, gate_min, gate_max):
        direction = "shorten" if narration > gate_max else "lengthen"
        detail = (
            f"the narration runs {narration:.1f}s, outside the "
            f"{gate_min:.0f}-{gate_max:.0f}s this gate accepts — QC's "
            f"{settings.duration_min:.0f}-{settings.duration_max:.0f}s "
            f"publishing window plus {PRE_RENDER_MARGIN:.0%} for how much "
            f"the voice's rate varies with the words it is given. Nothing "
            f"downstream changes a script's length, so this stops here "
            f"rather than spending the clip and render stages on it: "
            f"{direction} the script (RAHASYA_WORDS_PER_SEC sets the budget "
            f"it is written to) and run the plan again.")
        emit(PipelineEvent(Stage.LENGTH, "failed", detail,
                           {"narration_seconds": narration,
                            "gate_min": gate_min, "gate_max": gate_max,
                            "duration_min": settings.duration_min,
                            "duration_max": settings.duration_max}))
        store.save_plan(plan, status="rejected_length")
        raise GateError("length", detail)
    emit(PipelineEvent(Stage.LENGTH, "done",
                       f"{narration:.1f}s, inside "
                       f"{gate_min:.0f}-{gate_max:.0f}s"
                       + ("" if duration_in_range(narration,
                                                  settings.duration_min,
                                                  settings.duration_max)
                          else f" (but outside QC's "
                               f"{settings.duration_min:.0f}-"
                               f"{settings.duration_max:.0f}s — QC will fail "
                               f"this on duration)")))

    reason = unavailable_reason(settings)
    emit(PipelineEvent(Stage.CLIPS, "started",
                       f"{len(plan.script.beats)} scenes"))
    if reason:
        # Surfaced as its own event, not buried in the provider counts. A
        # run with no key still produces a video, which is exactly how a
        # broken setup passes for a working one.
        emit(PipelineEvent(Stage.CLIPS, "info", reason))
    agent = StockVideoMatcherAgent(
        omniroute_base_url=settings.omniroute_base,
        omniroute_api_key=settings.omniroute_key,
        omniroute_model=settings.model_cheap or None,
        pexels_api_key=settings.pexels_api_key)
    counts = generate_plan_clips(
        plan, agent, client, settings.work_dir, store,
        model=settings.model_image or None,
        use_keyless=settings.keyless_images,
        browser_image_api=settings.browser_image_api,
        workers=settings.image_workers,
        reason=reason,
        progress=lambda i, n, beat, provider: emit(PipelineEvent(
            Stage.CLIPS, "info", f"{i}/{n} {beat} via {provider}")))
    emit(PipelineEvent(Stage.CLIPS, "done", ", ".join(
        f"{k}:{v}" for k, v in counts.items()), {"providers": counts}))

    emit(PipelineEvent(Stage.CAPTIONS, "started", captions_source))
    font = (settings.caption_font_devanagari
            if captions_source == "voice_text" else settings.caption_font)
    ass_path = write_ass(
        plan, Path(settings.work_dir) / plan.plan_id / "captions.ass",
        source=captions_source, font=font, font_size=settings.caption_size,
        width=settings.width, height=settings.height)
    emit(PipelineEvent(Stage.CAPTIONS, "done", Path(ass_path).name))

    # The music bed. Nothing above this line knows about it and the caller
    # normally passes nothing, so this is where a clean clone gets one:
    # whatever is in `assets/music/`, preferring a real file over the
    # generated placeholder (see engine/assembly/audio.py and
    # scripts/make_audio_assets.py). Resolved here rather than inside
    # `render` so that a direct render stays exactly as explicit as it was.
    if music_path is None:
        music_path = audio.find_music(settings)
    emit(PipelineEvent(Stage.RENDER, "started",
                       f"music: {Path(music_path).name}" if music_path
                       else "music: none (assets/music/ is empty)"))
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
            "providers": counts}

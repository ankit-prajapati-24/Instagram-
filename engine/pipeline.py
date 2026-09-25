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

from engine.agents import (WORD_TOLERANCE, AgentError, latin_violations,
                           run_hooks, run_metadata, run_research, run_script,
                           script_words)
from engine.assembly import audio
from engine.assembly.captions import write_ass
from engine.assembly.render import probe_video, render
from engine.config import (beat_count, speech_rate, spoken_seconds,
                           word_budget)
from engine.contract import (Beat, Hook, Metadata, Provenance, ReelPlan,
                             Safety, Script, Topic)
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

    # Cheap layers first, before a single token is spent -- unless the
    # gate is off, in which case it is announced rather than skipped
    # quietly. A disabled gate that looked like a passing one would be a
    # lie the panel then repeats.
    if not settings.dedup_enabled:
        emit(PipelineEvent(Stage.DEDUP, "info", "the dedup gate is off (RAHASYA_DEDUP=0), so a topic already produced will not be refused",
                           {"enabled": False}))
    else:
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
        if provenance.entities and settings.dedup_enabled:
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

    if not settings.dedup_enabled:
        emit(PipelineEvent(Stage.DEDUP, "info", "the dedup gate is off (RAHASYA_DEDUP=0), so a topic already produced will not be refused",
                           {"enabled": False}))
    else:
        emit(PipelineEvent(Stage.DEDUP, "started", "semantic layer"))
        result = dedup.check(
            topic, store, client,
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


# --- the hand-written path --------------------------------------------------
#
# Same destination as ``plan_stage``, reached without a model. Every provider
# behind the gateway can be quota-blocked, keyless or 402 and this still
# produces a plan, because nothing below this line takes a client, makes an
# HTTP request or imports one that would. That is the feature: the function
# signature is the guarantee, not a promise in a docstring.
#
# What it skips, and why each skip is safe:
#
#   research    The user supplies the sources, or says out loud that there
#               are none. Never silently empty -- QC hard-fails an unsourced
#               claim, and discovering that after a ten-minute render is the
#               exact failure this path exists to avoid, so it is discovered
#               at the form instead.
#   hooks       Beat 1 IS the hook on a hand-written script. One Hook is
#               synthesised from it so the approve gate has something to
#               choose, and re-seeding beat 1 from it is then a no-op.
#   script      This is the thing the human wrote.
#   metadata    Optional. Left blank, the plan has none and the existing
#               publish-preview 409 says so at the only moment it matters.
#   moderation  Not run, and recorded as not run (see MANUAL_NOT_MODERATED)
#               so QC and the publish checklist both say NOT CHECKED. A
#               manual plan must never look moderated.
#   dedup       Layers 1, 2 and 4 need no model and all run. Layer 3 is the
#               embedding call, and ``dedup.check`` already skips it when it
#               is handed no client -- deliberately, so a missing gateway
#               cannot silently disable the cheap layers.
#
# The daily spend ceiling is deliberately NOT checked here: this path spends
# nothing, so a spend ceiling has no jurisdiction over it, and blocking the
# one route that still works on a day the ceiling was hit would be exactly
# backwards. ``produce_stage`` still checks it, because that stage does spend.

MANUAL_NOT_MODERATED = (
    "no moderation call was made — this script was written by hand and the "
    "manual path never sends it to a checker")

# The arc the blank form pre-fills. Not a rule: the role of every beat is a
# dropdown the user can change. hook first and cta last whatever the count.
MANUAL_ROLE_ARC = ("setup", "escalation", "reveal", "twist", "escalation",
                   "reveal", "twist", "cliffhanger")


class ManualScriptError(ValueError):
    """A hand-written script that cannot become a plan as typed.

    Carries every problem at once rather than the first one: a person
    retyping ten beats should be told about all ten, not made to resubmit
    ten times.
    """

    def __init__(self, problems: list[str]):
        super().__init__(" ".join(problems))
        self.problems = list(problems)


def default_roles(count: int | None = None, settings=None) -> list[str]:
    """Roles for a blank form of ``count`` beats. Opens on the hook, ends
    on the cta, cycles the middle."""
    from engine.config import settings as configured

    total = count or beat_count(settings or configured)
    if total <= 0:
        return []
    if total == 1:
        return ["hook"]
    if total == 2:
        return ["hook", "cta"]
    middle = [MANUAL_ROLE_ARC[i % len(MANUAL_ROLE_ARC)]
              for i in range(total - 2)]
    return ["hook", *middle, "cta"]


def budget_report(script: Script, settings) -> dict:
    """What the word count means, in one place, for the panel and the gate.

    Every number is derived from ``engine.config`` — ``word_budget()``,
    ``speech_rate()``, ``spoken_seconds()`` — and from the two gates' own
    constants (``WORD_TOLERANCE``, ``pre_render_range``). Nothing here is a
    literal, which is why the panel can render a live budget meter without
    knowing a single one of these numbers itself.
    """
    budget = word_budget(settings)
    words = script_words(script)
    drift = (words - budget) / budget if budget else 0.0
    band_min = round(budget * (1 - WORD_TOLERANCE))
    band_max = round(budget * (1 + WORD_TOLERANCE))
    in_band = abs(drift) <= WORD_TOLERANCE
    predicted = spoken_seconds(words, settings)
    gate_min, gate_max = pre_render_range(settings.duration_min,
                                          settings.duration_max)
    warning = ""
    if not in_band:
        warning = (
            f"{words} words is {drift * 100:+.0f}% off the {budget}-word "
            f"budget, outside the ±{WORD_TOLERANCE:.0%} band the script "
            f"agent is held to ({band_min}-{band_max} words). It will still "
            f"render — at {predicted:.1f}s it is inside the "
            f"{gate_min:.0f}-{gate_max:.0f}s the pre-render length gate "
            f"accepts — but QC scores the finished file against "
            f"{settings.duration_min:.0f}-{settings.duration_max:.0f}s.")
    return {
        "words": words,
        "budget": budget,
        "band_min": band_min,
        "band_max": band_max,
        "drift": round(drift, 4),
        "in_band": in_band,
        "tolerance": WORD_TOLERANCE,
        "speech_rate": speech_rate(settings),
        "predicted_seconds": predicted,
        "duration_min": settings.duration_min,
        "duration_max": settings.duration_max,
        "gate_min": round(gate_min, 1),
        "gate_max": round(gate_max, 1),
        "renderable": duration_in_range(predicted, gate_min, gate_max),
        "warning": warning,
    }


def build_manual_script(beats: list[dict], settings) -> Script:
    """Turn the form's rows into a validated ``Script``, or say why not.

    ``target_seconds`` is derived from the words at ``speech_rate()``,
    exactly as ``run_script``'s parser derives it for the model's output —
    it is a hint ``measured_seconds`` overrides later, and a hand-typed one
    would be the same seconds-first sizing that made the model overshoot.
    """
    rows = [dict(row) for row in beats]
    problems: list[str] = []

    if not rows:
        raise ManualScriptError(["the script has no beats at all."])

    low, high = settings.beats_min, settings.beats_max
    if not low <= len(rows) <= high:
        problems.append(
            f"{len(rows)} beats: this pipeline renders {low}-{high} of them "
            f"and the blank form starts at {beat_count(settings)}, which is "
            f"what the word budget is divided across.")

    for index, row in enumerate(rows, start=1):
        for field in ("voice_text", "caption_text", "visual_prompt"):
            if not str(row.get(field) or "").strip():
                problems.append(f"b{index}: {field} is empty and is required.")
    if problems:
        raise ManualScriptError(problems)

    rate = speech_rate(settings)
    built: list[Beat] = []
    for index, row in enumerate(rows, start=1):
        voice = str(row["voice_text"]).strip()
        built.append(Beat.model_validate({
            "beat_id": f"b{index}",
            "role": row.get("role") or "setup",
            "voice_text": voice,
            "caption_text": str(row["caption_text"]).strip(),
            "on_screen_text": (str(row.get("on_screen_text") or "").strip()
                               or None),
            # Passed straight through in the shape `read_script_json`
            # produces and `Beat` expects, so a pasted script's own sticker
            # survives the round trip through the form. None here is not a
            # gap: it is what makes the beat fall through to the trigger
            # map, the same as any model-authored beat that asked for
            # nothing.
            "sticker": row.get("sticker") or None,
            "visual_prompt": str(row["visual_prompt"]).strip(),
            "motion": row.get("motion") or "zoom_in",
            "transition": row.get("transition") or "fade",
            "target_seconds": round(max(len(voice.split()), 1) / rate, 2),
        }))

    script = Script(total_seconds=settings.target_seconds,
                    chosen_hook="h1", beats=built)

    # The rule the model is held to, held to here by the same function. Not
    # a second copy of the regex: see engine.agents.latin_violations.
    violations = latin_violations(script)
    if violations:
        raise ManualScriptError([
            "voice_text is fed straight to the Hindi voice and its spelling "
            "decides the pronunciation, so it has to be Devanagari only. "
            "ASCII digits, punctuation and ॰ are fine; Latin letters are "
            "not.",
            *(f"{beat_id}: Latin script in voice_text — "
              f"{', '.join(sorted(set(words)))}"
              for beat_id, words in violations),
            "Transliterate them (DNA → डीएनए, report → रिपोर्ट) and submit "
            "again. caption_text stays Roman Hinglish on purpose and is "
            "never checked.",
        ])

    report = budget_report(script, settings)
    if not report["renderable"]:
        raise ManualScriptError([
            f"{report['words']} spoken words reads as "
            f"{report['predicted_seconds']:.1f}s at "
            f"{report['speech_rate']:g} words/sec, outside the "
            f"{report['gate_min']:.0f}-{report['gate_max']:.0f}s the "
            f"pre-render length gate accepts.",
            "That gate runs after voice synthesis and nothing downstream "
            "changes a script's length, so it is asked here instead — at "
            "the form, for free, rather than after a full voice pass.",
            f"The budget is {report['budget']} words "
            f"({report['band_min']}-{report['band_max']} inside the "
            f"±{WORD_TOLERANCE:.0%} band).",
        ])
    return script


def manual_plan_stage(topic_raw: str, beats: list[dict], store, settings, *,
                      entities: list[str] | None = None,
                      claims: list | None = None,
                      metadata=None,
                      acknowledge_unsourced: bool = False,
                      emit: Emit = _noop) -> ReelPlan:
    """A plan from a script a human typed. Makes zero LLM calls.

    There is no ``client`` parameter, and that is load-bearing rather than
    tidy: this function is unable to reach a model because it is never given
    anything that could. ``tests/test_manual.py`` asserts that, and drives
    the whole path with the gateway pointed at a dead port and every agent
    entry point replaced by a raise.

    Returns the same ``ReelPlan``, saved with the same
    ``awaiting_approval`` status, as ``plan_stage`` does. From the approve
    gate onwards nothing can tell the two apart, and nothing should try.
    """
    script = build_manual_script(beats, settings)
    emit(PipelineEvent(Stage.SCRIPT, "done",
                       f"{len(script.beats)} beats written by hand, "
                       f"{script_words(script)} words"))

    # --- provenance ------------------------------------------------------
    # The stated constraint is "every factual claim carries a source URL, or
    # the claim is cut". Research is what normally collects them, and it did
    # not run, so the choice is handed to the person who wrote the script —
    # at the form, where it costs nothing, instead of at QC, where it costs
    # the render. A claim with no URL is refused; no claims at all is
    # allowed only when it was said out loud, and is then honest: a plan
    # that asserts nothing has nothing unsourced, so QC's claim_provenance
    # check passes rather than being bypassed.
    supplied = list(claims or [])
    unsourced = [c for c in supplied if not str(
        getattr(c, "source_url", None) or "").strip()]
    if unsourced:
        raise ManualScriptError([
            "every factual claim carries a source URL, or the claim is cut. "
            "QC hard-fails an unsourced claim, and it does that after the "
            "render — so these are refused now instead.",
            *(f"no source URL: {str(getattr(c, 'text', c))[:70]}"
              for c in unsourced),
        ])
    if not supplied and not acknowledge_unsourced:
        raise ManualScriptError([
            "no sources were supplied, and the research stage that normally "
            "collects them did not run on this path.",
            "Either give each factual claim a source URL, or say explicitly "
            "that you are proceeding without sources — asserting nothing "
            "that needs one.",
        ])
    provenance = Provenance(claims=supplied, searched_queries=[],
                            entities=[e.strip() for e in (entities or [])
                                      if str(e).strip()])
    emit(PipelineEvent(
        Stage.RESEARCH, "info",
        f"NOT RUN — {len(provenance.claims)} source(s) supplied by hand"
        if provenance.claims
        else "NOT RUN — proceeding with no sources, acknowledged"))

    # --- the hook --------------------------------------------------------
    # Beat 1 is the hook on a hand-written script, so the "variants" stage
    # has nothing to choose between. One Hook is still created, because the
    # approve gate requires a known chosen_hook and re-seeds beat 1 from it
    # — which, with the hook taken from beat 1, is a no-op.
    first = script.beats[0]
    style = ("question" if first.caption_text.rstrip().endswith("?")
             else "claim")
    hook = Hook(variant_id="h1", voice_text=first.voice_text,
                caption_text=first.caption_text, style=style,
                seconds=first.target_seconds)
    script.chosen_hook = hook.variant_id
    emit(PipelineEvent(Stage.HOOKS, "info",
                       "NOT RUN — beat 1 is the hook"))

    # --- metadata --------------------------------------------------------
    # Optional, and blank is a real answer: nothing between here and the
    # finished file reads it, and the publish preview already 409s without
    # it, at the only moment it actually matters.
    written = None
    if metadata is not None:
        candidate = (metadata if isinstance(metadata, Metadata)
                     else Metadata.model_validate(metadata))
        if any([candidate.yt_title.strip(), candidate.yt_description.strip(),
                candidate.ig_caption.strip(),
                candidate.pinned_comment.strip(), candidate.hashtags,
                candidate.thumbnail_prompt.strip()]):
            written = candidate
    emit(PipelineEvent(
        Stage.METADATA, "done" if written else "info",
        written.yt_title if written
        else "NOT RUN — left blank; the publish preview will 409"))

    topic = Topic.make(topic_raw, entities=provenance.entities)
    plan = ReelPlan(topic=topic, hooks=[hook], script=script,
                    metadata=written, provenance=provenance,
                    safety=Safety(moderation_passed=False, flags=[],
                                  moderation_unavailable=MANUAL_NOT_MODERATED))
    emit(PipelineEvent(Stage.MODERATION, "info",
                       f"NOT CHECKED - {MANUAL_NOT_MODERATED}"))

    # --- dedup -----------------------------------------------------------
    # client=None on purpose: layers 1, 2 and 4 are pure SQL and run in
    # full; layer 3 is the embedding call and dedup.check already skips it
    # rather than failing when it has nothing to call. Layer 4 keys off
    # topic.entities, which on this path are whatever the user typed — none
    # means the cooldown layer has nothing to compare and is inert, not
    # bypassed, and approve then records nothing for future cooldowns
    # either.
    if not settings.dedup_enabled:
        emit(PipelineEvent(
            Stage.DEDUP, "info",
            "the dedup gate is off (RAHASYA_DEDUP=0), so a topic already "
            "produced will not be refused", {"enabled": False}))
    else:
        emit(PipelineEvent(Stage.DEDUP, "started",
                           "exact, trigram and cooldown layers "
                           "(semantic needs an embedding provider)"))
        result = dedup.check(topic, store, None,
                             trigram_threshold=settings.dedup_trigram,
                             cooldown_days=settings.entity_cooldown_days)
        if not result.passed:
            emit(PipelineEvent(Stage.DEDUP, "failed", result.detail,
                               {"layer": result.layer}))
            raise GateError("dedup", f"[{result.layer}] {result.detail}")
        emit(PipelineEvent(
            Stage.DEDUP, "done",
            f"{result.detail} (semantic layer skipped: no model)"))

    store.save_plan(plan, status="awaiting_approval")
    return plan


def voice_stage(plan: ReelPlan, store, settings, *,
                emit: Emit = _noop) -> dict[str, int]:
    """VOICE, alone. Returns the word-timing-source counts.

    Its own stage rather than the head of ``clips_stage`` because it is the
    other thing a human may want to replace by hand, and this is the only
    moment at which replacing it is free. Every number downstream is
    derived from what this writes: clip count is ``ceil(measured / 2.5)``,
    the slot durations divide the measured span, the caption words are
    positions inside it, and the LENGTH gate and QC both judge the total.
    Swap a beat's audio after CLIPS has run and the footage has been cut
    for a beat that no longer exists.

    Takes no ``client``, the way ``render_stage`` and ``manual_plan_stage``
    take none, and for the same reason: synthesis never needed a model --
    Piper is local, edge-tts is its own service -- so the third of produce
    a human may redo by hand cannot reach the gateway at all. Structural,
    not a promise.
    """
    # Read here as well as in ``clips_stage`` so the one-shot path still
    # fails before a minute of synthesis rather than after it. Voice itself
    # spends nothing; this check is about what comes next.
    _check_budget(store, settings)
    settings.ensure_dirs()

    emit(PipelineEvent(Stage.VOICE, "started",
                       f"{settings.voice_engine}: "
                       f"{settings.piper_voice if settings.voice_engine == 'piper' else settings.voice}"))
    sources = synth_plan(
        plan, settings.work_dir, settings,
        progress=lambda i, n, beat, secs, engine: emit(PipelineEvent(
            Stage.VOICE, "info",
            f"{i}/{n} {beat} {secs:.1f}s via {engine}")))
    emit(PipelineEvent(Stage.VOICE, "done",
                       f"{plan.duration():.1f}s measured",
                       {"timing_sources": sources}))
    return sources


def voice_engines(plan: ReelPlan) -> dict[str, int]:
    """How many beats each engine spoke, read off the plan itself.

    ``synth_plan`` knows what it synthesised but not what a human replaced
    afterwards. Counted from the plan for the same reason
    ``clip_providers`` is: a resumed half must not report a tally the voice
    review gate has since made untrue.
    """
    counts: dict[str, int] = {}
    for beat in plan.script.beats:
        name = beat.voice_engine or "unknown"
        counts[name] = counts.get(name, 0) + 1
    return counts


def clips_stage(plan: ReelPlan, client, store, settings, *,
                emit: Emit = _noop) -> dict:
    """LENGTH -> CLIPS. Returns the provider counts.

    The middle third of produce, and the part worth looking at before the
    render runs: by the time this returns, every clip that will appear in
    the video exists on disk, with the query that found it still attached,
    and nothing expensive has happened to it yet. Stock footage frequently
    does not match the story -- a real run fetched a European city park for
    a script about skeletons in a frozen Himalayan lake -- and the only way
    a human could see that before was to wait out the render.

    Split here rather than anywhere else because this is the last point at
    which a clip can be swapped for free. CAPTIONS reads no clip, and
    RENDER reads every one of them.

    Expects ``voice_stage`` to have run: a clip count is derived from a
    beat's measured span, and synthesis is what measures it.
    """
    _check_budget(store, settings)
    settings.ensure_dirs()

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
    return counts


def clip_providers(plan: ReelPlan) -> dict[str, int]:
    """How many clips each provider supplied, read off the plan itself.

    ``generate_plan_clips`` returns the same tally, but only for the run
    that fetched them. A clip a human replaced by hand after the review
    gate never passed through it, so the resumed half counts what the plan
    actually holds instead of carrying a number that is now a lie.
    """
    counts: dict[str, int] = {}
    for beat in plan.script.beats:
        for clip in beat.clips:
            counts[clip.provider] = counts.get(clip.provider, 0) + 1
    return counts


def render_stage(plan: ReelPlan, store, settings, *,
                 emit: Emit = _noop,
                 captions_source: str | None = None,
                 music_path: str | None = None,
                 providers: dict | None = None) -> dict:
    """CAPTIONS -> RENDER -> QC. Returns the render result.

    Takes no ``client``, and that is load-bearing rather than tidy, the
    same way ``manual_plan_stage`` takes none: everything after the clip
    review gate is ffmpeg and SQLite, so the second half of a reviewed
    produce cannot reach a model, spend a credit, or re-fetch the footage
    a human just finished correcting. The budget check lives in
    ``clips_stage`` for the same reason — this half has nothing to spend.
    """
    settings.ensure_dirs()
    captions_source = captions_source or settings.captions_source
    counts = providers if providers is not None else clip_providers(plan)

    emit(PipelineEvent(Stage.CAPTIONS, "started", captions_source))
    from engine.assembly import looks as looks_mod

    look = looks_mod.resolve(getattr(settings, "look", None))
    ass_path = write_ass(
        plan, Path(settings.work_dir) / plan.plan_id / "captions.ass",
        source=captions_source, look=look,
        width=settings.width, height=settings.height)
    emit(PipelineEvent(Stage.CAPTIONS, "done", Path(ass_path).name))

    # The music bed. Nothing above this line knows about it and the caller
    # normally passes nothing, so this is where a clean clone gets one:
    # whatever is in `assets/music/`, preferring a real file over the
    # generated placeholder (see engine/assembly/audio.py and
    # scripts/make_audio_assets.py). Resolved here rather than inside
    # `render` so that a direct render stays exactly as explicit as it was.
    # The reel's own bed beats the shared folder, and a caller who named
    # a path explicitly beats both -- that parameter is someone being
    # deliberate, and a stored pick must not quietly override it.
    music_credit = ""
    if music_path is None:
        music_path, music_credit = audio.resolve_bed(
            store.music_choice(plan.plan_id), settings)
    emit(PipelineEvent(Stage.RENDER, "started",
                       f"music: {Path(music_path).name}" if music_path
                       else "music: none (assets/music/ is empty)"))
    out_path = Path(settings.out_dir) / f"{plan.topic.slug}-{plan.plan_id[:8]}.mp4"
    key = f"render:{plan.plan_id}:v1"
    # Filled in by the render with what it actually composited. The Lordicon
    # credit is recorded from here rather than re-derived at publish time,
    # because by then the settings, the baked art and the code may all have
    # moved on while the MP4 has not.
    used: dict = {}
    # What the panel picked for this reel, if anything. Read once here
    # rather than inside the render, because the render takes a plan and
    # settings and has no store to ask.
    choices = store.sticker_choices(plan.plan_id)
    try:
        render(plan, settings, out_path, ass_path=ass_path,
               music_path=music_path, report=used, choices=choices,
               progress=lambda frac: emit(PipelineEvent(
                   Stage.RENDER, "info", f"{frac * 100:.0f}%")))
    except Exception as exc:
        store.record_render(plan.plan_id, key, "failed",
                            error_log=str(exc)[:2000])
        emit(PipelineEvent(Stage.RENDER, "failed", str(exc)[:300]))
        raise
    info = probe_video(out_path, settings.ffmpeg)
    store.record_render(plan.plan_id, key, "done", output_path=str(out_path),
                        duration_s=info.get("duration"),
                        # Two obligations, not one: the art credit the
                        # render reported and the credit the bed owes.
                        # Keeping only the first is how a CC-BY track
                        # ships uncredited.
                        attribution=audio.join_credits(
                            used.get("attribution"), music_credit))
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


def produce_stage(plan: ReelPlan, client, store, settings, *,
                  emit: Emit = _noop,
                  captions_source: str | None = None,
                  music_path: str | None = None) -> dict:
    """Everything after the human gate, in one uninterrupted call.

    The one-shot path, kept because a user who does not want to listen to
    ten beats and look at twenty clips should not be made to. It is
    literally the three parts back to back — there is no second copy of
    the stage order — so a reviewed run and an unreviewed one cannot drift
    apart.
    """
    voice_stage(plan, store, settings, emit=emit)
    counts = clips_stage(plan, client, store, settings, emit=emit)
    return render_stage(plan, store, settings, emit=emit,
                        captions_source=captions_source,
                        music_path=music_path, providers=counts)

"""The four-agent chain.

Each agent is one OmniRoute chat call with a strict JSON contract. They live in
one module because they change together: they share the prompt-loading
convention, the parse-and-validate path, and the ReelPlan models they emit.

The prompts in ``engine/prompts/`` carry the retention and policy rules as
enforced constraints rather than advice, and the QC scorecard re-checks the
ones that can be checked mechanically.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import ValidationError

from engine.contract import (Hook, Metadata, Provenance, Script, Topic)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class Repairable(Exception):
    """A semantic problem the model can plausibly fix if told about it.

    Distinct from a shape error, which pydantic raises. This exists so the
    single repair attempt in ``_ask`` covers both — an off-budget script is
    exactly as fixable as a mistyped field, and catching it here saves a
    ten-minute render that QC would reject on duration anyway.
    """


class AgentError(RuntimeError):
    """An agent's output did not match its contract."""

    def __init__(self, stage: str, detail: str, raw=None):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.raw = raw


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise AgentError(name, f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8")


def _claims_block(provenance: Provenance) -> str:
    if not provenance.claims:
        return "(no verified claims available — assert nothing factual)"
    return "\n".join(
        f"- [{c.confidence}] {c.text}  (source: {c.source_url})"
        for c in provenance.claims)


def _ask(client, stage: str, prompt: str, *, model: str | None = None,
         temperature: float = 0.85, parse=None):
    """One agent call, with a single repair attempt on a shape mismatch.

    Models miss the schema in small, mechanical ways — a claim id as the
    integer 1 instead of "b1", a number as "4.5s". Failing the stage outright
    throws away every call made so far in the run, so the exact validation
    error is handed back once and the model is asked to correct it. A second
    failure is real and raises.

    ``parse`` takes the decoded JSON and returns the model object, raising
    ValidationError if the shape is wrong.
    """
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(2):
        started = time.monotonic()
        print(f"[AGENT] {stage} attempt={attempt + 1}/2 model={model or 'default'} started", flush=True)
        result = client.chat(messages, model=model, want_json=True,
                             temperature=temperature)
        elapsed = time.monotonic() - started
        print(f"[AGENT] {stage} attempt={attempt + 1}/2 completed seconds={elapsed:.1f} json={result.data is not None}", flush=True)
        if result.data is None:
            problem = "model returned no JSON"
        else:
            if parse is None:
                return result, None
            try:
                return result, parse(result.data)
            except ValidationError as exc:
                problem = _explain(exc)
            except Repairable as exc:
                problem = str(exc)

        if attempt == 0:
            print(f"[AGENT] {stage} schema validation failed; starting repair attempt", flush=True)
            messages = messages + [
                {"role": "assistant",
                 "content": json.dumps(result.data)[:4000]
                 if result.data is not None else (result.text or "")[:4000]},
                {"role": "user",
                 "content": ("That did not match the required schema:\n"
                             f"{problem}\n\n"
                             "Return the SAME content again, corrected. JSON "
                             "only, no commentary. Keep every id a quoted "
                             "string and every duration a plain number.")},
            ]
            continue

        raise AgentError(stage, problem,
                         result.data if result.data is not None else result.text)


def _explain(exc: ValidationError) -> str:
    """The parts of a pydantic error a model can act on."""
    lines = []
    for error in exc.errors()[:6]:
        where = ".".join(str(p) for p in error["loc"])
        lines.append(f"- {where}: {error['msg']} "
                     f"(got {error.get('input')!r})")
    return "\n".join(lines)


def run_research(client, topic: Topic, *, model: str | None = None):
    prompt = load_prompt("research").format(topic=topic.raw)
    result, provenance = _ask(client, "research", prompt, model=model,
                              temperature=0.4,
                              parse=Provenance.model_validate)
    return provenance, result.cost


def run_hooks(client, topic: Topic, provenance: Provenance, *,
              model: str | None = None):
    prompt = load_prompt("hooks").format(
        topic=topic.raw, claims=_claims_block(provenance))
    def parse(data):
        raw = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            raise AgentError("hooks", "expected a non-empty 'hooks' list",
                             data)
        return [Hook.model_validate(h) for h in raw]

    result, hooks = _ask(client, "hooks", prompt, model=model,
                         temperature=1.0, parse=parse)
    return hooks, result.cost


def run_script(client, topic: Topic, provenance: Provenance,
               hook: Hook | None, *, model: str | None = None,
               word_target: int | None = None, beats: int | None = None,
               words_per_second: float | None = None):
    """``word_target`` is derived from the voice engine's measured rate.

    Left unset it resolves to ``target_seconds * words_per_second`` from the
    configuration, which is the same product ``plan_stage`` passes. It is
    resolved here rather than written into the signature because a literal
    default goes stale: it said 136 for a while after that product became
    103, and re-typing the new product as a literal would only move the same
    bug one rate change further out. Nothing to hand-copy, nothing to rot.

    ``beats`` is resolved the same way and for the same reason. It said 12
    for a while after the word budget above became 103, which asked the
    model for 8.6 words/beat -- it wrote 161 words instead and failed even
    after the repair retry. ``beats`` and ``word_target`` are recalibrated
    together from here on, because it is their ratio, not either number
    alone, that the model can or cannot write.

    ``words_per_second`` is the voice's measured rate, and it is in the
    signature for the same reason the other two are: the prompt now states
    it to the model. Every seconds figure in script.txt is a word count
    converted at this rate, and the per-beat word range is derived from
    ``word_target / beats`` — the three numbers the model can read cannot
    disagree with each other because there is only one of each.

    Duration follows from word count, so the prompt is told the budget
    rather than a beat range it can satisfy at any length.
    """
    if word_target is None:
        # Imported here, not at module scope: engine.config constructs
        # Settings at import time and reads .env, and the agents module is
        # imported by tools that have no business doing either.
        from engine.config import word_budget

        word_target = word_budget()
    if beats is None:
        from engine.config import beat_count

        beats = beat_count()
    if words_per_second is None:
        from engine.config import speech_rate

        words_per_second = speech_rate()

    # Every number below is derived here, from those three. The bug this
    # replaces was three literals in script.txt — "4 to 18", "4.4", "44.0" —
    # that stayed still while the budget moved.
    from engine.config import beat_word_range, words_per_beat as _per_beat

    per_beat = _per_beat(word_target, beats)
    beat_words_min, beat_words_max = beat_word_range(per_beat)
    prompt = load_prompt("script").format(
        word_target=word_target,
        beats=beats,
        words_per_beat=per_beat,
        beat_words_min=beat_words_min,
        beat_words_max=beat_words_max,
        words_per_second=f"{words_per_second:g}",
        seconds_per_beat=round(per_beat / words_per_second, 1),
        total_seconds=round(word_target / words_per_second, 1),
        topic=topic.raw,
        hook=(f"{hook.voice_text}  /  {hook.caption_text}" if hook
              else "(no hook chosen — write your own opening beat)"),
        hook_id=hook.variant_id if hook else "h1",
        claims=_claims_block(provenance))
    def parse(data):
        raw = data.get("script") if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            raise AgentError("script", "expected a 'script' object", data)

        # target_seconds is no longer asked of the model. It is a hint that
        # measured_seconds overrides everywhere downstream, and asking for
        # it invited exactly the seconds-first sizing that overshot the
        # budget: the model filled in 4.4s per beat, converted it at a
        # conversational rate, and wrote 17 words. Derived from the words
        # it actually wrote, at this voice's rate, it cannot disagree with
        # them. A stale value in a hand-written payload is overwritten for
        # the same reason.
        for beat in raw.get("beats") or []:
            if isinstance(beat, dict):
                spoken = len(str(beat.get("voice_text") or "").split())
                beat["target_seconds"] = round(
                    max(spoken, 1) / words_per_second, 2)

        script = Script.model_validate(raw)
        if not script.beats:
            raise AgentError("script", "script contained no beats", data)

        # Runtime follows word count, so an off-budget script produces an
        # out-of-range video. Catching it here costs one extra call; catching
        # it in QC costs the whole render.
        words = sum(len(b.voice_text.split()) for b in script.beats)
        drift = (words - word_target) / word_target
        if abs(drift) > 0.15:
            direction = "too long" if drift > 0 else "too short"
            raise Repairable(
                f"the script is {direction}: {words} spoken words against a "
                f"target of {word_target} ({drift * 100:+.0f}%). Rewrite it "
                f"at {word_target} words, keeping the same beats and "
                f"meaning — "
                + ("shorten the longest lines."
                   if drift > 0 else "lengthen the shortest lines."))
        return script

    result, script = _ask(client, "script", prompt, model=model,
                          temperature=0.9, parse=parse)
    return script, result.cost


def run_metadata(client, topic: Topic, script: Script,
                 provenance: Provenance, *, model: str | None = None):
    script_block = "\n".join(
        f"{b.beat_id} [{b.role}] {b.caption_text}" for b in script.beats)
    sources = "\n".join(
        sorted({c.source_url for c in provenance.claims if c.source_url}))
    prompt = load_prompt("metadata").format(
        topic=topic.raw, script=script_block,
        sources=sources or "(none)")
    def parse(data):
        payload = data
        if isinstance(payload, dict) and "metadata" in payload:
            payload = payload["metadata"]
        return Metadata.model_validate(payload)

    result, metadata = _ask(client, "metadata", prompt, model=model,
                            temperature=0.8, parse=parse)
    return metadata, result.cost


def dump_prompt(stage: str, **kwargs) -> str:
    """Render a prompt without calling anything — used by the UI's debug view."""
    return load_prompt(stage).format(**kwargs)


__all__ = ["AgentError", "load_prompt", "dump_prompt", "run_research",
           "run_hooks", "run_script", "run_metadata", "json"]

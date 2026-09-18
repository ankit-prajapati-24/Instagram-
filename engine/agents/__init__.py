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
from pathlib import Path

from pydantic import ValidationError

from engine.contract import (Hook, Metadata, Provenance, Script, Topic)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


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
        result = client.chat(messages, model=model, want_json=True,
                             temperature=temperature)
        if result.data is None:
            problem = "model returned no JSON"
        else:
            if parse is None:
                return result, None
            try:
                return result, parse(result.data)
            except ValidationError as exc:
                problem = _explain(exc)

        if attempt == 0:
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
               word_target: int = 136, beats: int = 12):
    """``word_target`` is derived from the voice engine's measured rate.

    Duration follows from word count, so the prompt is told the budget
    rather than a beat range it can satisfy at any length.
    """
    prompt = load_prompt("script").format(
        word_target=word_target,
        words_per_beat=max(round(word_target / beats), 4),
        topic=topic.raw,
        hook=(f"{hook.voice_text}  /  {hook.caption_text}" if hook
              else "(no hook chosen — write your own opening beat)"),
        hook_id=hook.variant_id if hook else "h1",
        claims=_claims_block(provenance))
    def parse(data):
        raw = data.get("script") if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            raise AgentError("script", "expected a 'script' object", data)
        script = Script.model_validate(raw)
        if not script.beats:
            raise AgentError("script", "script contained no beats", data)
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

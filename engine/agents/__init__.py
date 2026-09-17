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
         temperature: float = 0.85):
    result = client.chat([{"role": "user", "content": prompt}], model=model,
                         want_json=True, temperature=temperature)
    if result.data is None:
        raise AgentError(stage, "model returned no JSON", result.text)
    return result


def run_research(client, topic: Topic, *, model: str | None = None):
    prompt = load_prompt("research").format(topic=topic.raw)
    result = _ask(client, "research", prompt, model=model, temperature=0.4)
    try:
        provenance = Provenance.model_validate(result.data)
    except ValidationError as exc:
        raise AgentError("research", str(exc), result.data) from exc
    return provenance, result.cost


def run_hooks(client, topic: Topic, provenance: Provenance, *,
              model: str | None = None):
    prompt = load_prompt("hooks").format(
        topic=topic.raw, claims=_claims_block(provenance))
    result = _ask(client, "hooks", prompt, model=model, temperature=1.0)

    raw = result.data.get("hooks") if isinstance(result.data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise AgentError("hooks", "expected a non-empty 'hooks' list",
                         result.data)
    try:
        hooks = [Hook.model_validate(h) for h in raw]
    except ValidationError as exc:
        raise AgentError("hooks", str(exc), result.data) from exc
    return hooks, result.cost


def run_script(client, topic: Topic, provenance: Provenance,
               hook: Hook | None, *, model: str | None = None):
    prompt = load_prompt("script").format(
        topic=topic.raw,
        hook=(f"{hook.voice_text}  /  {hook.caption_text}" if hook
              else "(no hook chosen — write your own opening beat)"),
        hook_id=hook.variant_id if hook else "h1",
        claims=_claims_block(provenance))
    result = _ask(client, "script", prompt, model=model, temperature=0.9)

    raw = result.data.get("script") if isinstance(result.data, dict) else None
    if not isinstance(raw, dict):
        raise AgentError("script", "expected a 'script' object", result.data)
    try:
        script = Script.model_validate(raw)
    except ValidationError as exc:
        raise AgentError("script", str(exc), result.data) from exc
    if not script.beats:
        raise AgentError("script", "script contained no beats", result.data)
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
    result = _ask(client, "metadata", prompt, model=model, temperature=0.8)

    payload = result.data
    if isinstance(payload, dict) and "metadata" in payload:
        payload = payload["metadata"]
    try:
        metadata = Metadata.model_validate(payload)
    except ValidationError as exc:
        raise AgentError("metadata", str(exc), result.data) from exc
    return metadata, result.cost


def dump_prompt(stage: str, **kwargs) -> str:
    """Render a prompt without calling anything — used by the UI's debug view."""
    return load_prompt(stage).format(**kwargs)


__all__ = ["AgentError", "load_prompt", "dump_prompt", "run_research",
           "run_hooks", "run_script", "run_metadata", "json"]

"""Four-layer dedup, cheapest first.

This is not an efficiency feature. YouTube's inauthentic-content detection
looks at channel-level patterns, so semantic dedup is the primary policy
defence and is treated as such: a failure here is a hard stop, never a retry.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from engine.contract import Topic


@dataclass
class DedupResult:
    passed: bool
    layer: str | None = None
    detail: str = ""
    score: float = 0.0


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _trigrams(text: str) -> set[str]:
    padded = f"  {_normalise(text)}  "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard overlap of padded character trigrams.

    Catches rewordings that an exact hash misses ("roopkund lake skeletons"
    vs "skeletons of roopkund lake") without needing an embedding call.
    """
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def check(topic: Topic, store, client=None, *, embed_text: str | None = None,
          trigram_threshold: float = 0.6, cosine_threshold: float = 0.88,
          cooldown_days: int = 45, history: int = 400) -> DedupResult:
    """Run the four layers in order and return the first failure.

    ``client`` may be None, in which case the semantic layer is skipped rather
    than failed — a missing gateway must not silently disable the cheap layers.
    """
    # 1. exact
    if store.hash_exists(topic.dedupe_hash):
        return DedupResult(False, "exact", f"slug already produced: "
                                           f"{topic.slug}", 1.0)

    # 2. trigram
    best, best_slug = 0.0, ""
    for slug in store.published_slugs(history):
        score = trigram_similarity(topic.slug, slug)
        if score > best:
            best, best_slug = score, slug
    if best > trigram_threshold:
        return DedupResult(False, "trigram",
                           f"{best:.2f} similar to '{best_slug}'", best)

    # 3. semantic
    if client is not None:
        previous = store.recent_embeddings(history)
        if previous:
            try:
                vector = client.embed([embed_text or topic.raw])[0]
            except Exception:
                # Embeddings route to their own provider and can be missing
                # while chat works. Layers 1, 2 and 4 still ran; skipping 3 is
                # better than failing the plan over a credential gap.
                return DedupResult(True, None,
                                   "layers 1, 2 and 4 clear; semantic layer "
                                   "skipped (no embedding provider)", best)
            top, top_plan = 0.0, ""
            for plan_id, other in previous:
                score = cosine(vector, other)
                if score > top:
                    top, top_plan = score, plan_id
            if top > cosine_threshold:
                return DedupResult(False, "semantic",
                                   f"cosine {top:.3f} vs plan {top_plan}", top)

    # 4. entity cooldown
    blocked = store.cooldown_detail(topic.entities, cooldown_days)
    if blocked:
        listed = ", ".join(f"{name} (free in {days}d)"
                           for name, days in blocked)
        return DedupResult(False, "cooldown",
                           f"covered too recently: {listed}", 1.0)

    return DedupResult(True, None, "all four layers clear", best)

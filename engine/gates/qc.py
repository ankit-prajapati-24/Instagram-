"""QC scorecard.

A hard failure routes to the human queue. Nothing here auto-retries: every
check that can fail is semantic, and retrying a semantic failure just burns
credits producing the same reject.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from engine.contract import ReelPlan

# These are the fingerprints of an AI script in this niche. Audiences in India
# recognise them instantly, and they are also the phrasing that clusters
# templated channels together in YouTube's own pattern detection.
BANNED_PHRASES: tuple[str, ...] = (
    "aaj hum baat karenge",
    "kya aap jaante hain",
    "chaliye shuru karte hain",
    "doston",
    "aap ko jaan kar hairani hogi",
    "iske baare mein aapka kya khayal hai",
    "in today's video",
    "let's dive in",
    "little did they know",
    "the truth may shock you",
)


@dataclass
class Check:
    name: str
    ok: bool
    kind: str  # "hard" | "warn"
    detail: str = ""


@dataclass
class Scorecard:
    checks: list[Check] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[Check]:
        return [c for c in self.checks if c.kind == "hard" and not c.ok]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.kind == "warn" and not c.ok]

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "ok": c.ok, "kind": c.kind,
                 "detail": c.detail} for c in self.checks
            ],
            "hard_failures": [c.name for c in self.hard_failures],
            "warnings": [c.name for c in self.warnings],
        }


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?|।]+", text) if s.strip()]


def run_qc(plan: ReelPlan, *, similarity: float | None = None,
           duration_min: float = 38.0, duration_max: float = 52.0,
           max_silence_gap: float | None = None,
           word_alignment: float | None = None) -> Scorecard:
    checks: list[Check] = []
    beats = plan.script.beats
    duration = plan.duration()

    # --- hard ------------------------------------------------------------
    checks.append(Check(
        "duration", duration_min <= duration <= duration_max, "hard",
        f"{duration:.1f}s (need {duration_min:.0f}-{duration_max:.0f}s)"))

    missing = [c.text[:40] for c in plan.provenance.claims
               if not (c.source_url or "").strip()]
    checks.append(Check(
        "claim_provenance", not missing, "hard",
        "all claims sourced" if not missing
        else f"{len(missing)} unsourced: {missing[:2]}"))

    if similarity is not None:
        checks.append(Check(
            "semantic_similarity", similarity < 0.88, "hard",
            f"cosine {similarity:.3f} (limit 0.88)"))

    checks.append(Check(
        "moderation", plan.safety.moderation_passed, "hard",
        "passed" if plan.safety.moderation_passed
        else f"flags: {plan.safety.flags}"))

    haystack = plan.all_text().lower()
    hits = [p for p in BANNED_PHRASES if p in haystack]
    checks.append(Check(
        "banned_phrase", not hits, "hard",
        "clean" if not hits else f"found: {hits}"))

    checks.append(Check(
        "beat_count", 9 <= len(beats) <= 13, "hard",
        f"{len(beats)} beats (need 9-13)"))

    dual = [b.beat_id for b in beats
            if not b.voice_text.strip() or not b.caption_text.strip()]
    checks.append(Check(
        "dual_text", not dual, "hard",
        "every beat has voice + caption" if not dual
        else f"missing on: {dual[:3]}"))

    if max_silence_gap is not None:
        checks.append(Check(
            "silence_gap", max_silence_gap <= 1.2, "hard",
            f"largest gap {max_silence_gap:.2f}s (limit 1.2s)"))

    if word_alignment is not None:
        checks.append(Check(
            "word_alignment", word_alignment >= 0.98, "hard",
            f"{word_alignment * 100:.1f}% aligned (need 98%)"))

    # --- warn ------------------------------------------------------------
    if beats:
        rate = duration / len(beats)
        checks.append(Check(
            "visual_change_rate", rate <= 4.0, "warn",
            f"one visual every {rate:.1f}s (want <=4.0s)"))

    lengths = [len(s.split()) for s in _sentences(plan.all_text())]
    if len(lengths) >= 3:
        stdev = statistics.pstdev(lengths)
        checks.append(Check(
            "sentence_variance", stdev > 5.5, "warn",
            f"stdev {stdev:.1f} (want >5.5; low = AI-flat rhythm)"))

    if plan.metadata:
        title_ok = 0 < len(plan.metadata.yt_title) <= 100
        checks.append(Check("title_length", title_ok, "warn",
                            f"{len(plan.metadata.yt_title)} chars"))
        pinned = plan.metadata.pinned_comment.strip()
        checks.append(Check(
            "pinned_comment", len(pinned) >= 25 and "?" in pinned, "warn",
            "debate-shaped" if len(pinned) >= 25 and "?" in pinned
            else "too short or not a question"))

    return Scorecard(checks)

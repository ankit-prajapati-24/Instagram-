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
           actual_duration: float | None = None,
           narration_seconds: float | None = None,
           expect_render: bool = False) -> Scorecard:
    """Score a plan. ``actual_duration`` is the probed length of the MP4.

    Pass it whenever a render exists. The sum of beat lengths overstates the
    finished video, because every xfade overlaps its two beats — nine joins at
    0.5s each cut 4.5s off a ten-beat Reel. Checking the estimate would let a
    53s plan pass while the file it produced is 48s, or the reverse.
    """
    checks: list[Check] = []
    beats = plan.script.beats
    duration = actual_duration if actual_duration else plan.duration()
    measured = "rendered" if actual_duration else "estimated"

    # A render that exists but could not be probed must not fall back to the
    # beat sum. The sum and the file disagreed once already, and a silent
    # fallback would score a 36s video as a passing 41s "estimate".
    if expect_render and not actual_duration:
        checks.append(Check(
            "duration_probe", False, "hard",
            "a render exists but its duration could not be read from the "
            "file; refusing to score it against the beat sum"))

    # --- hard ------------------------------------------------------------
    checks.append(Check(
        "duration", duration_min <= duration <= duration_max, "hard",
        f"{duration:.1f}s {measured} "
        f"(need {duration_min:.0f}-{duration_max:.0f}s)"))

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

    # The check that matters most, and the one this pipeline got wrong once.
    #
    # The narration is a plain concat of per-beat audio; the picture is an
    # xfade chain. If the two timelines disagree, the tail of the narration is
    # truncated by the output duration and every caption after the first join
    # drifts. It renders, it has audio, it is the right resolution, and it is
    # unusable — so it needs its own hard gate rather than trusting the graph.
    if narration_seconds is not None and actual_duration:
        drift = abs(actual_duration - narration_seconds)
        checks.append(Check(
            "av_sync", drift <= 0.35, "hard",
            f"video {actual_duration:.2f}s vs narration "
            f"{narration_seconds:.2f}s (drift {drift:.2f}s, limit 0.35s)"))

    # Deliberately absent: "silence_gap" and "word_alignment" from spec 7.4.
    #
    # Caption timings are interpolated across each beat's measured span, so
    # they are contiguous and complete by construction — the gap was always
    # 0.00s and the alignment always 100%. Both were hard-fails that could not
    # fail, and they were the two checks that should have caught the A/V drift
    # above. A check that measures its own input is worse than no check,
    # because it reads as coverage.

    # --- warn ------------------------------------------------------------
    if beats:
        rate = duration / len(beats)  # same duration source as above
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

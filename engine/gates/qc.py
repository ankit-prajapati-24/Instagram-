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


# The tolerance the publishing window allows around a target duration, as a
# fraction of the target. Not a new number: it is the same +/-15% run_script
# already allows around the word budget, carried through to seconds by
# duration_window() below -- expressing it once here is what lets the window
# track target_seconds instead of standing still under it.
DURATION_TOLERANCE = 0.15


def duration_window(target_seconds: float,
                    tolerance: float = DURATION_TOLERANCE
                    ) -> tuple[float, float]:
    """The publishing window for a given target: target +/- tolerance.

    ``engine.config.Settings.duration_min``/``duration_max`` call this with
    the live ``target_seconds``, so the window a finished video is judged
    against moves when the target does. Before this function existed,
    DURATION_MIN/DURATION_MAX were the literals 38.0/52.0 -- exactly what
    45 +/-15% works out to -- and nothing recomputed them when the target
    changed: move the target to 60 and the 137-word budget it implies speaks
    for ~59.8s, outside a window that never moved.
    """
    return (target_seconds * (1 - tolerance), target_seconds * (1 + tolerance))


# This module's own default window, for callers -- mostly this file's own
# tests -- that score a plan without a Settings object to hand it. Anchored
# at 45.0, the target this pipeline shipped with before
# engine.config.Settings.target_seconds moved to 50, so this file's fixtures
# keep meaning what they were written to mean.
#
# Production code does not read these two names for the window it actually
# enforces: engine.config.Settings.duration_min/duration_max call
# duration_window() directly with the live target_seconds, so the window a
# real run is judged against always tracks the configured target, not this
# module's fixed anchor.
DURATION_MIN, DURATION_MAX = duration_window(45.0)

# How far outside that window the PRE-RENDER length gate still lets a plan
# through, as a fraction of the bound it misses.
#
# The two checks have different jobs. QC judges a finished video against what
# this channel publishes, and a 35s or a 56s Short is not publishable. The
# length gate is only there to stop the pipeline spending twelve minutes on
# clips and render for a script that is obviously wrong -- and "obviously
# wrong" is a looser question than "publishable".
#
# It has to be looser, because the two ends of the system fit with nothing
# between them. The word budget is ``target_seconds * words_per_second`` and
# the script agent may drift +/-15% around it, which at the configured rate
# maps to exactly 38.25-51.75s: a quarter of a second of slack at each end.
# But the rate is a property of the content, not of the configuration. This
# repo has measured 1.83 w/s on numeral- and acronym-heavy lines, 2.94 on
# clean prose, and 2.47 on its own sample script. At 2.47 the shortest script
# the agent accepts speaks for ~35.5s, and a gate set exactly at DURATION_MIN
# (38.25) would refuse it after paying for voice -- destroying the run
# instead of handing a human a video and a scorecard to judge.
#
# 15% is not a new number: it is the same drift ``run_script`` already allows
# on word count, carried through to seconds. It means the gate passes the
# agent's whole accepted band for any script the voice speaks between roughly
# 1.98 and 2.71 w/s -- the configured 2.29 and the sample's 2.47 included --
# and still refuses the 66.5s run it was built for, which was 28% over.
#
# Derived from the two constants above rather than written out again: a
# second pair of literals would separate from the window the first time
# either moved, which is the failure the single definition exists to prevent.
PRE_RENDER_MARGIN = 0.15


def pre_render_range(duration_min: float = DURATION_MIN,
                     duration_max: float = DURATION_MAX) -> tuple[float,
                                                                  float]:
    """The window the pre-render length gate scores narration against.

    Always the publishing window plus ``PRE_RENDER_MARGIN``, so widening or
    moving one moves the other with it.
    """
    return (duration_min * (1 - PRE_RENDER_MARGIN),
            duration_max * (1 + PRE_RENDER_MARGIN))


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


def duration_in_range(seconds: float, duration_min: float = DURATION_MIN,
                      duration_max: float = DURATION_MAX) -> bool:
    """Is a length inside the window this pipeline ships? One definition.

    ``run_qc`` asks it of the rendered file; the length gate asks it of the
    narration, before anything is rendered. Same window, same comparison.
    """
    return duration_min <= seconds <= duration_max


def run_qc(plan: ReelPlan, *, similarity: float | None = None,
           duration_min: float = DURATION_MIN,
           duration_max: float = DURATION_MAX,
           actual_duration: float | None = None,
           narration_seconds: float | None = None,
           expect_render: bool = False) -> Scorecard:
    """Score a plan. ``actual_duration`` is the probed length of the MP4.

    Pass it whenever a render exists. Not because the beat sum runs long any
    more — ``segment_lengths`` pads each segment by half an overlap on each
    side precisely so the xfade chain comes out exactly as long as the
    narration, and the pre-render length gate's whole justification is that
    the narration IS the runtime. The reason is what the sum is made of: a
    beat with no ``measured_seconds`` contributes its *target*, so before
    synthesis the estimate is a plan rather than a measurement, and after it
    the sum of per-beat probes is still not a probe of the finished
    container. The file is what ships, so the file is what gets scored.
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
        "duration", duration_in_range(duration, duration_min, duration_max),
        "hard",
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

    if plan.safety.moderation_unavailable:
        # Not a hard fail: the checker was unreachable, which is a setup gap
        # rather than a content verdict. It stays loud so a human decides.
        checks.append(Check(
            "moderation", False, "warn",
            f"NOT CHECKED - {plan.safety.moderation_unavailable}"))
    else:
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
        # Count visuals, not beats. A beat holds several stock clips now —
        # 13 beats carried 33 clips on the first real run — so dividing by
        # the beat count reported a cut every 5.1s when the picture was
        # actually changing every 2.0s, and warned about it.
        #
        # Beats are the fallback, not the measure: a plan stored before
        # clips existed has none, and renders one still per beat.
        visuals = sum(len(b.clips) for b in beats) or len(beats)
        rate = duration / visuals  # same duration source as above
        checks.append(Check(
            "visual_change_rate", rate <= 4.0, "warn",
            f"one visual every {rate:.1f}s across {visuals} visuals "
            f"(want <=4.0s)"))

    engines = {b.voice_engine for b in beats if b.voice_engine}
    if engines:
        checks.append(Check(
            "voice_engine", len(engines) == 1 and "piper" in engines, "warn",
            f"{', '.join(sorted(engines))}"
            + ("" if engines == {"piper"} else " — a fallback engine ran, so "
               "this does not sound like the voice you chose")))

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

"""Stock footage as the visual layer.

One Pexels clip per 2.5 seconds of narration, matched per beat so a clip
never straddles a beat boundary. That constraint is what keeps the A/V sync
work in ``engine/assembly/render.py`` intact: the clip layer only subdivides
a span the beat already owns.

Falls back to the still-image chain in ``images.py`` for any slot the agent
cannot fill, so a missing clip never fails a render.
"""

from __future__ import annotations

import hashlib
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from engine.contract import Beat, Clip, ReelPlan
from engine.media.images import generate_beat_image

# The agent generates one query per this many seconds. Mirrors
# VisualQueryGenerator.calculate_clip_count so the two cannot drift apart.
SECONDS_PER_CLIP = 2.5

PEXELS_LICENCE = "pexels"

# What .env.example ships with. Treating it as a key produces a 401 on
# every beat and a confusing run; it is the same thing as no key.
KEY_PLACEHOLDER = "your_pexels_api_key_here"


def unavailable_reason(settings) -> str | None:
    """Why stock footage cannot be fetched, or None if it can.

    Returned rather than raised: a run without a key should still produce a
    video from the fallback chain. It just must not look like a run that
    got footage.
    """
    key = (getattr(settings, "pexels_api_key", "") or "").strip()
    if not key or key == KEY_PLACEHOLDER:
        return ("PEXELS_API_KEY is not set, so every scene falls back to "
                "the image chain. Add it to .env — registration is free.")
    return None


def clip_count(seconds: float) -> int:
    """How many clips fill a span of narration."""
    return max(1, math.ceil(max(1e-9, float(seconds)) / SECONDS_PER_CLIP))


def slot_durations(total: float, count: int) -> list[float]:
    """Divide ``total`` into ``count`` slots that sum to it exactly.

    The remainder goes in the last slot rather than being spread. Measured
    worst-case summation error is 1.78e-15s against a 3.33e-2s frame at
    30fps, and a twelve-beat video accumulated exactly zero error; bit-exact
    equality isn't achievable anyway. The 1e-9 tolerance instead guards
    against a coarse implementation — rounding to 2 decimals, truncating, or
    quantising to whole frames — whose errors run around 1e-3.
    """
    if count < 1:
        raise ValueError(f"a beat needs at least one clip slot, got {count}")
    base = total / count
    slots = [base] * (count - 1)
    # Last slot: total minus sum of others ensures exact sum by construction.
    return slots + [total - sum(slots)]


def beat_clips(agent, beat: Beat, target_dir: str | Path) -> list[Clip]:
    """Match and download footage for one beat.

    The slot count is the timeline's contract, not a suggestion: if the
    agent returns fewer matches than slots, the missing slots still exist and
    are marked ``unfilled`` for the caller to fall back. Dropping them would
    silently shorten the beat's picture.
    """
    seconds = beat.seconds()
    count = clip_count(seconds)
    durations = slot_durations(seconds, count)

    result = agent.match(script_segment=beat.voice_text,
                         duration_seconds=seconds,
                         download=True,
                         output_dir=str(target_dir))
    matches = list(result.matches)[:count]

    clips: list[Clip] = []
    for index, duration in enumerate(durations):
        match = matches[index] if index < len(matches) else None
        if match is None or not match.download_path or match.video is None:
            clips.append(Clip(path="", query="", provider="unfilled",
                              duration=duration))
            continue
        clips.append(Clip(
            path=str(match.download_path),
            query=match.query.search_query,
            provider="pexels",
            duration=duration,
            source_url=match.video.url,
            pexels_id=match.video.id,
            author=match.video.user_name,
            licence=PEXELS_LICENCE))
    return clips


def _checksum(path: str | Path) -> str | None:
    """SHA-256 of ``path``'s bytes, or ``None`` if it can't be read.

    Mirrors ``images._checksum``, but tolerant, and deliberately so: this
    call site runs in the main thread's ``as_completed`` loop in
    ``generate_plan_clips``, *outside* the per-beat ``try/except`` in
    ``make()``. A clip's path can come from a matcher that reports a path
    without ever writing it there; raising on that here would crash the
    whole stage rather than fail one beat, which breaks the "a missing clip
    never fails a render" contract. Do not restore the raising version to
    match ``images.py`` — that sibling's call site always reads a file it
    just wrote, so it's never in this position.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]
    except OSError as exc:
        print(f"[clips] checksum skipped, unreadable: {path} ({exc})",
              flush=True)
        return None


def generate_plan_clips(plan: ReelPlan, agent, client, work_dir: str | Path,
                        store=None, *, model: str | None = None,
                        use_keyless: bool = True,
                        browser_image_api: str = "",
                        workers: int = 4,
                        reason: str | None = None,
                        progress=None) -> dict[str, int]:
    """Fill ``beat.clips`` for every beat; report provider counts.

    Beats run concurrently for the same reason images do: each is an
    independent network call that spends its time waiting.

    Any slot the agent could not fill is replaced by a still from the image
    chain, which keeps its slot duration. A fallback changes what is on
    screen, never when.

    ``reason``, when set (see ``unavailable_reason``), skips the agent
    entirely rather than let every beat fail the same known way. A rate
    limit or other per-beat failure is caught instead of retried: one
    attempt per beat, because retrying a 429 turns a slow path into a
    banned one.
    """
    target_dir = Path(work_dir) / plan.plan_id / "clips"
    target_dir.mkdir(parents=True, exist_ok=True)
    beats = plan.script.beats
    counts: dict[str, int] = {}

    def make(item):
        index, beat = item
        if reason:
            clips = [Clip(path="", query="", provider="unfilled",
                          duration=d)
                     for d in slot_durations(beat.seconds(),
                                             clip_count(beat.seconds()))]
        else:
            try:
                clips = beat_clips(agent, beat, target_dir)
            except Exception as exc:
                # One attempt per beat. A rate limit answered with retries
                # turns a slow path into a banned one.
                print(f"[clips] {beat.beat_id} fell back: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                clips = [Clip(path="", query="", provider="unfilled",
                              duration=d)
                         for d in slot_durations(beat.seconds(),
                                                 clip_count(beat.seconds()))]
        for slot, clip in enumerate(clips):
            if clip.provider != "unfilled":
                continue
            still = target_dir / f"{beat.beat_id}-{slot}.png"
            path, provider = generate_beat_image(
                client, beat, still, seed=index * 100 + slot, model=model,
                use_keyless=use_keyless,
                browser_image_api=browser_image_api)
            clips[slot] = Clip(path=path, query=beat.visual_prompt,
                               provider=provider, duration=clip.duration,
                               licence="ai-generated")
        return index, beat, clips

    done = 0
    effective_workers = 1 if browser_image_api else max(workers, 1)
    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [pool.submit(make, item) for item in enumerate(beats)]
        for future in as_completed(futures):
            index, beat, clips = future.result()
            beat.clips = clips
            for clip in clips:
                counts[clip.provider] = counts.get(clip.provider, 0) + 1
                if store is not None and clip.path:
                    store.save_asset(
                        plan.plan_id, beat.beat_id, "clip", clip.provider,
                        clip.path, source_url=clip.source_url,
                        checksum=_checksum(clip.path),
                        licence=clip.licence or "ai-generated")
            done += 1
            if progress:
                progress(done, len(beats), beat.beat_id,
                         ",".join(sorted({c.provider for c in clips})))

    return counts

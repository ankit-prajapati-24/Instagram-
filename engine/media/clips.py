"""Stock footage as the visual layer.

One Pexels clip per 2.5 seconds of narration, matched per beat so a clip
never straddles a beat boundary. That constraint is what keeps the A/V sync
work in ``engine/assembly/render.py`` intact: the clip layer only subdivides
a span the beat already owns.

Falls back to the still-image chain in ``images.py`` for any slot the agent
cannot fill, so a missing clip never fails a render.
"""

from __future__ import annotations

import math
from pathlib import Path

from engine.contract import Beat, Clip

# The agent generates one query per this many seconds. Mirrors
# VisualQueryGenerator.calculate_clip_count so the two cannot drift apart.
SECONDS_PER_CLIP = 2.5

PEXELS_LICENCE = "pexels"


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

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

# The agent generates one query per this many seconds. Mirrors
# VisualQueryGenerator.calculate_clip_count so the two cannot drift apart.
SECONDS_PER_CLIP = 2.5


def clip_count(seconds: float) -> int:
    """How many clips fill a span of narration."""
    return max(1, math.ceil(max(1e-9, float(seconds)) / SECONDS_PER_CLIP))


def slot_durations(total: float, count: int) -> list[float]:
    """Divide ``total`` into ``count`` slots that sum to it exactly.

    The remainder goes in the last slot rather than being spread, because
    the sum has to be exact: the render reads these as a timeline, and a
    rounding crumb per slot accumulates into visible drift.
    """
    if count < 1:
        raise ValueError(f"a beat needs at least one clip slot, got {count}")
    base = total / count
    # Create all slots at base size, then adjust the last one for any
    # floating-point error to ensure exact sum and non-negative remainder.
    all_slots = [base] * count
    error = total - sum(all_slots)
    return all_slots[:-1] + [all_slots[-1] + error]

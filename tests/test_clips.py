import math

import pytest

from engine.media.clips import clip_count, slot_durations


@pytest.mark.parametrize("seconds,expected", [
    (1.0, 1), (2.5, 1), (2.6, 2), (5.0, 2), (5.1, 3), (12.0, 5),
])
def test_clip_count_is_one_per_two_and_a_half_seconds(seconds, expected):
    assert clip_count(seconds) == expected


def test_clip_count_never_returns_zero():
    assert clip_count(0.0) == 1
    assert clip_count(0.1) == 1


def test_slots_sum_to_the_total_exactly():
    """Sum must be within 1e-9 seconds of the total. This guards against
    coarse implementations (rounding to 2 decimals: ~1e-3 error per slot),
    not against floating-point noise (worst case: 1.78e-15 per IEEE 754).
    One frame at 30fps is 3.33e-2 seconds—13 orders larger than float noise.
    """
    for total in (3.0, 4.7, 12.345, 0.9, 10.1, 7.3):
        for count in (1, 2, 3, 5, 7):
            slots = slot_durations(total, count)
            assert len(slots) == count
            assert sum(slots) == pytest.approx(total, abs=1e-9)


def test_slots_are_even_apart_from_the_remainder():
    slots = slot_durations(10.0, 4)
    assert slots == [2.5, 2.5, 2.5, 2.5]


def test_the_remainder_lands_in_the_last_slot():
    """Property: all slots except the last are identical to the first.
    The last slot may differ to accommodate the remainder."""
    total = 10.1
    count = 3
    slots = slot_durations(total, count)

    assert len(slots) == count
    # Property: all slots except last are identical
    assert all(s == slots[0] for s in slots[:-1]), \
        "All slots except last should be equal"
    # Sum is in tolerance (guards against coarse implementations)
    assert sum(slots) == pytest.approx(total, abs=1e-9)


def test_a_single_slot_spans_the_whole_beat():
    assert slot_durations(4.2, 1) == [4.2]


def test_accumulated_error_over_twelve_beats():
    """Verify accumulated error over a full video is negligible vs frame duration.

    One frame at 30fps is ~0.0333 seconds. Even accumulating 12 beats' worth
    of floating-point operations keeps error far below one frame.
    """
    frame_duration_30fps = 1.0 / 30.0  # ~0.0333 seconds

    beat_totals = [5.0, 3.2, 7.5, 4.1, 6.3, 2.8, 8.1, 3.9, 5.7, 4.4, 6.2, 3.6]

    total_duration = 0.0
    for beat_total in beat_totals:
        count = clip_count(beat_total)
        slots = slot_durations(beat_total, count)
        total_duration += sum(slots)

    expected_total = sum(beat_totals)
    error = abs(total_duration - expected_total)

    # Error should be far below one frame duration
    assert error < frame_duration_30fps / 1000.0, \
        f"Accumulated error {error}s must be << frame_duration {frame_duration_30fps}s"


def test_tolerance_catches_coarse_implementations():
    """Verify that deliberately coarse rounding fails the 1e-9 tolerance.

    This demonstrates that the tolerance guards against implementations that
    round/truncate slots to fewer decimals, not against IEEE 754 float noise.
    """
    total = 10.1
    count = 3

    # Coarse implementation: round each slot to 2 decimals
    base = total / count
    coarse_slots = [round(base, 2)] * count
    coarse_sum = sum(coarse_slots)

    # Coarse implementation should violate the 1e-9 tolerance
    with pytest.raises(AssertionError):
        assert coarse_sum == pytest.approx(total, abs=1e-9)


def test_zero_count_is_rejected():
    with pytest.raises(ValueError):
        slot_durations(5.0, 0)

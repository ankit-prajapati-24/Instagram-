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
    """Exact sum required: the render reads these as a timeline, and any
    rounding crumb per slot becomes visible A/V drift. Structural test:
    last slot must equal total minus sum of others."""
    for total in (3.0, 4.7, 12.345, 0.9, 10.1, 7.3):
        for count in (1, 2, 3, 5, 7):
            slots = slot_durations(total, count)
            assert len(slots) == count
            # Structural: last slot accounts for floating-point rounding.
            # Fails for naive [base]*count where all slots are identical.
            expected_last = total - sum(slots[:-1])
            assert slots[-1] == expected_last, \
                f"total={total}, count={count}, last={slots[-1]}, expected={expected_last}"


def test_slots_are_even_apart_from_the_remainder():
    slots = slot_durations(10.0, 4)
    assert slots == [2.5, 2.5, 2.5, 2.5]


def test_the_remainder_lands_in_the_last_slot():
    """Last slot must absorb floating-point rounding. Structural test that
    fails for naive [base]*count where all slots are identical."""
    total = 10.1
    count = 3
    slots = slot_durations(total, count)

    assert len(slots) == count
    # Structural: last slot must equal (total - sum of others).
    # This fails for naive implementations when float arithmetic differs.
    expected_last = total - sum(slots[:-1])
    assert slots[-1] == expected_last, \
        f"Last slot {slots[-1]} must equal {expected_last} (remainder correction)"


def test_a_single_slot_spans_the_whole_beat():
    assert slot_durations(4.2, 1) == [4.2]


def test_zero_count_is_rejected():
    with pytest.raises(ValueError):
        slot_durations(5.0, 0)

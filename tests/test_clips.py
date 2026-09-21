import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.contract import Beat
from engine.media.clips import beat_clips, clip_count, slot_durations
from tests.factories import make_plan


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


def _beat(seconds=5.0):
    beat = Beat(beat_id="b1", role="hook", voice_text="कुछ",
                caption_text="kuch", target_seconds=seconds,
                visual_prompt="p", motion="zoom_in", transition="fade")
    beat.measured_seconds = seconds
    return beat


def _match(index, path="clip.mp4", error=None, video=True):
    return SimpleNamespace(
        clip_index=index,
        query=SimpleNamespace(search_query=f"query {index}"),
        video=SimpleNamespace(id=100 + index, url="https://pexels/x",
                              user_name="Someone") if video else None,
        download_path=path,
        error=error)


def test_beat_clips_returns_one_clip_per_slot(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1)])
    clips = beat_clips(agent, _beat(5.0), tmp_path)
    assert len(clips) == 2
    assert [c.provider for c in clips] == ["pexels", "pexels"]


def test_beat_clip_durations_sum_to_the_measured_beat(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(i) for i in range(3)])
    beat = _beat(7.3)
    clips = beat_clips(agent, beat, tmp_path)
    assert sum(c.duration for c in clips) == pytest.approx(7.3, abs=1e-9)


def test_beat_clips_uses_measured_seconds_not_target(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    beat = _beat(3.0)
    beat.target_seconds = 99.0          # the model's guess, must be ignored
    beat.measured_seconds = 2.0
    beat_clips(agent, beat, tmp_path)
    assert agent.match.call_args.kwargs["duration_seconds"] == 2.0


def test_a_slot_the_agent_could_not_fill_is_marked_unfilled(tmp_path):
    """The caller falls those back to the image chain; this function only
    reports them honestly rather than dropping the slot."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1, path=None, video=False,
                                   error="no results")])
    clips = beat_clips(agent, _beat(5.0), tmp_path)
    assert len(clips) == 2
    assert clips[1].provider == "unfilled"
    assert clips[1].path == ""


def test_slot_count_wins_when_the_agent_returns_too_few(tmp_path):
    """clip_count is the timeline's contract. If the agent under-delivers
    the missing slots still exist, unfilled."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    clips = beat_clips(agent, _beat(7.5), tmp_path)   # wants 3
    assert len(clips) == 3
    assert [c.provider for c in clips] == ["pexels", "unfilled", "unfilled"]


def test_extra_matches_beyond_the_slot_count_are_discarded(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(i) for i in range(6)])
    clips = beat_clips(agent, _beat(2.4), tmp_path)   # wants 1
    assert len(clips) == 1


from engine.media.clips import generate_plan_clips


def test_generate_plan_clips_fills_every_beat(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2)

    assert all(beat.clips for beat in plan.script.beats)
    assert counts["pexels"] == len(plan.script.beats)


def test_an_unfilled_slot_falls_back_to_the_image_chain(tmp_path, monkeypatch):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0, path=None, video=False, error="nothing")])

    calls = []

    def fake_image(client, beat, out_path, **kwargs):
        calls.append(beat.beat_id)
        Path(out_path).write_bytes(b"png")
        return str(out_path), "keyless"

    monkeypatch.setattr("engine.media.clips.generate_beat_image", fake_image)

    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2)

    assert calls, "the image chain was never reached"
    assert counts.get("keyless") == len(plan.script.beats)
    assert all(c.provider == "keyless"
               for beat in plan.script.beats for c in beat.clips)


def test_every_clip_is_recorded_as_an_asset(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    store = MagicMock()
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        store=store, workers=2)

    assert store.save_asset.call_count == len(plan.script.beats)
    kwargs = store.save_asset.call_args.kwargs
    assert kwargs["licence"] == "pexels"


def test_the_slot_sum_invariant_holds_after_fallback(tmp_path, monkeypatch):
    """Falling back must not change the timeline."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1, path=None, video=False)])

    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))

    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 5.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        workers=2)

    for beat in plan.script.beats:
        assert sum(c.duration for c in beat.clips) == pytest.approx(
            beat.seconds(), abs=1e-9)


from engine.media.clips import unavailable_reason


def test_no_pexels_key_is_reported_as_a_reason():
    settings = SimpleNamespace(pexels_api_key="")
    assert "PEXELS_API_KEY" in unavailable_reason(settings)


def test_the_template_placeholder_counts_as_no_key():
    settings = SimpleNamespace(pexels_api_key="your_pexels_api_key_here")
    assert "PEXELS_API_KEY" in unavailable_reason(settings)


def test_a_real_key_has_no_reason():
    settings = SimpleNamespace(pexels_api_key="x" * 40)
    assert unavailable_reason(settings) is None


def test_the_stage_skips_the_agent_entirely_without_a_key(tmp_path,
                                                          monkeypatch):
    agent = MagicMock()
    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2,
                                 reason="PEXELS_API_KEY is not set")

    agent.match.assert_not_called()
    assert "pexels" not in counts
    assert all(sum(c.duration for c in b.clips) == pytest.approx(
        b.seconds(), abs=1e-9) for b in plan.script.beats)


def test_a_rate_limit_is_surfaced_not_retried(tmp_path, monkeypatch):
    """Pexels free tier is 200 requests/hour. Hammering it turns a slow
    path into a banned one."""
    agent = MagicMock()
    agent.match.side_effect = RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=1)

    assert agent.match.call_count == len(plan.script.beats), \
        "one attempt per beat, no retry storm"
    assert counts.get("placeholder") == len(plan.script.beats)

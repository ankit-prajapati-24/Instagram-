import hashlib
import math
import time
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


def _beat_with_id(beat_id, seconds=5.0):
    beat = Beat(beat_id=beat_id, role="hook", voice_text="कुछ",
                caption_text="kuch", target_seconds=seconds,
                visual_prompt="p", motion="zoom_in", transition="fade")
    beat.measured_seconds = seconds
    return beat


class RealNamingAgent:
    """A fake agent that reproduces stock_agent.ClipDownloader's actual
    on-disk naming instead of hiding behind a mock.

    Every other test in this file mocks ``agent.match`` and never writes a
    real file, so none of them can see two beats collide on a filename.
    The real ``ClipDownloader.download_clip`` writes
    ``clip_{clip_index:02d}.mp4`` into ``output_dir``, and ``clip_index``
    only counts up within a single ``match()`` call (one beat) — it is not
    unique across beats. This fake writes files the same way, so a test
    that calls it for two beats sharing a directory reproduces the exact
    collision a real Pexels-backed run hit: beat 2's ``clip_01.mp4``
    overwrites beat 1's.

    ``delay`` sleeps between creating the output directory and writing each
    file, widening the window for two beats' writes to interleave when run
    concurrently — it makes a would-be race observable instead of leaving
    it to timing luck.
    """

    def __init__(self, delay: float = 0.0):
        self.output_dirs_used: list[str] = []
        self.delay = delay

    def match(self, script_segment, duration_seconds, download=True,
              output_dir="output_clips"):
        self.output_dirs_used.append(output_dir)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        count = clip_count(duration_seconds)
        matches = []
        for i in range(1, count + 1):
            if self.delay:
                time.sleep(self.delay)
            # Exactly stock_agent.ClipDownloader.download_clip's naming.
            path = out / f"clip_{i:02d}.mp4"
            path.write_bytes(f"{script_segment}-{i}".encode() * 20)
            matches.append(SimpleNamespace(
                clip_index=i,
                query=SimpleNamespace(search_query=f"query {i}"),
                video=SimpleNamespace(id=i, url="https://pexels/x",
                                      user_name="Someone"),
                download_path=str(path),
                error=None))
        return SimpleNamespace(matches=matches)


def test_two_beats_do_not_collide_on_the_real_downloader_filenames(
        tmp_path):
    """Reproduces the bug proven from the first real run's database: two
    beats sharing one clips directory both write clip_01.mp4/clip_02.mp4,
    so the second beat's download silently clobbers the first's, and both
    Beat.clips end up pointing at the same two files.

    Uses RealNamingAgent, not a MagicMock, because a mocked agent.match
    never writes a file and so can never exhibit this collision.
    """
    agent = RealNamingAgent()
    target_dir = tmp_path / "p1" / "clips"   # the one dir the whole plan gets

    beat1 = _beat_with_id("b1", seconds=5.0)   # 2 slots
    beat2 = _beat_with_id("b2", seconds=5.0)   # 2 slots

    clips1 = beat_clips(agent, beat1, target_dir)
    clips2 = beat_clips(agent, beat2, target_dir)

    paths1 = [c.path for c in clips1]
    paths2 = [c.path for c in clips2]

    assert set(paths1).isdisjoint(paths2), (
        f"beat1 and beat2 point at the same clip file(s): "
        f"{set(paths1) & set(paths2)}")

    # Every file either beat's Clip claims to own must still exist and be
    # non-empty after both beats have run — not overwritten by the other.
    for beat_id, paths in (("b1", paths1), ("b2", paths2)):
        for path in paths:
            assert Path(path).exists(), (
                f"{beat_id}'s clip {path} is missing after both beats ran")
            assert Path(path).stat().st_size > 0


def test_concurrent_beats_get_distinct_and_present_clip_files(tmp_path):
    """The concurrency angle: generate_plan_clips runs beats through a
    ThreadPoolExecutor with workers > 1, which turns a shared directory
    from a plain naming collision into a write race — one beat's file can
    be read by another beat's Clip while it is still being written.

    A small delay in RealNamingAgent widens the interleaving window so
    concurrent beats are genuinely overlapping rather than accidentally
    serialized by the GIL.
    """
    agent = RealNamingAgent(delay=0.01)
    plan = make_plan(beats=6)
    for beat in plan.script.beats:
        beat.measured_seconds = 5.0   # 2 clip slots per beat

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        workers=4)

    all_paths: list[str] = []
    for beat in plan.script.beats:
        paths = [c.path for c in beat.clips if c.provider == "pexels"]
        assert len(paths) == 2, f"{beat.beat_id} missing clips: {paths}"
        for path in paths:
            assert Path(path).exists(), (
                f"{beat.beat_id}'s clip {path} missing after the run")
            assert Path(path).stat().st_size > 0
        all_paths.extend(paths)

    duplicates = [p for p in set(all_paths) if all_paths.count(p) > 1]
    assert not duplicates, (
        f"beats ended up sharing clip file(s), so at least two beats would "
        f"render the same footage: {duplicates}")
    assert len(all_paths) == len(set(all_paths))


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
    """The happy path: a clip whose file genuinely landed on disk gets a
    real checksum recorded, not just a call to save_asset."""
    clip_bytes = b"fake downloaded mp4 bytes"
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(clip_bytes)
    expected_checksum = hashlib.sha256(clip_bytes).hexdigest()[:32]

    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0, path=str(clip_path))])
    store = MagicMock()
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        store=store, workers=2)

    assert store.save_asset.call_count == len(plan.script.beats)
    kwargs = store.save_asset.call_args.kwargs
    assert kwargs["licence"] == "pexels"
    assert kwargs["checksum"] == expected_checksum
    assert len(kwargs["checksum"]) == 32
    assert all(c in "0123456789abcdef" for c in kwargs["checksum"])


def test_a_clip_whose_file_never_landed_is_recorded_with_no_checksum(
        tmp_path):
    """A matcher can report a download_path without the file actually
    being there. That must not crash the stage (checksum runs outside the
    per-beat try/except), and the asset record should say honestly that it
    couldn't be hashed rather than fabricate one."""
    missing_path = tmp_path / "never-landed.mp4"
    assert not missing_path.exists()

    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0, path=str(missing_path))])
    store = MagicMock()
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        store=store, workers=2)

    assert store.save_asset.call_count == len(plan.script.beats)
    kwargs = store.save_asset.call_args.kwargs
    assert kwargs["checksum"] is None


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

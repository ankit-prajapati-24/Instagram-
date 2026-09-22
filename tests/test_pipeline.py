from engine.pipeline import Stage


def test_clips_run_after_voice_because_they_need_measured_duration():
    """Clip count is a function of beat length, and beat length only exists
    once synthesis has written measured_seconds."""
    order = list(Stage.ORDER)
    assert order.index(Stage.VOICE) < order.index(Stage.CLIPS)


def test_the_images_stage_is_gone():
    assert not hasattr(Stage, "IMAGES")
    assert "images" not in Stage.ORDER


def test_produce_order_starts_with_voice():
    assert Stage.PRODUCE[0] == Stage.VOICE


# --- the length gate: measure before spending -------------------------------
# VOICE -> CLIPS -> CAPTIONS -> RENDER -> QC put every expensive stage before
# the first check of how long the narration actually is. The failing run spent
# 821 seconds -- roughly twelve minutes of it on clips and render -- to learn
# that the script was 66.5s against a 38-52s window. Voice is where the true
# duration becomes knowable, so that is where the question gets asked.

import pytest

from engine.config import Settings
from engine.contract import Clip
from engine.gates import qc
from engine.pipeline import GateError, PipelineEvent, produce_stage
from engine.store import Store
from tests.factories import make_plan


class _Clips:
    """Stands in for generate_plan_clips and remembers whether it ran."""

    def __init__(self):
        self.calls = 0

    def __call__(self, plan, *args, **kwargs):
        self.calls += 1
        for beat in plan.script.beats:
            beat.clips = [Clip(path="c.mp4", query="q", provider="pexels",
                               duration=beat.seconds())]
        return {"pexels": len(plan.script.beats)}


def _harness(tmp_path, monkeypatch, *, seconds_per_beat):
    """produce_stage with everything after voice stubbed out."""
    settings = Settings()
    settings.work_dir = tmp_path / "work"
    settings.out_dir = tmp_path / "out"
    settings.music_dir = tmp_path / "music"
    settings.piper_models_dir = tmp_path / "models"

    store = Store(tmp_path / "engine.db")
    store.init()

    def fake_synth(plan, work_dir, settings_, progress=None):
        for beat in plan.script.beats:
            beat.measured_seconds = seconds_per_beat
            beat.voice_engine = "piper"
            beat.audio_path = "a.mp3"

    clips = _Clips()
    monkeypatch.setattr("engine.pipeline.synth_plan", fake_synth)
    monkeypatch.setattr("engine.pipeline.generate_plan_clips", clips)
    monkeypatch.setattr("engine.pipeline.unavailable_reason", lambda s: None)
    monkeypatch.setattr("engine.pipeline.StockVideoMatcherAgent",
                        lambda **kwargs: object())
    monkeypatch.setattr("engine.pipeline.write_ass",
                        lambda *a, **k: str(tmp_path / "captions.ass"))
    monkeypatch.setattr("engine.pipeline.render", lambda *a, **k: None)
    monkeypatch.setattr(
        "engine.pipeline.probe_video",
        lambda path, ffmpeg: {"duration": len(plan.script.beats)
                              * seconds_per_beat, "bytes": 2048})

    plan = make_plan(beats=13, measured=None)
    events: list[PipelineEvent] = []
    return plan, store, settings, clips, events


def test_an_overlong_narration_stops_before_the_clip_stage(tmp_path,
                                                           monkeypatch):
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=5.1)     # 13 x 5.1 = 66.3s

    with pytest.raises(GateError) as caught:
        produce_stage(plan, None, store, settings, emit=events.append)

    assert clips.calls == 0, "the clip stage ran anyway"
    detail = caught.value.detail
    assert "66.3" in detail                  # what was measured
    assert "42" in detail and "57" in detail  # the window it missed
    assert "shorten" in detail.lower()        # what to do about it


def test_the_length_gate_reports_itself_to_the_panel(tmp_path, monkeypatch):
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=5.1)

    with pytest.raises(GateError):
        produce_stage(plan, None, store, settings, emit=events.append)

    failed = [e for e in events if e.status == "failed"]
    assert [e.stage for e in failed] == [Stage.LENGTH]
    assert "66.3" in failed[0].detail


def test_a_short_narration_is_caught_by_the_same_gate(tmp_path, monkeypatch):
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=2.0)     # 26.0s

    with pytest.raises(GateError) as caught:
        produce_stage(plan, None, store, settings, emit=events.append)

    assert clips.calls == 0
    assert "26.0" in caught.value.detail
    assert "lengthen" in caught.value.detail.lower()


def test_a_plan_inside_the_window_passes_straight_through(tmp_path,
                                                          monkeypatch):
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=3.5)     # 45.5s

    result = produce_stage(plan, None, store, settings, emit=events.append)

    assert clips.calls == 1
    assert result["scorecard"]["passed"], result["scorecard"]["hard_failures"]
    assert not [e for e in events if e.status == "failed"]


def test_the_gate_uses_the_window_it_is_given_not_a_literal(tmp_path,
                                                            monkeypatch):
    """The same 66.3s narration passes once the configured window moves."""
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=5.1)
    settings.duration_min, settings.duration_max = 60.0, 70.0

    produce_stage(plan, None, store, settings, emit=events.append)

    assert clips.calls == 1


def test_the_gate_and_the_scorecard_cannot_drift_apart():
    """One formula, one definition. Two copies of the window's arithmetic
    would separate the first time the target moved, and a pre-render gate
    that disagrees with the check it fronts is worse than no gate at all.

    ``qc.DURATION_MIN``/``qc.DURATION_MAX`` are this module's own default
    window -- for callers that score a plan with no ``Settings`` to hand
    it -- anchored at the 45s target this pipeline shipped with before
    ``target_seconds`` moved to 50. Production's window lives on
    ``Settings.duration_min``/``duration_max``, computed by the very same
    ``duration_window()`` at whatever ``target_seconds`` is actually
    configured, so it moves with the target instead of standing still
    under it.
    """
    import inspect

    defaults = inspect.signature(qc.run_qc).parameters
    assert defaults["duration_min"].default == qc.DURATION_MIN
    assert defaults["duration_max"].default == qc.DURATION_MAX

    settings = Settings()
    assert (settings.duration_min, settings.duration_max) == \
        qc.duration_window(settings.target_seconds)


def test_the_pre_render_band_is_derived_from_the_publishing_window():
    """The gate is wider than QC on purpose, and derived from it anyway.

    A second pair of literals would drift the first time either moved. A
    margin applied to the same two constants cannot.
    """
    margin = qc.PRE_RENDER_MARGIN
    assert 0 < margin <= 0.25, "an undocumented gate is not a gate"

    low, high = qc.pre_render_range()
    assert low == qc.DURATION_MIN * (1 - margin)
    assert high == qc.DURATION_MAX * (1 + margin)
    assert low < qc.DURATION_MIN and high > qc.DURATION_MAX

    # It follows whatever window it is handed, so the two cannot separate.
    assert qc.pre_render_range(60.0, 70.0) == (60.0 * (1 - margin),
                                               70.0 * (1 + margin))


def test_the_pre_render_gate_stays_wider_at_every_target():
    """The gate must widen whatever window ``duration_window()`` hands it,
    at any plausible target -- not just the one qc.py's own module-level
    defaults happen to be anchored at."""
    for target in (45.0, 50.0, 60.0):
        low, high = qc.duration_window(target)
        gate_low, gate_high = qc.pre_render_range(low, high)
        assert gate_low < low < high < gate_high, (
            f"at target={target}, the pre-render gate ({gate_low:.1f}-"
            f"{gate_high:.1f}) is not wider than QC's own window "
            f"({low:.1f}-{high:.1f})")


def test_a_narration_just_outside_the_window_still_reaches_the_render(
        tmp_path, monkeypatch):
    """39.0s is a QC failure, not an obviously wrong script.

    At the sample script's measured 2.47 w/s the shortest script the agent
    accepts speaks for about this long. Refusing it here throws the run away
    before anything exists to look at; letting it through costs twelve
    minutes and hands a human a video and a scorecard.
    """
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=3.0)    # 13 x 3.0 = 39.0s

    result = produce_stage(plan, None, store, settings, emit=events.append)

    assert clips.calls == 1
    assert Stage.LENGTH not in [e.stage for e in events
                                if e.status == "failed"]
    # QC is where a near-miss is judged, on the finished file, by name.
    assert result["scorecard"]["hard_failures"] == ["duration"]


def test_the_gate_refusal_names_both_bands(tmp_path, monkeypatch):
    """The reader needs the publishing window to know why 60s is the limit."""
    plan, store, settings, clips, events = _harness(
        tmp_path, monkeypatch, seconds_per_beat=5.1)    # 66.3s

    with pytest.raises(GateError) as caught:
        produce_stage(plan, None, store, settings, emit=events.append)

    detail = caught.value.detail
    assert "42" in detail and "57" in detail        # what QC publishes
    assert "66" in detail                           # what the gate accepts
    assert "15%" in detail                          # and the margin between


def test_the_length_stage_runs_between_voice_and_clips():
    order = list(Stage.ORDER)
    assert order.index(Stage.VOICE) < order.index(Stage.LENGTH)
    assert order.index(Stage.LENGTH) < order.index(Stage.CLIPS)

from engine.contract import Clip
from engine.gates.qc import BANNED_PHRASES, run_qc
from tests.factories import make_plan


def test_good_plan_passes():
    sc = run_qc(make_plan())
    assert sc.passed, sc.to_dict()["hard_failures"]


def test_short_plan_fails_duration():
    sc = run_qc(make_plan(beats=9, measured=1.0))
    assert not sc.passed
    assert "duration" in sc.to_dict()["hard_failures"]


def test_overlong_plan_fails_duration():
    sc = run_qc(make_plan(beats=10, measured=8.0))
    assert "duration" in sc.to_dict()["hard_failures"]


def test_too_few_beats_fails():
    sc = run_qc(make_plan(beats=4, measured=11.0))
    assert "beat_count" in sc.to_dict()["hard_failures"]


def test_banned_phrase_is_hard_failure():
    plan = make_plan()
    plan.script.beats[0].caption_text = "Doston aaj hum baat karenge"
    assert "banned_phrase" in run_qc(plan).to_dict()["hard_failures"]


def test_unsourced_claim_is_hard_failure():
    sc = run_qc(make_plan(sourced=False))
    assert "claim_provenance" in sc.to_dict()["hard_failures"]


def test_failed_moderation_is_hard_failure():
    sc = run_qc(make_plan(moderated=False))
    assert "moderation" in sc.to_dict()["hard_failures"]


def test_high_similarity_is_hard_failure():
    sc = run_qc(make_plan(), similarity=0.91)
    assert "semantic_similarity" in sc.to_dict()["hard_failures"]


def test_acceptable_similarity_passes():
    assert run_qc(make_plan(), similarity=0.42).passed


def test_missing_dual_text_is_hard_failure():
    plan = make_plan()
    plan.script.beats[3].caption_text = "   "
    assert "dual_text" in run_qc(plan).to_dict()["hard_failures"]


def test_self_measuring_checks_are_gone():
    """silence_gap and word_alignment measured their own interpolated input.

    Caption timings are spread across each beat's measured span, so they are
    contiguous and complete by construction: the gap was always 0.00s and the
    alignment always 100%. Two hard-fails that could not fail, standing in for
    the A/V sync check below that actually catches drift.
    """
    names = {c["name"] for c in run_qc(make_plan()).to_dict()["checks"]}
    assert "silence_gap" not in names
    assert "word_alignment" not in names


def test_av_sync_catches_a_truncated_narration():
    plan = make_plan(beats=10, measured=5.0)   # 50s of voice
    sc = run_qc(plan, actual_duration=45.5, narration_seconds=50.0)
    assert not sc.passed
    assert "av_sync" in sc.to_dict()["hard_failures"]
    detail = next(c.detail for c in sc.checks if c.name == "av_sync")
    assert "drift 4.50s" in detail


def test_av_sync_passes_when_the_timelines_agree():
    plan = make_plan(beats=10, measured=4.4)
    assert run_qc(plan, actual_duration=44.0,
                  narration_seconds=44.0).passed


def test_av_sync_tolerates_sub_frame_rounding():
    plan = make_plan(beats=10, measured=4.4)
    assert run_qc(plan, actual_duration=44.03,
                  narration_seconds=44.0).passed


def test_av_sync_is_skipped_without_a_render():
    names = {c["name"] for c in
             run_qc(make_plan(), narration_seconds=44.0).to_dict()["checks"]}
    assert "av_sync" not in names


def test_scorecard_serialises_for_the_ui():
    d = run_qc(make_plan()).to_dict()
    assert set(d) == {"passed", "checks", "hard_failures", "warnings"}
    assert all({"name", "ok", "kind", "detail"} == set(c) for c in d["checks"])


def test_banned_list_covers_hinglish_and_english():
    assert "aaj hum baat karenge" in BANNED_PHRASES
    assert "let's dive in" in BANNED_PHRASES


# --- rendered vs estimated duration ----------------------------------------
# The beat sum overstates the finished video: every xfade overlaps its two
# beats, so nine joins at 0.5s remove 4.5s from a ten-beat Reel.

def test_rendered_duration_overrides_the_beat_sum():
    plan = make_plan(beats=10, measured=5.4)      # 54s of beats
    assert run_qc(plan).to_dict()["hard_failures"] == ["duration"]

    # The file it actually produced is 49.5s, which is in range.
    passed = run_qc(plan, actual_duration=49.5)
    assert passed.passed
    assert "49.5s rendered" in next(
        c.detail for c in passed.checks if c.name == "duration")


def test_estimate_is_labelled_when_no_render_exists():
    detail = next(c.detail for c in run_qc(make_plan()).checks
                  if c.name == "duration")
    assert "estimated" in detail


def test_a_short_render_fails_even_when_beats_look_fine():
    plan = make_plan(beats=10, measured=4.4)      # 44s of beats, in range
    assert run_qc(plan).passed
    assert not run_qc(plan, actual_duration=31.0).passed


# --- one beat is no longer one visual --------------------------------------
# The clip stage puts several stock clips inside a single beat: the failing
# run had 13 beats and 33 clips. Dividing the duration by the beat count
# reported a visual every 5.1s and warned, when the picture was actually
# cutting every 2.0s.

def test_visual_change_rate_counts_clips_not_beats():
    plan = make_plan(beats=13, measured=5.1, clips=True)
    visuals = sum(len(b.clips) for b in plan.script.beats)
    assert visuals > len(plan.script.beats), "fixture must be multi-clip"

    check = next(c for c in run_qc(plan).checks
                 if c.name == "visual_change_rate")

    expected = plan.duration() / visuals
    assert expected <= 4.0
    assert check.ok, check.detail
    assert f"{expected:.1f}s" in check.detail


def test_visual_change_rate_falls_back_to_beats_without_clips():
    """Plans stored before clips existed carry no clips at all."""
    plan = make_plan(beats=10, measured=5.4)
    assert not any(b.clips for b in plan.script.beats)

    check = next(c for c in run_qc(plan).checks
                 if c.name == "visual_change_rate")
    assert not check.ok
    assert "5.4s" in check.detail


def test_visual_change_rate_still_warns_when_the_cuts_are_slow():
    """The check must remain able to fail once it counts clips."""
    plan = make_plan(beats=10, measured=5.0)
    for beat in plan.script.beats:                 # one long clip per beat
        beat.clips = [Clip(path="c.mp4", query="q", provider="pexels",
                           duration=beat.seconds())]

    check = next(c for c in run_qc(plan).checks
                 if c.name == "visual_change_rate")
    assert not check.ok
    assert "5.0s" in check.detail


def test_a_mixed_beat_counts_every_clip_it_holds():
    """Beats do not all hold the same number of clips."""
    plan = make_plan(beats=10, measured=4.4)
    plan.script.beats[0].clips = [
        Clip(path=f"a{i}.mp4", query="q", provider="pexels", duration=1.1)
        for i in range(4)]
    for beat in plan.script.beats[1:]:
        beat.clips = [Clip(path="b.mp4", query="q", provider="pexels",
                           duration=beat.seconds())]

    check = next(c for c in run_qc(plan).checks
                 if c.name == "visual_change_rate")
    assert f"{plan.duration() / 13:.1f}s" in check.detail


def _check(scorecard, name):
    return next(c for c in scorecard.to_dict()["checks"] if c["name"] == name)


def test_a_fallback_voice_engine_is_flagged():
    """edge-tts standing in for a broken Piper does not sound like the
    voice that was chosen, and the run has to say so."""
    plan = make_plan()
    plan.script.beats[0].voice_engine = "edge"
    assert not _check(run_qc(plan), "voice_engine")["ok"]


def test_narration_the_user_uploaded_is_not_a_fallback():
    """This check used to pass only on exactly {"piper"}, which made
    narration a human chose to record indistinguishable from a silent
    fallback — the same mistake the publish checklist made about
    hand-picked clips."""
    plan = make_plan()
    plan.script.beats[0].voice_engine = "upload"

    check = _check(run_qc(plan), "voice_engine")
    assert check["ok"]
    assert "you supplied" in check["detail"]
    assert "fallback" not in check["detail"]


def test_a_fallback_is_still_flagged_when_uploads_are_present():
    plan = make_plan()
    plan.script.beats[0].voice_engine = "upload"
    plan.script.beats[1].voice_engine = "edge"
    assert not _check(run_qc(plan), "voice_engine")["ok"]

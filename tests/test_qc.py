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


def test_optional_checks_are_skipped_when_not_supplied():
    names = {c["name"] for c in run_qc(make_plan()).to_dict()["checks"]}
    assert "silence_gap" not in names
    assert "word_alignment" not in names


def test_optional_checks_fire_when_supplied():
    sc = run_qc(make_plan(), max_silence_gap=1.9, word_alignment=0.5)
    hard = sc.to_dict()["hard_failures"]
    assert "silence_gap" in hard
    assert "word_alignment" in hard


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

import pytest
from pydantic import ValidationError

from engine.contract import Beat, Hook, ReelPlan, Script, Topic


def test_topic_make_normalises_slug_and_hashes():
    t = Topic.make("  Roopkund Lake ke 800 SAAL purane Kankaal!! ")
    assert t.slug == "roopkund-lake-ke-800-saal-purane-kankaal"
    assert len(t.dedupe_hash) == 64
    assert Topic.make(
        "roopkund lake ke 800 saal purane kankaal").dedupe_hash == t.dedupe_hash


def test_topic_slug_handles_devanagari():
    t = Topic.make("रूपकुंड झील के कंकाल")
    assert t.slug
    assert " " not in t.slug


def test_beat_rejects_unknown_motion():
    with pytest.raises(ValidationError):
        Beat(beat_id="b1", role="hook", voice_text="a", caption_text="a",
             on_screen_text=None, target_seconds=3.0, visual_prompt="p",
             motion="spin", transition="fade")


def test_beat_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Beat(beat_id="b1", role="intro", voice_text="a", caption_text="a",
             on_screen_text=None, target_seconds=3.0, visual_prompt="p",
             motion="zoom_in", transition="fade")


def _beat(i, target, measured=None):
    return Beat(beat_id=f"b{i}", role="setup", voice_text="v",
                caption_text="c", on_screen_text=None,
                target_seconds=target, visual_prompt="p",
                motion="zoom_in", transition="fade",
                measured_seconds=measured)


def _plan(beats):
    hook = Hook(variant_id="h1", voice_text="v", caption_text="c",
                style="question", seconds=3.0)
    return ReelPlan(plan_id="p1", topic=Topic.make("x"), hooks=[hook],
                    script=Script(total_seconds=45.0, chosen_hook="h1",
                                  beats=beats))


def test_duration_prefers_measured_over_target():
    plan = _plan([_beat(1, 5.0, 4.0), _beat(2, 5.0, 6.5)])
    assert plan.duration() == pytest.approx(10.5)


def test_duration_falls_back_per_beat_not_all_or_nothing():
    """A half-synthesised plan reports the real seconds it already knows.

    The captions builder accumulates per-beat offsets from the same values, so
    an all-or-nothing rule here would desync captions from audio mid-render.
    """
    plan = _plan([_beat(1, 5.0, 4.0), _beat(2, 5.0, None)])
    assert plan.duration() == pytest.approx(9.0)
    assert plan.script.beats[0].seconds() == pytest.approx(4.0)
    assert plan.script.beats[1].seconds() == pytest.approx(5.0)


def test_defaults_are_populated():
    plan = _plan([_beat(1, 5.0)])
    assert plan.schema_version == "1.0"
    assert plan.metadata is None
    assert plan.provenance.claims == []
    assert plan.safety.moderation_passed is False
    assert plan.cost.usd == 0.0


def test_plan_survives_json_roundtrip():
    plan = _plan([_beat(1, 5.0, 4.0)])
    again = ReelPlan.model_validate_json(plan.model_dump_json())
    assert again.script.beats[0].measured_seconds == pytest.approx(4.0)
    assert again.topic.dedupe_hash == plan.topic.dedupe_hash


# --- absorbing the type near-misses models actually make --------------------
# Reported from the panel: research returned beat_id as the integer 1 and the
# whole run died with "Input should be a valid string". Losing four agent
# calls over 1 vs "1" is not a defensible trade.

def test_claim_accepts_an_integer_beat_id():
    from engine.contract import Claim
    claim = Claim(beat_id=1, text="x", source_url="https://e.org")
    assert claim.beat_id == "1"


def test_hook_and_beat_accept_integer_ids():
    from engine.contract import Hook
    hook = Hook(variant_id=2, voice_text="v", caption_text="c",
                style="question")
    assert hook.variant_id == "2"

    beat = Beat(beat_id=3, role="hook", voice_text="v", caption_text="c",
                target_seconds=4.0, visual_prompt="p", motion="zoom_in",
                transition="fade")
    assert beat.beat_id == "3"


def test_durations_accept_a_string_number():
    beat = Beat(beat_id="b1", role="hook", voice_text="v", caption_text="c",
                target_seconds="4.5", visual_prompt="p", motion="zoom_in",
                transition="fade")
    assert beat.target_seconds == pytest.approx(4.5)


def test_durations_accept_a_seconds_suffix():
    beat = Beat(beat_id="b1", role="hook", voice_text="v", caption_text="c",
                target_seconds="3.2s", visual_prompt="p", motion="zoom_in",
                transition="fade")
    assert beat.target_seconds == pytest.approx(3.2)


def test_coercion_does_not_loosen_the_shape():
    """Only convertible scalars are absorbed; wrong shapes still fail."""
    with pytest.raises(ValidationError):
        Beat(beat_id="b1", role="not-a-role", voice_text="v",
             caption_text="c", target_seconds=4.0, visual_prompt="p",
             motion="zoom_in", transition="fade")
    with pytest.raises(ValidationError):
        Beat(beat_id="b1", role="hook", voice_text="v", caption_text="c",
             target_seconds="not a number", visual_prompt="p",
             motion="zoom_in", transition="fade")


def test_none_stays_none():
    from engine.contract import Claim
    assert Claim(text="x", source_url=None).beat_id is None

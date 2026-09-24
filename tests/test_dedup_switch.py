"""Turning the dedup gate off, and being able to tell that it is off.

The gate refuses a topic already produced, or one too close to it. That
is the right default for a channel and the wrong one for building: the
same topic gets run a dozen times while the pipeline is being worked on,
and a gate that blocks every one of them is only in the way.

So it is off unless ``RAHASYA_DEDUP`` says otherwise, and the thing this
file is really guarding is that **off does not look like clean**. A
disabled gate reporting "all four layers clear" would be a lie the panel
repeats, and this codebase has already shipped that shape of bug twice —
a silent edge-tts fallback and a publish checklist counting hand-picked
clips as fallbacks. A gate that is not running says so.
"""

from __future__ import annotations

import pytest

from engine.config import Settings
from engine.gates import dedup
from engine.pipeline import GateError, PipelineEvent, plan_stage
from engine.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def _settings(tmp_path, *, on):
    s = Settings()
    s.db_path = tmp_path / "t.db"
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.dedup_enabled = on
    return s


def _run(topic, store, settings):
    from engine.fake_client import FakeOmniRoute

    events: list[PipelineEvent] = []
    client = FakeOmniRoute()
    try:
        plan = plan_stage(topic, client, store, settings,
                          emit=events.append)
    finally:
        client.close()
    return plan, events


def _dedup_events(events):
    return [e for e in events if e.stage == "dedup"]


# --- the switch -------------------------------------------------------------


def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("RAHASYA_DEDUP", raising=False)
    assert Settings().dedup_enabled is False


def test_the_env_var_turns_it_on(monkeypatch):
    monkeypatch.setenv("RAHASYA_DEDUP", "1")
    assert Settings().dedup_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_the_usual_spellings_of_off_are_all_off(monkeypatch, value):
    monkeypatch.setenv("RAHASYA_DEDUP", value)
    assert Settings().dedup_enabled is False


# --- what it does to a run --------------------------------------------------


def test_the_same_topic_runs_twice_with_the_gate_off(store, tmp_path):
    """The whole point: building means running one topic over and over."""
    settings = _settings(tmp_path, on=False)

    first, _ = _run("Kuldhara, the village that emptied", store, settings)
    second, _ = _run("Kuldhara, the village that emptied", store, settings)

    assert first.plan_id != second.plan_id
    assert first.topic.dedupe_hash == second.topic.dedupe_hash


def test_the_same_topic_is_still_refused_with_the_gate_on(store, tmp_path):
    """A drafted plan is not a produced one.

    An earlier version of this test drafted the topic once and expected
    the second run to be refused. It is not, and that is deliberate:
    ``hash_exists`` counts only COUNTED_STATUSES, so a plan nobody
    approved never blocks a retry. The topic has to reach "approved" for
    the exact layer to have anything to find.
    """
    settings = _settings(tmp_path, on=True)
    plan, _ = _run("Kuldhara, the village that emptied", store, settings)
    store.save_plan(plan, status="approved")

    with pytest.raises(GateError) as caught:
        _run("Kuldhara, the village that emptied", store, settings)

    assert "dedup" in str(caught.value).lower()


def test_a_drafted_topic_does_not_block_a_retry_even_with_the_gate_on(
        store, tmp_path):
    """The counterpart, pinned so the statuses cannot quietly widen: a
    plan that failed moderation must stay retryable."""
    settings = _settings(tmp_path, on=True)
    _run("Bhangarh fort ka farmaan", store, settings)

    second, _ = _run("Bhangarh fort ka farmaan", store, settings)
    assert second.plan_id


def test_an_off_gate_says_so_rather_than_reporting_itself_clean(store,
                                                                tmp_path):
    """A disabled gate reporting "all four layers clear" is a lie the
    panel then repeats."""
    _plan, events = _run("Kuldhara", store, _settings(tmp_path, on=False))

    rows = _dedup_events(events)
    assert rows, "the stage vanished entirely instead of reporting"
    said = " ".join(e.detail.lower() for e in rows)
    assert "off" in said or "disabled" in said
    assert "clear" not in said
    assert all(e.status != "done" for e in rows), \
        "an off gate reported a pass"


def test_an_on_gate_still_reports_a_pass_normally(store, tmp_path):
    _plan, events = _run("Kuldhara", store, _settings(tmp_path, on=True))

    rows = _dedup_events(events)
    assert any(e.status == "done" for e in rows)


# --- the layers themselves are untouched ------------------------------------


def test_the_switch_decides_whether_the_gate_runs_not_what_it_decides(
        store, tmp_path):
    """``check`` itself is untouched.

    Called directly it still refuses an exact duplicate, whatever the
    setting says. The switch lives in the pipeline, so turning it off
    cannot quietly soften the layers for the runs that do use them.
    """
    from engine.contract import Topic

    settings = _settings(tmp_path, on=False)
    plan, _ = _run("Kuldhara, the village that emptied", store, settings)
    store.save_plan(plan, status="approved")

    verdict = dedup.check(Topic.make("Kuldhara, the village that emptied"),
                          store, None)

    assert verdict.passed is False
    assert verdict.layer == "exact"

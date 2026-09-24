"""The shot each slot was asked for, kept instead of thrown away.

``VisualQueryGenerator`` already returns a ``shot_intent`` beside every
``search_query`` -- one per clip, describing what that clip should show.
``Clip`` had nowhere to put it, so ``beat_clips`` read the query off the
match and dropped the intent on the floor.

The consequence is visible on the review board. Every card of a beat
showed ``beat.visual_prompt``, which is one field for the whole beat, so
three slots of "kamare me ek kursi, aur kursi par mera naam" all claimed
to be asking for the same picture. They were not: the agent asked for a
chair, then a name. The card could not say so because nothing stored it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.contract import Clip
from engine.media.clips import beat_clips
from tests.factories import make_plan


class _Match:
    def __init__(self, query, intent, path):
        self.query = type("Q", (), {"search_query": query,
                                    "shot_intent": intent})()
        self.download_path = path
        self.video = type("V", (), {"url": "https://pexels.test/1",
                                    "id": 1, "user_name": "someone"})()


class _Agent:
    """Answers with one match per slot, each asking for a different shot."""

    def __init__(self, pairs):
        self.pairs = pairs

    def match(self, **kwargs):
        return type("R", (), {"matches": [
            _Match(q, i, f"/tmp/{n}.mp4")
            for n, (q, i) in enumerate(self.pairs)]})()


def test_a_clip_remembers_the_shot_it_was_asked_for(tmp_path):
    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.measured_seconds = 5.0
    agent = _Agent([("empty wooden chair", "The chair, alone"),
                    ("name carved in wood", "Push in on the name")])

    clips = beat_clips(agent, beat, tmp_path)

    assert [c.shot_intent for c in clips] == ["The chair, alone",
                                              "Push in on the name"]


def test_an_unfilled_slot_has_no_intent_rather_than_a_borrowed_one(tmp_path):
    """A slot the agent could not fill gets a generated still, which was
    not drawn to any intent. Carrying the previous slot's would be a
    caption for a picture nobody asked for."""
    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.measured_seconds = 5.0
    agent = _Agent([("empty wooden chair", "The chair, alone")])

    clips = beat_clips(agent, beat, tmp_path)

    assert clips[0].shot_intent == "The chair, alone"
    assert clips[1].provider == "unfilled"
    assert clips[1].shot_intent is None


def test_a_stored_plan_without_the_field_still_loads():
    """Plans written before this field exists must keep opening."""
    clip = Clip.model_validate({"path": "a.mp4", "query": "q",
                                "provider": "pexels", "duration": 2.5})

    assert clip.shot_intent is None


# --- the board ------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def test_each_row_carries_its_own_shot_not_the_beats(client):
    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.visual_prompt = "A dim room with a chair."
    beat.clips = [
        Clip(path="", query="empty wooden chair", provider="pexels",
             duration=2.5, shot_intent="The chair, alone"),
        Clip(path="", query="name carved in wood", provider="pexels",
             duration=2.5, shot_intent="Push in on the name"),
    ]
    client.app.state.store.save_plan(plan, status="awaiting_clip_review")

    rows = client.get("/api/plan/p1/clips").json()["clips"]

    assert [r["shot_intent"] for r in rows] == ["The chair, alone",
                                                "Push in on the name"]
    assert len({r["visual_prompt"] for r in rows}) == 1

"""How many clips this beat's length just bought, said at the voice gate.

Clip count is ``ceil(measured_seconds / 2.5)``, so it is decided the
moment the narration is measured -- at the voice gate, one gate before
any footage is fetched. It was never shown there, so the first sight of
"this beat needs four clips" was the clip board, after four downloads
had already happened.

It matters at the voice gate because that gate can still change it. A
beat re-spoken half a second shorter can drop from three slots to two,
and a beat that ran long silently becomes a beat needing more footage
than the script wrote shot descriptions for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from tests.factories import make_plan

VOICE_REVIEW = "awaiting_voice_review"


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _seed(client, lengths):
    plan = make_plan(plan_id="p1", beats=len(lengths))
    for beat, seconds in zip(plan.script.beats, lengths):
        beat.measured_seconds = seconds
    client.app.state.store.save_plan(plan, status=VOICE_REVIEW)
    return plan


def test_each_beat_says_how_many_clips_its_length_needs(client):
    _seed(client, [2.4, 5.4, 9.9])

    rows = client.get("/api/plan/p1/voice").json()["beats"]

    assert [row["clips"] for row in rows] == [1, 3, 4]


def test_it_matches_what_the_clip_stage_will_actually_build(client):
    """Two definitions of the same number would drift; this asserts one."""
    from engine.media.clips import clip_count

    plan = _seed(client, [2.4, 5.4, 9.9])
    rows = client.get("/api/plan/p1/voice").json()["beats"]

    assert [row["clips"] for row in rows] == [
        clip_count(beat.seconds()) for beat in plan.script.beats]


def test_the_gate_totals_them(client):
    """The number of Pexels requests releasing this gate commits to."""
    _seed(client, [2.4, 5.4, 9.9])

    data = client.get("/api/plan/p1/voice").json()

    assert data["clips_total"] == 8

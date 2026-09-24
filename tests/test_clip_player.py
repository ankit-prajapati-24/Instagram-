"""Playing a clip on the review board instead of squinting at one frame.

The board showed a poster frame per clip, and a poster is the first frame.
Stock footage routinely opens on something that has nothing to do with the
rest of the shot -- a title card, a hand entering frame, a second of black
-- so judging a clip by it is judging the wrong thing. The gate exists to
catch footage that does not match the story; it cannot do that from a
still.

So the clip file itself is served, and the card plays it.

Two properties these tests hold on to:

**Range requests.** A browser seeking inside a video sends ``Range`` and
expects ``206`` with ``Content-Range``. Without it the whole file is
pulled before anything moves, and a 20 MB clip makes the board feel
broken.

**The same containment as every other file route.** This one is keyed by
URL input and reads bytes off disk, so it gets the check ``/media``,
``/api/frame`` and ``/api/audio`` already get: nothing outside
``work_dir``/``out_dir`` is ever served.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.contract import Clip
from engine.store import Store
from tests.factories import make_plan

CLIP_REVIEW = "awaiting_clip_review"


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _settings(client) -> Settings:
    return client.app.state.settings


def _store(client) -> Store:
    return client.app.state.store


def _video(settings, path: Path, *, seconds: float = 2.0,
           colour: str = "0x304050") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={colour}:s=128x228:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


def _still(settings, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=0xff00ff:s=128x228:d=1", "-frames:v", "1",
         str(path)], check=True, capture_output=True)
    return path


def _seed(client, *, status=CLIP_REVIEW, beats=2, clips_per=2,
          plan_id="p1", still_slot=None):
    settings = _settings(client)
    plan = make_plan(plan_id=plan_id, beats=beats)
    work = Path(settings.work_dir) / plan_id / "clips"
    for beat in plan.script.beats:
        span = beat.seconds() / clips_per
        clips = []
        for slot in range(clips_per):
            if still_slot == (beat.beat_id, slot):
                path = _still(settings, work / f"{beat.beat_id}-{slot}.png")
            else:
                path = _video(settings, work / f"{beat.beat_id}-{slot}.mp4")
            clips.append(Clip(path=str(path), query=f"grey plate {slot}",
                              provider="pexels", duration=span))
        beat.clips = clips
    _store(client).save_plan(plan, status=status)
    return plan


# --- serving the clip -------------------------------------------------------


def test_a_clip_is_served_whole_with_its_own_media_type(client):
    _seed(client)
    response = client.get("/api/clip/p1/b0/0")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/")
    assert len(response.content) > 1000


def test_a_still_in_a_slot_is_served_as_an_image(client):
    _seed(client, still_slot=("b0", 1))
    response = client.get("/api/clip/p1/b0/1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_seeking_works_because_ranges_are_answered(client):
    """A browser scrubbing a video sends Range and expects 206. Without
    it the whole file is pulled before the first frame moves."""
    _seed(client)
    whole = client.get("/api/clip/p1/b0/0").content

    response = client.get("/api/clip/p1/b0/0",
                          headers={"Range": "bytes=0-99"})

    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-99/")
    assert response.content == whole[:100]


def test_the_board_offers_the_play_url_on_every_row(client):
    _seed(client)
    rows = client.get("/api/plan/p1/clips").json()["clips"]

    assert rows
    for row in rows:
        assert row["play"] == \
            f"/api/clip/p1/{row['beat_id']}/{row['slot']}"


# --- what it refuses --------------------------------------------------------


def test_an_unknown_plan_beat_or_slot_is_404(client):
    _seed(client)
    assert client.get("/api/clip/nope/b0/0").status_code == 404
    assert client.get("/api/clip/p1/nope/0").status_code == 404
    assert client.get("/api/clip/p1/b0/99").status_code == 404


def test_a_slot_whose_file_is_gone_is_404_not_a_broken_stream(client):
    plan = _seed(client)
    Path(plan.script.beats[0].clips[0].path).unlink()

    assert client.get("/api/clip/p1/b0/0").status_code == 404


def test_a_clip_path_outside_the_work_dir_is_never_served(client, tmp_path):
    """Every path here is server-built today, but it is keyed by URL input
    and read off disk, so it gets the containment check the other file
    routes get."""
    outside = _video(_settings(client), tmp_path / "elsewhere" / "x.mp4")
    plan = _seed(client)
    plan.script.beats[0].clips[0] = Clip(
        path=str(outside), query="q", provider="pexels",
        duration=plan.script.beats[0].clips[0].duration)
    _store(client).save_plan(plan, status=CLIP_REVIEW)

    assert client.get("/api/clip/p1/b0/0").status_code == 404


def test_the_clip_is_servable_after_the_gate_too(client):
    """Not gated on the review status: the same board is readable once a
    plan has moved on, and a player that dies at that moment would be a
    surprise, not a safeguard."""
    _seed(client, status="produced")
    assert client.get("/api/clip/p1/b0/0").status_code == 200


def test_the_board_carries_the_line_the_footage_has_to_match(client):
    """The scene description says what to show; the line is what is
    actually being said over it. Matching footage to a beat means
    hearing the line, so the board hands both to the panel."""
    plan = _seed(client)
    rows = client.get("/api/plan/p1/clips").json()["clips"]

    first = next(r for r in rows if r["beat_id"] == "b0")
    beat = plan.script.beats[0]
    assert first["caption_text"] == beat.caption_text
    assert first["voice_text"] == beat.voice_text
    assert first["visual_prompt"] == beat.visual_prompt

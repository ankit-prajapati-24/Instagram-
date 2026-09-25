r"""Choosing a look from the panel, against a real preview.

The picker sits on the clip gate beside the music bed and the sticker
board, because that is the last gate before the render and the one
place the reel's own footage is already on screen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.assembly.looks import PRESETS
from engine.config import Settings
from engine.contract import Clip
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


def _seed(client, *, status=CLIP_REVIEW, timings=True):
    from engine.media.voice import caption_timings

    settings = client.app.state.settings
    plan = make_plan(plan_id="p1", beats=2)
    for index, beat in enumerate(plan.script.beats):
        beat.caption_text = "raat ke teen baje darwaza khula"
        beat.measured_seconds = 4.0
        beat.words = (caption_timings(beat.caption_text, 4.0)
                      if timings else [])
        beat.on_screen_text = "Not Enough" if index == 0 else None
        path = (Path(settings.work_dir) / "p1" / "clips"
                / beat.beat_id / "clip_01.mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x303840:s=320x568:d=3",
             "-pix_fmt", "yuv420p", str(path)], check=True,
            capture_output=True)
        beat.clips = [Clip(path=str(path), query="q", provider="pexels",
                           duration=2.0)]
    client.app.state.store.save_plan(plan, status=status)
    return plan


# --- the menu ---------------------------------------------------------------


def test_the_menu_lists_every_preset_with_a_preview_url(client):
    _seed(client)

    body = client.get("/api/plan/p1/looks").json()

    assert {row["look_id"] for row in body["looks"]} == set(PRESETS)
    for row in body["looks"]:
        assert row["preview"].endswith(row["look_id"])


def test_the_menu_offers_the_parts_a_custom_look_is_made_of(client):
    _seed(client)

    body = client.get("/api/plan/p1/looks").json()

    assert body["fonts"], "no fonts to choose from"
    assert "letters" in body["animations"]


def test_the_menu_says_which_look_is_in_use(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert client.get("/api/plan/p1/looks").json()["chosen"] == "poster"


# --- the preview ------------------------------------------------------------


def test_a_preview_is_served_as_video(client):
    _seed(client)

    response = client.get("/api/look-preview/p1/blocky-urban")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/")
    assert len(response.content) > 2000


def test_a_preview_before_voice_says_so(client):
    _seed(client, timings=False)

    response = client.get("/api/look-preview/p1/poster")

    assert response.status_code == 404
    assert "voice" in response.json()["detail"].lower()


def test_an_unknown_look_id_is_a_404(client):
    _seed(client)

    assert client.get("/api/look-preview/p1/no-such").status_code == 404


# --- choosing ---------------------------------------------------------------


def test_choosing_a_preset_stores_its_values(client):
    _seed(client)

    response = client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert response.status_code == 200
    stored = client.app.state.store.look_choice("p1")
    assert stored["look_id"] == "poster"
    assert stored["font"] == PRESETS["poster"].font


def test_a_custom_look_stores_what_was_posted(client):
    _seed(client)

    client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Teko", "caption_size": 92,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Teko", "punch_size": 116,
        "punch_animation": "swing"})

    stored = client.app.state.store.look_choice("p1")
    assert stored["look_id"] == "custom"
    assert stored["font"] == "Teko"


def test_a_custom_look_naming_an_unbundled_font_is_refused(client):
    """libass substitutes silently, so a font nobody ships would render
    as something else with nothing said."""
    _seed(client)

    response = client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Comic Sans MS", "caption_size": 72,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Comic Sans MS", "punch_size": 82,
        "punch_animation": "fade"})

    assert response.status_code == 422
    assert client.app.state.store.look_choice("p1") is None


def test_a_custom_look_naming_an_unknown_animation_is_refused(client):
    _seed(client)

    response = client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Teko", "caption_size": 92,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Teko", "punch_size": 116,
        "punch_animation": "cartwheel"})

    assert response.status_code == 422


def test_a_produced_reel_cannot_change_its_look(client):
    """The MP4 already carries the captions it was rendered with."""
    _seed(client, status="produced")

    response = client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert response.status_code == 409


def test_a_look_can_be_cleared_back_to_the_channel_default(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert client.delete("/api/plan/p1/look").status_code == 200
    assert client.app.state.store.look_choice("p1") is None


# --- the board --------------------------------------------------------------


def test_the_clip_board_reports_the_reels_look(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "techno"})

    body = client.get("/api/plan/p1/clips").json()

    assert body["look"]["look_id"] == "techno"


def test_a_reel_on_the_default_says_so(client):
    _seed(client)

    body = client.get("/api/plan/p1/clips").json()

    assert body["look"]["look_id"] == client.app.state.settings.look
    assert body["look"]["chosen"] is False

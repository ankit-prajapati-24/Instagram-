"""Picking the reel's music bed from the review board.

Three routes, mirroring the clip picker beside them: search, choose,
and put it back. Searching downloads nothing -- Openverse hands back a
playable URL with every result, so the candidate row points the browser
at the CDN and the disk stays untouched until something is chosen.

Two properties worth naming, because both are about what is *not* here.

**A pick carries an id, never a URL.** Same guard as the clip picker: a
route that downloaded the link the browser handed it would download any
link anyone handed it.

**Choosing is gated on the review status.** The bed is composited by the
render, so the clip gate is the last moment it can change. Letting a
produced reel swap its bed would leave the database describing a file
the MP4 does not contain -- including its credit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.media.music_search import Candidate, MusicChoice, SearchUnavailable
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


def _seed(client, status=CLIP_REVIEW):
    plan = make_plan(plan_id="p1", beats=2)
    client.app.state.store.save_plan(plan, status=status)
    return plan


def _candidate(tid="a1", licence="cc0"):
    return Candidate(
        openverse_id=tid, title="Creepy vinyl", creator="someone",
        licence=licence, licence_version="1.0",
        licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution='"Creepy vinyl" by someone', duration=92000,
        preview=f"https://cdn.freesound.org/previews/{tid}.mp3",
        source_url=f"https://freesound.org/s/{tid}", provider="freesound")


# --- searching --------------------------------------------------------------


def test_a_search_returns_cards_the_panel_can_play(client):
    _seed(client)
    with patch("engine.media.music_search.search",
               return_value=[_candidate()]):
        body = client.get("/api/plan/p1/music-search?q=dark drone").json()

    assert body["results"][0]["title"] == "Creepy vinyl"
    assert body["results"][0]["preview"].endswith(".mp3")
    assert body["results"][0]["seconds"] == 92.0


def test_a_card_says_whether_it_costs_a_credit(client):
    """The whole reason CC-BY is allowed: the obligation is visible
    before it is taken on, not discovered at publish time."""
    _seed(client)
    with patch("engine.media.music_search.search",
               return_value=[_candidate("a1", "cc0"),
                             _candidate("a2", "by")]):
        rows = client.get("/api/plan/p1/music-search?q=x").json()["results"]

    assert [r["needs_credit"] for r in rows] == [False, True]


def test_openverse_being_down_is_a_503_with_the_reason(client):
    _seed(client)
    with patch("engine.media.music_search.search",
               side_effect=SearchUnavailable("Openverse did not answer")):
        response = client.get("/api/plan/p1/music-search?q=x")

    assert response.status_code == 503
    assert "Openverse" in response.json()["detail"]


def test_searching_an_unknown_plan_is_a_404(client):
    assert client.get("/api/plan/nope/music-search?q=x").status_code == 404


# --- choosing ---------------------------------------------------------------


def _choice(tmp_path_str, licence="cc0"):
    return MusicChoice(
        openverse_id="a1", path=tmp_path_str, title="Creepy vinyl",
        creator="someone", licence=licence,
        attribution='"Creepy vinyl" by someone',
        source_url="https://freesound.org/s/a1")


def test_choosing_a_track_stores_it_against_the_plan(client, tmp_path):
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")

    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed))):
        response = client.post("/api/plan/p1/music",
                               json={"openverse_id": "a1"})

    assert response.status_code == 200
    assert client.app.state.store.music_choice("p1")["openverse_id"] == "a1"


def test_the_stored_choice_keeps_the_credit_it_owes(client, tmp_path):
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")

    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed), licence="by")):
        client.post("/api/plan/p1/music", json={"openverse_id": "a1"})

    stored = client.app.state.store.music_choice("p1")
    assert stored["licence"] == "by"
    assert stored["attribution"]


def test_the_response_tells_the_panel_what_credit_was_taken_on(client,
                                                               tmp_path):
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")

    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed), licence="by")):
        body = client.post("/api/plan/p1/music",
                           json={"openverse_id": "a1"}).json()

    assert body["credit"]


def test_a_produced_reel_cannot_swap_its_bed(client):
    """The MP4 already contains a bed, and the record already names its
    credit. Changing either now would make the database describe a file
    that does not exist."""
    _seed(client, status="produced")

    response = client.post("/api/plan/p1/music", json={"openverse_id": "a1"})

    assert response.status_code == 409
    assert "produced" in response.json()["detail"]


def test_a_download_that_fails_is_a_503_and_stores_nothing(client):
    _seed(client)
    with patch("engine.media.music_search.fetch_for_plan",
               side_effect=SearchUnavailable("the audio file answered 404")):
        response = client.post("/api/plan/p1/music",
                               json={"openverse_id": "a1"})

    assert response.status_code == 503
    assert client.app.state.store.music_choice("p1") is None


def test_the_download_may_not_escape_the_work_directory(client, tmp_path):
    """Belt and braces over the id-not-URL rule, matching the clip
    picker: whatever comes back is checked to be where it should be."""
    outside = tmp_path / "elsewhere" / "a1.mp3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"ID3")
    _seed(client)

    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(outside))):
        response = client.post("/api/plan/p1/music",
                               json={"openverse_id": "a1"})

    assert response.status_code == 500
    assert client.app.state.store.music_choice("p1") is None


# --- putting it back --------------------------------------------------------


def test_a_bed_can_be_cleared_back_to_the_shared_folder(client, tmp_path):
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")
    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed))):
        client.post("/api/plan/p1/music", json={"openverse_id": "a1"})

    response = client.delete("/api/plan/p1/music")

    assert response.status_code == 200
    assert client.app.state.store.music_choice("p1") is None


# --- the board shows what is chosen -----------------------------------------


def test_the_clip_board_reports_the_reels_bed(client, tmp_path):
    """The board is where the choice is made, so it has to show what the
    choice currently is -- otherwise the only way to tell is to render."""
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")
    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed), licence="by")):
        client.post("/api/plan/p1/music", json={"openverse_id": "a1"})

    body = client.get("/api/plan/p1/clips").json()

    assert body["music"]["title"] == "Creepy vinyl"
    assert body["music"]["credit"]


def test_a_reel_with_no_bed_of_its_own_says_so(client):
    _seed(client)

    assert client.get("/api/plan/p1/clips").json()["music"] is None


# --- hearing the bed that is set --------------------------------------------


def test_the_chosen_bed_can_be_played_back(client, tmp_path):
    """The panel plays the stored file, not the Openverse URL: what
    matters is what is on disk and going into the render."""
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3" + b"\0" * 4000)
    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed))):
        client.post("/api/plan/p1/music", json={"openverse_id": "a1"})

    response = client.get("/api/music/p1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    assert len(response.content) > 1000


def test_a_reel_with_no_bed_has_nothing_to_play(client):
    _seed(client)

    assert client.get("/api/music/p1").status_code == 404


def test_a_bed_whose_file_has_gone_is_a_404_not_a_crash(client, tmp_path):
    _seed(client)
    bed = tmp_path / "work" / "p1" / "music" / "a1.mp3"
    bed.parent.mkdir(parents=True)
    bed.write_bytes(b"ID3")
    with patch("engine.media.music_search.fetch_for_plan",
               return_value=_choice(str(bed))):
        client.post("/api/plan/p1/music", json={"openverse_id": "a1"})
    bed.unlink()

    assert client.get("/api/music/p1").status_code == 404


def test_the_player_route_will_not_serve_outside_the_work_directory(
        client, tmp_path):
    """The same containment /media, /api/frame and /api/audio get. This
    one reads a path out of the database, and a database is not a
    trust boundary."""
    outside = tmp_path / "elsewhere" / "secret.mp3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"ID3" + b"\0" * 100)
    _seed(client)
    client.app.state.store.set_music_choice(
        "p1", openverse_id="a1", path=str(outside), title="", creator="",
        licence="cc0", attribution="", source_url="")

    assert client.get("/api/music/p1").status_code == 404

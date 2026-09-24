"""Searching Pexels by hand from the review board.

The matcher's clip is often wrong because the *query* was wrong, not
because the ranking was -- a real run searched "ancient greek temple
columns" for a beat about a Himalayan lake, and picking between that
search's five results would not have helped. So the board can run its
own search, with a query the user typed.

No test here touches the network: every one drives a
``httpx.MockTransport`` holding a real Pexels response shape.

The property most worth holding on to is the last one in this file. A
pick carries a Pexels **id**, never a URL, so the only links this server
ever fetches are ones Pexels gave it. A route that downloaded whatever
link the browser sent would download whatever link anyone sent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from engine.config import Settings
from engine.media.clip_search import (PICKED_PROVIDER, Candidate,
                                      SearchUnavailable, place_in_slot,
                                      search, used_ids)
from engine.contract import Clip
from tests.factories import make_plan


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    s.pexels_api_key = "test-key"
    return s


def _video(vid, *, width=1080, height=1920, name="Someone"):
    return {
        "id": vid,
        "url": f"https://www.pexels.com/video/{vid}/",
        "duration": 14,
        "image": f"https://images.pexels.com/{vid}.jpg",
        "user": {"name": name},
        "video_files": [
            # The SD file shares the video's orientation, as Pexels' own
            # do -- an SD entry that disagreed made an earlier version of
            # this fixture report a landscape video as portrait.
            {"file_type": "video/mp4",
             "width": 640 if height >= width else 1138,
             "height": 1138 if height >= width else 640,
             "link": f"https://player.pexels.com/{vid}-sd.mp4"},
            {"file_type": "video/mp4", "width": width, "height": height,
             "link": f"https://player.pexels.com/{vid}-hd.mp4"},
        ],
    }


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- searching --------------------------------------------------------------


def test_a_search_returns_candidates_the_board_can_show(settings):
    def handler(request):
        assert request.headers["authorization"] == "test-key"
        return httpx.Response(200, json={"videos": [_video(1), _video(2)]})

    results = search("frozen lake", settings, client=_client(handler))

    assert [c.pexels_id for c in results] == [1, 2]
    assert results[0].preview.startswith("https://images.pexels.com/")
    assert results[0].author == "Someone"
    assert results[0].source_url.endswith("/1/")


def test_the_preview_uses_the_lightest_file_not_the_largest(settings):
    """A dozen 4K files is hundreds of megabytes of browser traffic to
    decide one slot."""
    def handler(request):
        return httpx.Response(200, json={"videos": [_video(1)]})

    assert search("x", settings, client=_client(handler))[0].play \
        .endswith("-sd.mp4")


def test_landscape_results_are_offered_rather_than_hidden(settings):
    """Orientation is a preference: the render crops to portrait anyway,
    and a hand search that returned nothing sends the user back to
    guessing."""
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if "orientation" in request.url.params:
            return httpx.Response(200, json={"videos": []})
        return httpx.Response(200, json={
            "videos": [_video(7, width=1920, height=1080)]})

    results = search("x", settings, client=_client(handler))

    assert [c.pexels_id for c in results] == [7]
    assert results[0].to_dict()["portrait"] is False
    assert any("orientation" in c for c in calls), "portrait was not tried first"


def test_the_same_video_is_not_offered_twice(settings):
    def handler(request):
        return httpx.Response(200, json={"videos": [_video(3)]})

    assert len(search("x", settings, client=_client(handler))) == 1


def test_a_video_with_no_mp4_is_skipped_not_shown_broken(settings):
    def handler(request):
        bad = {"id": 9, "url": "u", "duration": 3, "video_files": []}
        return httpx.Response(200, json={"videos": [bad, _video(4)]})

    assert [c.pexels_id for c in
            search("x", settings, client=_client(handler))] == [4]


def test_the_limit_is_respected(settings):
    def handler(request):
        return httpx.Response(200, json={
            "videos": [_video(i) for i in range(50)]})

    assert len(search("x", settings, client=_client(handler), limit=5)) == 5


# --- what it refuses --------------------------------------------------------


def test_no_api_key_says_so_rather_than_returning_nothing(settings):
    settings.pexels_api_key = ""
    with pytest.raises(SearchUnavailable, match="PEXELS_API_KEY"):
        search("x", settings)


def test_an_empty_query_is_refused(settings):
    with pytest.raises(SearchUnavailable):
        search("   ", settings)


def test_a_rate_limit_is_named_with_the_actual_limit(settings):
    def handler(request):
        return httpx.Response(429, json={})

    with pytest.raises(SearchUnavailable, match="200 requests"):
        search("x", settings, client=_client(handler))


def test_pexels_being_down_is_reported_not_swallowed(settings):
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchUnavailable, match="did not answer"):
        search("x", settings, client=_client(handler))


# --- duplicates -------------------------------------------------------------


def test_ids_already_used_in_the_plan_are_reported_with_where(settings):
    plan = make_plan(plan_id="p1", beats=2)
    for beat in plan.script.beats:
        beat.clips = [Clip(path="x.mp4", query="q", provider="pexels",
                           duration=1.0, pexels_id=55)]

    where = used_ids(plan)

    assert set(where) == {55}
    assert len(where[55]) == 2
    assert "slot 0" in where[55][0]


def test_a_plan_with_no_pexels_ids_reports_nothing(settings):
    plan = make_plan(plan_id="p1", beats=1)
    plan.script.beats[0].clips = [
        Clip(path="x.mp4", query="q", provider="upload", duration=1.0)]
    assert used_ids(plan) == {}


# --- picking one ------------------------------------------------------------


class _Downloader:
    """Stands in for ClipDownloader, honouring its arguments."""

    def __init__(self):
        self.calls = []

    def download_clip(self, url, index, output_dir=None, **kwargs):
        from pathlib import Path
        self.calls.append((url, index, output_dir))
        target = Path(output_dir) / f"clip_{index:02d}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
        return target


def _plan_with_slot(settings):
    plan = make_plan(plan_id="p1", beats=1)
    plan.script.beats[0].clips = [
        Clip(path="old.mp4", query="a wrong query", provider="pexels",
             duration=2.5, pexels_id=1)]
    return plan


def test_picking_downloads_the_largest_file_and_rewrites_the_slot(settings):
    def handler(request):
        assert request.url.path.endswith("/videos/42")
        return httpx.Response(200, json=_video(42, name="Ada"))

    plan = _plan_with_slot(settings)
    beat = plan.script.beats[0]
    downloader = _Downloader()

    clip = place_in_slot(plan, beat, 0, 42, settings, settings.work_dir,
                         client=_client(handler), downloader=downloader)

    assert clip.provider == PICKED_PROVIDER
    assert clip.pexels_id == 42
    assert clip.author == "Ada"
    assert downloader.calls[0][0].endswith("-hd.mp4"), \
        "the preview file was downloaded instead of the full one"


def test_picking_never_touches_the_slot_duration(settings):
    """The narration owns it, and the render trims or loops to it."""
    def handler(request):
        return httpx.Response(200, json=_video(42))

    plan = _plan_with_slot(settings)
    before = plan.script.beats[0].clips[0].duration

    clip = place_in_slot(plan, plan.script.beats[0], 0, 42, settings,
                         settings.work_dir, client=_client(handler),
                         downloader=_Downloader())

    assert clip.duration == before


def test_a_pick_carries_an_id_and_the_link_comes_back_from_pexels(settings):
    """The property that keeps this route from fetching anything anyone
    asks it to: the browser sends a number, not a URL."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=_video(42))

    plan = _plan_with_slot(settings)
    place_in_slot(plan, plan.script.beats[0], 0, 42, settings,
                  settings.work_dir, client=_client(handler),
                  downloader=_Downloader())

    assert len(seen) == 1
    assert seen[0].startswith("https://api.pexels.com/")
    assert seen[0].endswith("/42")


def test_a_video_pexels_has_dropped_is_reported(settings):
    def handler(request):
        return httpx.Response(404, json={})

    with pytest.raises(SearchUnavailable, match="no video"):
        place_in_slot(_plan_with_slot(settings),
                      _plan_with_slot(settings).script.beats[0], 0, 42,
                      settings, settings.work_dir,
                      client=_client(handler), downloader=_Downloader())


def test_a_video_with_no_downloadable_file_is_reported(settings):
    def handler(request):
        return httpx.Response(200, json={"id": 42, "url": "u",
                                         "video_files": []})

    with pytest.raises(SearchUnavailable, match="no mp4"):
        place_in_slot(_plan_with_slot(settings),
                      _plan_with_slot(settings).script.beats[0], 0, 42,
                      settings, settings.work_dir,
                      client=_client(handler), downloader=_Downloader())


# --- the routes -------------------------------------------------------------


@pytest.fixture()
def api(tmp_path):
    from fastapi.testclient import TestClient

    from engine.app import create_app

    s = Settings()
    s.db_path = tmp_path / "t.db"
    s.out_dir = tmp_path / "out"
    s.work_dir = tmp_path / "work"
    s.pexels_api_key = "test-key"
    s.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=s))


def _seed(api, *, status="awaiting_clip_review", pexels_id=1):
    plan = make_plan(plan_id="p1", beats=1)
    plan.script.beats[0].clips = [
        Clip(path="old.mp4", query="a wrong query", provider="pexels",
             duration=2.5, pexels_id=pexels_id)]
    api.app.state.store.save_plan(plan, status=status)
    return plan


def test_searching_returns_results_and_marks_the_ones_already_used(
        api, monkeypatch):
    import engine.media.clip_search as mod

    monkeypatch.setattr(mod, "search", lambda q, s, **k: [
        Candidate(pexels_id=1, preview="p1", play="v1", duration=5,
                  author="A", source_url="s1", width=1080, height=1920),
        Candidate(pexels_id=2, preview="p2", play="v2", duration=6,
                  author="B", source_url="s2", width=1080, height=1920),
    ])
    _seed(api, pexels_id=1)

    body = api.get("/api/plan/p1/clip-search", params={"q": "lake"}).json()

    assert [r["pexels_id"] for r in body["results"]] == [1, 2]
    assert body["results"][0]["used_in"], "a duplicate was not marked"
    assert body["results"][1]["used_in"] == []


def test_a_search_problem_is_a_503_that_says_what_happened(api, monkeypatch):
    import engine.media.clip_search as mod

    def boom(q, s, **k):
        raise mod.SearchUnavailable("Pexels is rate limiting this key")

    monkeypatch.setattr(mod, "search", boom)
    _seed(api)

    response = api.get("/api/plan/p1/clip-search", params={"q": "x"})
    assert response.status_code == 503
    assert "rate limiting" in response.json()["detail"]


def test_picking_a_clip_rewrites_the_slot(api, monkeypatch):
    import engine.media.clip_search as mod

    def fake_place(plan, beat, slot, pexels_id, settings, work_dir, **k):
        from pathlib import Path
        target = Path(work_dir) / "p1" / "clips" / "picked.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        beat.clips[slot] = Clip.model_validate({
            **beat.clips[slot].model_dump(),
            "path": str(target), "provider": mod.PICKED_PROVIDER,
            "pexels_id": pexels_id, "author": "Ada"})
        return beat.clips[slot]

    monkeypatch.setattr(mod, "place_in_slot", fake_place)
    _seed(api)

    body = api.post("/api/plan/p1/clip/b0/0/pick",
                    json={"pexels_id": 42}).json()

    assert body["provider"] == mod.PICKED_PROVIDER
    assert body["pexels_id"] == 42
    assert body["play"] == "/api/clip/p1/b0/0"
    stored = api.app.state.store.get_plan("p1").script.beats[0].clips[0]
    assert stored.pexels_id == 42


def test_picking_is_refused_off_the_review_gate(api):
    _seed(api, status="produced")
    response = api.post("/api/plan/p1/clip/b0/0/pick", json={"pexels_id": 1})
    assert response.status_code == 409


def test_picking_an_unknown_beat_or_slot_is_404(api):
    _seed(api)
    assert api.post("/api/plan/p1/clip/nope/0/pick",
                    json={"pexels_id": 1}).status_code == 404
    assert api.post("/api/plan/p1/clip/b0/9/pick",
                    json={"pexels_id": 1}).status_code == 404


def test_the_pick_route_accepts_no_url_from_the_caller(api):
    """The shape of the request is the safeguard: there is no field a
    caller could put a link in."""
    from engine.app import PickRequest

    assert set(PickRequest.model_fields) == {"pexels_id"}

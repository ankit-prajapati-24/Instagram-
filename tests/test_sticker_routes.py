from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.assembly import sticker_catalog
from engine.config import Settings
from engine.contract import StickerCue
from engine.media.voice import caption_timings
from engine.store import Store
from tests.factories import make_plan

REAL_CATALOGUE = Path("work/_lordicon") / sticker_catalog.CACHE_NAME


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    settings.stickers = True
    app = TestClient(create_app(settings=settings))
    # A real sitemap, copied not fetched: `refresh` no-ops when the cache is
    # already there, so this is what keeps the suite off the network.
    cache = Path(settings.work_dir) / "_lordicon"
    cache.mkdir(parents=True, exist_ok=True)
    if not REAL_CATALOGUE.exists():
        pytest.skip(f"catalogue not cached at {REAL_CATALOGUE}")
    shutil.copy(REAL_CATALOGUE, cache / sticker_catalog.CACHE_NAME)
    return app


def _store(client) -> Store:
    return client.app.state.store


def _stored(client, stickers):
    """A saved plan with caption timings, and the given per-beat stickers.

    ``stickers`` maps a beat id to a ``StickerCue``. Timings matter: a beat
    with no ``words`` produces no cue at all, so a plan saved without them
    would make every assertion below vacuous.
    """
    plan = make_plan(beats=4, measured=4.0)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, 4.0)
    for beat_id, cue in stickers.items():
        beat = next(b for b in plan.script.beats if b.beat_id == beat_id)
        beat.sticker = cue
        # The named word has to be in the caption, or the cue lands at the
        # midpoint and the test stops testing what it says it tests.
        beat.caption_text = f"{beat.caption_text} {cue.word}"
        beat.words = caption_timings(beat.caption_text, 4.0)
    _store(client).save_plan(plan)
    return plan.plan_id


@pytest.fixture()
def plan_with_stickers(client):
    return _stored(client, {"b2": StickerCue(
        word="raat", terms=["moon", "star", "night"])})


@pytest.fixture()
def plan_no_hits(client):
    """Terms that are all abstract nouns, which the catalogue does not have."""
    return _stored(client, {"b2": StickerCue(
        word="raat", terms=["dream", "journey", "farewell"])})


def test_candidates_are_keyed_by_beat_and_searched_by_the_scripts_terms(client, plan_with_stickers):
    body = client.get(f"/api/plan/{plan_with_stickers}/stickers").json()
    row = next(r for r in body["rows"] if r["beat_id"] == "b2")
    assert row["choose"].endswith("/sticker/b2")
    assert row["terms"] == ["moon", "star", "night"]
    assert row["candidates"], "the script's terms should find icons"


def test_a_beat_whose_terms_find_nothing_still_gets_a_row(client, plan_no_hits):
    """No candidates is not no sticker. The emoji renders and the person
    can type their own term."""
    body = client.get(f"/api/plan/{plan_no_hits}/stickers").json()
    row = next(r for r in body["rows"] if r["beat_id"] == "b2")
    assert row["candidates"] == []


def test_choosing_is_keyed_by_beat(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b2",
                    json={"slug": "27-globe"})
    assert r.status_code == 200
    assert r.json()["style"] in ("punchy", "dark")
    assert _store(client).sticker_choices(plan_with_stickers) == \
        {"b2": "27-globe"}


def test_choosing_for_a_beat_that_does_not_exist_is_a_404(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b99",
                    json={"slug": "27-globe"})
    assert r.status_code == 404


def test_search_by_hand_returns_catalogue_slugs(client):
    body = client.get("/api/sticker-search", params={"term": "cloud"}).json()
    assert body["slugs"], "cloud is in the catalogue"
    assert all("-" in slug for slug in body["slugs"])


def test_search_never_treats_the_term_as_a_path(client, tmp_path):
    """The term is caller-controlled and is only ever matched against slugs
    already in the sitemap. It must not reach the filesystem."""
    for hostile in ("../../../etc/passwd", "/etc/passwd", "..\\..\\win.ini"):
        body = client.get("/api/sticker-search",
                          params={"term": hostile}).json()
        assert body["slugs"] == []


def test_choosing_a_slug_that_is_not_in_the_catalogue_is_refused(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b2",
                    json={"slug": "../../../evil"})
    assert r.status_code == 400

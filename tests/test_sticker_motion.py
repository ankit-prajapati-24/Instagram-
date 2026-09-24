"""Seeing a sticker move before committing seventeen seconds to baking it.

The picker showed ``preview_png`` -- one matted, graded frame. That frame
is the right thing to rest on, because it is what the icon will actually
look like in the reel, grade and all. But every one of these icons is an
animation, and a wall of stills says nothing about how any of them move:
a spinning lock and a wobbling lock are the same picture.

So the still stays, and the source GIF is served alongside it for the
panel to swap in on hover. Nothing is baked, nothing is graded, nothing
is committed -- it is the same cached source file the bake already
downloads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from tests.factories import make_plan


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _stub_catalogue(monkeypatch, tmp_path, slug="1821-moon-stars"):
    """A catalogue holding one slug, and a real GIF on disk for it."""
    import engine.app as app_mod
    from PIL import Image

    cache = tmp_path / "stickers"
    (cache / "sources").mkdir(parents=True, exist_ok=True)
    gif = cache / "sources" / f"{slug}.gif"
    frames = [Image.new("RGB", (8, 8), (i * 40, 0, 0)) for i in range(1, 4)]
    frames[0].save(gif, save_all=True, append_images=frames[1:],
                   duration=80, loop=0)

    monkeypatch.setattr(app_mod.sticker_choices_mod, "cache_root",
                        lambda settings: cache)
    monkeypatch.setattr(app_mod.sticker_catalog, "load",
                        lambda root: (slug,))
    return slug, gif


def test_the_animated_source_is_served_for_a_catalogue_slug(
        client, tmp_path, monkeypatch):
    slug, gif = _stub_catalogue(monkeypatch, tmp_path)

    response = client.get(f"/api/sticker-motion/{slug}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content == gif.read_bytes()
    assert response.content[:3] == b"GIF"


def test_what_is_served_is_really_an_animation(client, tmp_path,
                                               monkeypatch):
    """A still would defeat the whole point, so the frame count is
    checked rather than assumed."""
    from io import BytesIO

    from PIL import Image

    slug, _gif = _stub_catalogue(monkeypatch, tmp_path)
    body = client.get(f"/api/sticker-motion/{slug}").content

    with Image.open(BytesIO(body)) as im:
        assert getattr(im, "n_frames", 1) > 1


def test_a_slug_outside_the_catalogue_is_refused(client, tmp_path,
                                                 monkeypatch):
    """The slug reaches a filename, so it is checked against the
    catalogue rather than sanitised and hoped over."""
    _stub_catalogue(monkeypatch, tmp_path)

    assert client.get("/api/sticker-motion/not-a-real-slug").status_code \
        == 404
    assert client.get("/api/sticker-motion/..%2f..%2fsecret").status_code \
        in (404, 400)


def test_a_slug_in_the_catalogue_with_no_file_yet_is_not_a_500(
        client, tmp_path, monkeypatch):
    """The source downloads on first use; if that fails the picker should
    keep working with its stills."""
    import engine.app as app_mod

    slug, gif = _stub_catalogue(monkeypatch, tmp_path)
    gif.unlink()

    def boom(*a, **k):
        raise RuntimeError("lordicon is down")

    monkeypatch.setattr(app_mod.sticker_choices_mod, "source_gif", boom)
    assert client.get(f"/api/sticker-motion/{slug}").status_code == 502


def test_the_server_hands_the_motion_url_to_the_panel(client, tmp_path,
                                                      monkeypatch):
    """The panel must not build this URL itself.

    An earlier version of this test looked for the literal path in
    index.html -- which would have passed only if the panel hardcoded
    it, the opposite of what is wanted. What matters is that the board's
    candidate rows carry the URL and the page reads it from there.
    """
    import engine.app as app_mod

    slug, _gif = _stub_catalogue(monkeypatch, tmp_path)
    plan = make_plan(plan_id="p1", beats=2)
    client.app.state.store.save_plan(plan, status="awaiting_clip_review")

    monkeypatch.setattr(app_mod.sticker_catalog, "refresh",
                        lambda root: None)
    monkeypatch.setattr(app_mod.sticker_catalog, "search",
                        lambda term, slugs: [slug])
    # One cue, stubbed: this test is about the URL the row carries, and
    # make_plan's text contains no trigger word to fire on.
    cue = SimpleNamespace(name="night", word="raat", start=1.5,
                          beat_index=0, beat_id="b0", terms=("night",),
                          source="trigger")
    monkeypatch.setattr(app_mod.stickers_mod, "find_cues",
                        lambda plan, cap=None: [cue])

    rows = client.get("/api/plan/p1/stickers").json()["rows"]
    offered = [c for row in rows for c in row["candidates"]]
    assert offered, "the fixture produced no candidates to check"
    for candidate in offered:
        assert candidate["motion"] == f"/api/sticker-motion/{candidate['slug']}"

    page = (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")
    assert "dataset.motion" in page, "the panel ignores the URL it is given"
    assert "/api/sticker-motion/" not in page,         "the panel hardcodes a URL the server already provides"

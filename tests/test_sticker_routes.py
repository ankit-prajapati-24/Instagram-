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


def test_choosing_a_well_formed_slug_that_is_not_in_the_catalogue_is_refused(
        client, plan_with_stickers):
    """The membership check on its own, with nothing else covering for it.

    `../../../evil` is also rejected by `safe_slug`'s shape regex, so it
    cannot show what this gate does. `9999-not-a-real-icon` has a perfectly
    valid slug shape and simply is not in the sitemap -- without the
    membership check this route would fetch whatever URL that slug builds.
    """
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b2",
                    json={"slug": "9999-not-a-real-icon"})
    assert r.status_code == 400


# --- the panel ------------------------------------------------------------
#
# These read index.html as text. That is crude, but the alternative is a
# browser, and what they guard is not layout: it is that the page keys its
# cards the way the backend keys its choices, and that it uses the URLs the
# server hands it instead of building its own.


def _panel() -> str:
    import engine.app as app_mod
    return (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")


def test_the_panel_keys_its_sticker_cards_on_the_beat_not_the_trigger():
    """One reel can fire one trigger at two beats, so the trigger name
    cannot be the DOM key.

    Keyed by trigger, `stickercard-water` would be written twice, the
    second card would find the first card's node, and clicking it would
    post to the first beat's choose URL -- the same collision the
    beat-keyed store removed from the backend, still live in the panel.
    """
    page = _panel()
    assert "data-trigger" not in page, \
        "the panel still keys a candidate on the trigger name"
    assert "stickercard-${r.trigger}" not in page, \
        "two cues of one trigger would collide on this id"
    assert "data-beat" in page, "the panel does not key anything on the beat"


def test_the_panel_searches_through_the_url_the_server_gives_it():
    """Same rule as the motion URL: the row carries `search`, so the page
    must read it rather than spelling the route out itself."""
    page = _panel()
    assert "/api/sticker-search" not in page, \
        "the panel hardcodes a route the server already provides"
    assert ("dataset.search" in page or "row.search" in page
            or "r.search" in page or "rows[0].search" in page), \
        "the panel ignores the search URL it is given"


def test_the_panel_does_not_still_promise_a_seventeen_second_bake():
    """A pick baked both styles until the beat's role made the style
    knowable in advance. It is one style now, about half the wait, and a
    panel that says seventeen is telling the person to expect twice what
    it costs."""
    page = _panel()
    assert "17 second" not in page and "about 17s" not in page, \
        "the panel still quotes the two-style bake time"


# --- the panel, actually run ----------------------------------------------

RENDER_STUBS = """
const NODES = {};
function node(id) {
  if (!NODES[id]) NODES[id] = { id, innerHTML: "", textContent: "",
    classList: { contains: () => false, add(){}, remove(){}, toggle(){} },
    querySelectorAll: () => [], value: "" };
  return NODES[id];
}
const $ = node;
const esc = (s) => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
let STICKERS = null;
"""

RENDER_CHECKS = """
STICKERS = { rows: [
  { beat_id: "b1", trigger: "water", source: "trigger", word: "paani",
    terms: ["lake"], start: 4.76, style: "punchy", chosen: null,
    candidates: [{slug:"27-globe", preview:"/p/a", motion:"/m/a"}],
    choose: "/api/plan/p1/sticker/b1", search: "/api/sticker-search" },
  { beat_id: "b3", trigger: "water", source: "trigger", word: "paani",
    terms: ["lake"], start: 12.57, style: "dark", chosen: "1875-planet",
    candidates: [{slug:"1875-planet", preview:"/p/b", motion:"/m/b"}],
    choose: "/api/plan/p1/sticker/b3", search: "/api/sticker-search" },
  { beat_id: "b5", trigger: "b5", source: "model", word: "code",
    terms: ["laptop","keyboard"], start: 20.1, style: "punchy", chosen: null,
    candidates: [], choose: "/api/plan/p1/sticker/b5",
    search: "/api/sticker-search" }
]};
renderStickers();
const out = NODES["stickergrid"].innerHTML;
const checks = [
  ["three cards, no collided id", (out.match(/id="stickercard-/g)||[]).length === 3],
  ["no trigger-keyed card id", !out.includes("stickercard-water")],
  ["thumbs carry the beat", out.includes('data-beat="b1"')],
  ["b3 keeps its own chosen marker", /class="chosen"[^>]*data-beat="b3"/.test(out)],
  ["b1 is not marked chosen", !/class="chosen"[^>]*data-beat="b1"/.test(out)],
  // Searching moved above the board: one box for the reel instead of one
  // per card, which meant typing the same search once per sticker into an
  // input that did not fit the card holding it.
  ["no card carries its own search box", !out.includes("stickerfind")],
  ["a model cue says the script asked", out.includes("the script asked for it")],
  ["empty candidates invite a search", out.includes("no candidates")]
];
let bad = 0;
for (const [name, ok] of checks) {
  if (!ok) { console.log("FAIL " + name); bad++; }
}
process.exit(bad ? 1 : 0);
"""


def test_the_panel_renders_two_cues_of_one_trigger_as_two_cards(tmp_path):
    """The collision, proven by running the renderer rather than reading it.

    Every other test in this section greps index.html, which cannot tell a
    working page from one that throws: a typo in the template literal would
    satisfy all of them and blank the board. This extracts the render
    functions and runs them under node against a payload with two `water`
    cues at different beats -- the exact shape that collided when cards
    were keyed by trigger name.

    Skips rather than fails where node is absent; the grep tests still
    hold the line there.
    """
    import shutil
    import subprocess

    import engine.app as app_mod

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")

    page = (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")
    script = page[page.index("<script"):page.rindex("</script>")]
    body = script[script.index("function renderStickers()"):
                  script.index("async function chooseSticker")]

    harness = tmp_path / "render_check.js"
    harness.write_text(RENDER_STUBS + body + RENDER_CHECKS, encoding="utf-8")
    done = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert done.returncode == 0, (
        f"the sticker board did not render as it must:\n{done.stdout}"
        f"{done.stderr}")

# Sticker Picker — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the panel offer Lordicon candidates for every sticker that fired, and remember which one was chosen for this reel.

**Architecture:** A cached copy of Lordicon's sitemap is searched with terms the trigger map carries, so `death` looks for skulls rather than for the word "death". A chosen slug is written to its own store table, keyed by plan and trigger, and baked into a content-addressed cache shared across reels. `prepare()` gains one new rung above the committed art; with no choice recorded it behaves exactly as it does today.

**Tech Stack:** Python 3.14, Pillow, FastAPI, sqlite3, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-23-sticker-picker-design.md`

**Scope:** Backend only. The `engine/ui/index.html` section the spec describes is deliberately **not** in this plan — that file is being edited by another session, and the routes here are usable and testable without it.

## Global Constraints

- **No new runtime dependency.** Pillow, `imageio-ffmpeg` and stdlib only; `urllib.request` is the download mechanism, as in `scripts/fetch_sticker_art.py`.
- **A render must need nothing external.** The catalogue is fetched and icons are baked at *choose* time only. Nothing in this plan may add network or Pillow work to the render path.
- **A missing decoration must never cost a render, and a missing choice must never cost one either.** Every new failure returns `None` and falls to the rung below; `prepare()` keeps returning a list rather than raising.
- **`prepare(plan, settings)` with no choices must behave byte-identically to today.** There is an existing suite of 156 tests over this behaviour; it must stay green untouched.
- **Only slugs present in the catalogue may be downloaded.** The choose route takes a slug from an HTTP body; fetching an arbitrary URL from it is the obvious hole.
- **`engine/contract.py` must not be modified.** A chosen slug is a production decision about one render, not part of the plan's creative content, and that file is contended.
- **The trigger map stays data.** `test_a_new_trigger_needs_no_code_change` must keep passing; adding search terms to a trigger must also need no code change.
- **Lordicon licence:** a chosen icon still renders as designed art, so `Sticker.baked` must be `True` for it and the credit must still be owed.
- This codebase writes docstrings and comments that explain *why*, not *what*.

## Review Focus

1. **The catalogue is unreachable** (Lordicon down, no network, sitemap moved) — the candidates route must return an empty list with a reason, not a 500 and not a stack trace. *(Task 1, Task 5)*
2. **A slug that is not in the catalogue is posted to the choose route** — it must be refused before any download, or the panel becomes a URL fetcher. *(Task 5)*
3. **A chosen icon fails the interior-white check at bake time** — `bake_one` raises by design; the route must report it and leave the previous art in place rather than 500 or half-write a choice. *(Task 3, Task 5)*
4. **A chosen slug whose cache directory was deleted** — resolution must fall through to committed art, then emoji, and never raise. *(Task 4)*
5. **Two plans choose different slugs for the same trigger** — the cache is content-addressed and the table is keyed by plan, so neither may see the other's art. *(Task 2, Task 4)*

---

### Task 1: The catalogue

**Files:**
- Create: `engine/assembly/sticker_catalog.py`
- Test: `tests/test_sticker_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SITEMAP_URL: str`, `refresh(cache_dir: Path, *, force: bool = False) -> Path`, `load(cache_dir: Path) -> tuple[str, ...]`, `search(term: str, slugs: tuple[str, ...], *, limit: int = 8) -> list[str]`, `gif_url(slug: str, variant: str = "flat") -> str`. Tasks 3 and 5 use all of them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sticker_catalog.py`:

```python
"""Lordicon's catalogue: fetched once, searched locally.

Nothing here talks to the network except `refresh`, and its test writes the
sitemap itself. Searching is a pure function over a tuple of slugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.assembly import sticker_catalog as cat

SLUGS = ("1414-circle", "27-globe", "1547-globe-honeymoon", "1875-planet",
         "1195-earthworm", "2130-skull-poison", "2816-skull-halloween",
         "2841-crashed-skull", "440-dna", "1278-dna-diagnonal")


def _sitemap(path: Path, slugs=SLUGS) -> Path:
    locs = "".join(
        f"<url><loc>https://lordicon.com/icons/wired/flat/{s}</loc></url>"
        f"<url><loc>https://lordicon.com/icons/wired/outline/{s}</loc></url>"
        for s in slugs)
    path.write_text(f"<urlset>{locs}</urlset>", encoding="utf-8")
    return path


def test_the_url_is_built_the_same_way_the_fetch_script_builds_it():
    assert cat.gif_url("2130-skull-poison") == (
        "https://media.lordicon.com/icons/wired/flat/2130-skull-poison.gif")
    assert cat.gif_url("27-globe", "outline") == (
        "https://media.lordicon.com/icons/wired/outline/27-globe.gif")


def test_loading_collapses_the_four_style_variants_to_one_slug_each(tmp_path):
    _sitemap(tmp_path / "sitemap.xml")
    slugs = cat.load(tmp_path)
    assert len(slugs) == len(SLUGS)
    assert "27-globe" in slugs
    assert not any(s.startswith("https://") for s in slugs)


def test_an_exact_word_beats_a_prefix_and_a_shorter_name_beats_a_longer():
    hits = cat.search("globe", SLUGS)
    assert hits[0] == "27-globe", hits
    assert "1547-globe-honeymoon" in hits


def test_search_is_over_the_name_not_the_number():
    # "27" is the number on 27-globe; searching it must not surface it.
    assert cat.search("27", SLUGS) == []


def test_a_bad_term_returns_the_wrong_thing_which_is_why_terms_are_curated():
    """The catalogue has no icon named for the planet.

    Searching `earth` returns a worm, and no amount of ranking fixes that --
    which is why the trigger map carries search terms rather than the
    trigger's own name being used as the query.
    """
    assert cat.search("earth", SLUGS) == ["1195-earthworm"]


def test_an_empty_or_missing_catalogue_searches_to_nothing(tmp_path):
    assert cat.search("skull", ()) == []
    assert cat.load(tmp_path) == ()


def test_refresh_writes_once_and_reuses(tmp_path, monkeypatch):
    calls = []

    def fake_urlretrieve(url, dest):
        calls.append(url)
        _sitemap(Path(dest))

    monkeypatch.setattr(cat.urllib.request, "urlretrieve", fake_urlretrieve)

    first = cat.refresh(tmp_path)
    second = cat.refresh(tmp_path)
    assert first == second
    assert len(calls) == 1, "second refresh should not hit the network"

    cat.refresh(tmp_path, force=True)
    assert len(calls) == 2


def test_an_unreachable_catalogue_raises_a_named_error(tmp_path, monkeypatch):
    def boom(url, dest):
        raise OSError("no route to host")

    monkeypatch.setattr(cat.urllib.request, "urlretrieve", boom)
    with pytest.raises(cat.CatalogUnavailable, match="no route to host"):
        cat.refresh(tmp_path)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_sticker_catalog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.assembly.sticker_catalog'`.

- [ ] **Step 3: Write the module**

Create `engine/assembly/sticker_catalog.py`:

```python
"""Lordicon's catalogue of icon slugs.

Fetched once and searched locally. Their site renders its icon browser in
JavaScript and returns 403 to a plain fetch, but the sitemap is static XML
and public, and it lists every icon -- 3,578 of them at the time of writing.
That is the whole catalogue, without an API key or a login.

Nothing here runs during a render. The panel calls it while someone is
choosing, which is the only time a fresh catalogue matters.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

SITEMAP_URL = "https://lordicon.com/sitemap-icons-wired.xml"
CACHE_NAME = "lordicon-wired.xml"

# https://lordicon.com/icons/wired/<variant>/<slug>
_LOC = re.compile(r"<loc>https://lordicon\.com/icons/wired/\w+/([^<]+)</loc>")

_GIF = "https://media.lordicon.com/icons/wired/{variant}/{slug}.gif"


class CatalogUnavailable(RuntimeError):
    """The catalogue could not be fetched. Callers show no candidates and
    carry on: a picker that cannot list icons is an inconvenience, not a
    failed render."""


def gif_url(slug: str, variant: str = "flat") -> str:
    """The public GIF for one icon, the same URL the fetch script builds."""
    return _GIF.format(variant=variant, slug=slug)


def refresh(cache_dir: Path, *, force: bool = False) -> Path:
    """Download the sitemap if it is not already cached. Returns its path.

    Cached rather than re-fetched because it is 5.4MB and changes about as
    often as Lordicon adds icons, which is not while someone is picking one.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / CACHE_NAME
    if dest.exists() and not force:
        return dest
    staged = dest.with_suffix(".part")
    try:
        urllib.request.urlretrieve(SITEMAP_URL, staged)
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise CatalogUnavailable(str(exc)) from exc
    staged.replace(dest)
    return dest


def load(cache_dir: Path) -> tuple[str, ...]:
    """Every distinct slug in the cached sitemap, or ``()`` if there is none.

    The sitemap lists each icon four times, once per style variant; a slug
    identifies the icon, and the variant is chosen at download time.
    """
    path = Path(cache_dir) / CACHE_NAME
    if not path.exists():
        return ()
    text = path.read_text(encoding="utf-8")
    return tuple(dict.fromkeys(_LOC.findall(text)))


def search(term: str, slugs: tuple[str, ...], *, limit: int = 8) -> list[str]:
    """Slugs matching ``term``, best first.

    A slug is ``<number>-<name>``; only the name is searched, because the
    numbers are catalogue ids and matching them surfaces nonsense. An exact
    word in the name beats a prefix, a prefix beats a substring, and among
    equals a shorter name wins -- so ``globe`` finds ``27-globe`` before
    ``1547-globe-honeymoon``.

    Ranking cannot rescue a bad term: the catalogue has no icon named for
    the planet, so ``earth`` finds a worm. That is why the trigger map
    carries the terms rather than the trigger's own name being the query.
    """
    term = term.strip().lower()
    if not term:
        return []
    scored: list[tuple[int, int, str, str]] = []
    for slug in slugs:
        name = slug.split("-", 1)[1] if "-" in slug else slug
        words = name.split("-")
        if term in words:
            rank = 0
        elif any(word.startswith(term) for word in words):
            rank = 1
        elif term in name:
            rank = 2
        else:
            continue
        scored.append((rank, len(words), name, slug))
    scored.sort()
    return [slug for _, _, _, slug in scored[:limit]]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_catalog.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/sticker_catalog.py tests/test_sticker_catalog.py
git commit -m "feat: search Lordicon's catalogue from its public sitemap"
```

---

### Task 2: Remembering a choice

**Files:**
- Modify: `engine/store.py` (the `SCHEMA` string, and a new method pair)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Store.choose_sticker(plan_id: str, trigger: str, slug: str) -> None` and `Store.sticker_choices(plan_id: str) -> dict[str, str]`. Tasks 4 and 5 use both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_a_sticker_choice_is_remembered_per_plan_and_trigger(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    assert store.sticker_choices("p1") == {}

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p1", "science", "440-dna")
    assert store.sticker_choices("p1") == {
        "death": "2130-skull-poison", "science": "440-dna"}


def test_choosing_again_replaces_rather_than_accumulates(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p1", "death", "2841-crashed-skull")
    assert store.sticker_choices("p1") == {"death": "2841-crashed-skull"}


def test_two_plans_choosing_the_same_trigger_do_not_collide(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p2", "death", "2816-skull-halloween")
    assert store.sticker_choices("p1") == {"death": "2130-skull-poison"}
    assert store.sticker_choices("p2") == {"death": "2816-skull-halloween"}


def test_the_choices_table_is_added_to_a_database_that_predates_it(tmp_path):
    """The same shape as the `attribution` column: an existing engine.db
    must gain the table on the next start, not on a fresh install only."""
    path = tmp_path / "old.db"
    store = Store(path)
    store.init()

    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE sticker_choices")

    store.init()
    assert store.sticker_choices("p1") == {}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_store.py -k sticker -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'choose_sticker'`.

- [ ] **Step 3: Add the table to `SCHEMA`**

In `engine/store.py`, inside the `SCHEMA` string, after the `assets` table:

```sql
CREATE TABLE IF NOT EXISTS sticker_choices (
  plan_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  slug TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, trigger)
);
```

`CREATE TABLE IF NOT EXISTS` inside `SCHEMA` is enough here — unlike a new
*column*, a new *table* is created on every `init()`, including on databases
that predate it. That is why this needs no entry in `ADDED_COLUMNS`.

- [ ] **Step 4: Add the two methods**

In `engine/store.py`, after the renders section:

```python
    # -- sticker choices --------------------------------------------------
    def choose_sticker(self, plan_id: str, trigger: str, slug: str) -> None:
        """Record which Lordicon icon this reel should use for ``trigger``.

        Per plan on purpose: the same trigger can wear different art in
        different reels, the way clips already do. Replacing rather than
        appending, because there is one answer per trigger per reel and a
        history of rejected picks would only have to be filtered out again.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sticker_choices(plan_id, trigger, slug, "
                "created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(plan_id, trigger) DO UPDATE SET "
                "slug=excluded.slug, created_at=excluded.created_at",
                (plan_id, trigger, slug, _now()))

    def sticker_choices(self, plan_id: str) -> dict[str, str]:
        """``{trigger: slug}`` for this plan, empty when nothing was chosen."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT trigger, slug FROM sticker_choices WHERE plan_id=?",
                (plan_id,)).fetchall()
        return {row["trigger"]: row["slug"] for row in rows}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/store.py tests/test_store.py
git commit -m "feat: remember which sticker art a reel chose"
```

---

### Task 3: The shared bake cache

**Files:**
- Create: `engine/assembly/sticker_choices.py`
- Test: `tests/test_sticker_choices.py`

**Interfaces:**
- Consumes: `sticker_catalog.gif_url`; `scripts.bake_stickers.bake_one`; `engine.assembly.sticker_art.matte`, `apply_style`, `ground`; `engine.assembly.stickers.STYLES`, `sticker_canvas`.
- Produces: `bake_key(slug, style, size, fps) -> str`, `cached_sequence(slug, style, *, fps, size, root) -> tuple[str, int, int] | None`, `ensure_baked(slug, *, root, size, fps) -> None`, `preview_png(slug, *, root, style="punchy", px=96) -> Path`. Tasks 4 and 5 use all four.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sticker_choices.py`:

```python
"""The cache that stands between a chosen slug and a rendered sticker.

Content-addressed on purpose: two reels that pick the same icon share one
bake, and picking an icon a second time costs nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from engine.assembly import sticker_choices as sc


def _gif(path: Path, frames: int = 9, side: int = 64) -> Path:
    """A GIF on a white ground, like the ones Lordicon serves.

    Colour varies per frame because Pillow merges byte-identical
    consecutive frames when writing a GIF, which would collapse this to a
    still.
    """
    from PIL import ImageDraw
    images = []
    for i in range(frames):
        im = Image.new("RGB", (side, side), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse((8, 8, side - 8, side - 8), fill=(20 + i * 3, 40, 60))
        images.append(im)
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=40, loop=0)
    return path


def test_the_key_separates_everything_that_changes_the_pixels():
    a = sc.bake_key("27-globe", "dark", 184, 30)
    assert a != sc.bake_key("27-globe", "punchy", 184, 30)
    assert a != sc.bake_key("27-globe", "dark", 60, 30)
    assert a != sc.bake_key("27-globe", "dark", 184, 60)
    assert a != sc.bake_key("1875-planet", "dark", 184, 30)
    assert a == sc.bake_key("27-globe", "dark", 184, 30)


def test_nothing_cached_resolves_to_none(tmp_path):
    assert sc.cached_sequence("27-globe", "dark", fps=30, size=60,
                              root=tmp_path) is None


def test_a_baked_slug_resolves_like_the_committed_art_does(tmp_path,
                                                           monkeypatch):
    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(sc, "_download", lambda slug, dest: src.replace(dest))

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)

    found = sc.cached_sequence("27-globe", "punchy", fps=30, size=60,
                               root=tmp_path)
    assert found is not None
    pattern, frames, canvas = found
    from engine.assembly.stickers import HOLD_SECONDS, sticker_canvas
    assert frames == round(HOLD_SECONDS * 30)
    assert canvas == sticker_canvas(60)
    assert Path(pattern % 0).exists()


def test_baking_the_same_slug_twice_does_not_download_twice(tmp_path):
    src = _gif(tmp_path / "src.gif")
    calls = []

    def fake_download(slug, dest):
        calls.append(slug)
        _gif(Path(dest))

    import engine.assembly.sticker_choices as mod
    mod._download = fake_download

    mod.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)
    mod.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)
    assert len(calls) == 1


def test_a_preview_is_one_frame_not_a_bake(tmp_path, monkeypatch):
    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(sc, "_download", lambda slug, dest: _gif(Path(dest)))

    png = sc.preview_png("27-globe", root=tmp_path, px=96)
    assert png.exists()
    with Image.open(png) as im:
        assert im.size == (96, 96)
        assert im.mode == "RGBA"
    # A preview must not have produced a sequence.
    assert not list((tmp_path / "bakes").rglob("frame-000.png"))


def test_an_icon_with_a_trapped_white_pocket_is_refused_by_name(tmp_path,
                                                                monkeypatch):
    from PIL import ImageDraw
    frames = []
    for i in range(4):
        im = Image.new("RGB", (64, 64), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse((8, 8, 56, 56), fill=(20 + i, 30, 40))
        d.ellipse((26, 26, 38, 38), fill=(255, 255, 255))
        frames.append(im)
    holed = tmp_path / "holed.gif"
    frames[0].save(holed, save_all=True, append_images=frames[1:],
                   duration=40, loop=0)

    monkeypatch.setattr(sc, "_download",
                        lambda slug, dest: holed.replace(Path(dest)))

    with pytest.raises(ValueError, match="2816-skull-halloween"):
        sc.ensure_baked("2816-skull-halloween", root=tmp_path, size=60,
                        fps=30)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_sticker_choices.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.assembly.sticker_choices'`.

- [ ] **Step 3: Write the module**

Create `engine/assembly/sticker_choices.py`:

```python
"""Turning a chosen Lordicon slug into frames the renderer can composite.

The cache is keyed by everything that changes the pixels -- slug, style,
size, fps -- and by nothing else, so two reels that pick the same icon share
one bake and picking it a second time is free. That is the whole reason a
choice stores a slug rather than a path.

Nothing here runs during a render. `ensure_baked` is called from the panel
when someone commits to an icon; by the time the render starts, the frames
are already on disk and `cached_sequence` is a directory listing.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

from engine.assembly.sticker_catalog import gif_url

BAKES_DIRNAME = "bakes"
PREVIEWS_DIRNAME = "previews"
SOURCES_DIRNAME = "sources"


def bake_key(slug: str, style: str, size: int, fps: int) -> str:
    """A directory name for one baked sequence.

    Hashed rather than concatenated because a slug is free-form text from a
    sitemap and this becomes a path. The slug is kept in front of the digest
    anyway, so a human can read the cache.
    """
    digest = hashlib.sha256(
        f"{slug}|{style}|{size}|{fps}".encode()).hexdigest()[:12]
    return f"{slug}-{style}-{digest}"


def _download(slug: str, dest: Path) -> None:
    """Fetch one icon's GIF. Split out so tests can replace it."""
    urllib.request.urlretrieve(gif_url(slug), dest)


def _source(slug: str, root: Path) -> Path:
    """The cached source GIF, downloaded on first use."""
    folder = Path(root) / SOURCES_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{slug}.gif"
    if not dest.exists():
        staged = dest.with_suffix(".part")
        _download(slug, staged)
        staged.replace(dest)
    return dest


def cached_sequence(slug: str, style: str, *, fps: int, size: int,
                    root: Path) -> tuple[str, int, int] | None:
    """``(pattern, frames, canvas)`` for a chosen icon, or None.

    Returns None for every kind of absence -- never baked, cache cleaned,
    baked at another size -- so the caller falls to the rung below rather
    than rendering something wrong. The same contract as
    ``stickers.baked_sequence``, deliberately: the caller treats them alike.
    """
    # Not reusing ``stickers.baked_sequence``: it expects a
    # ``<trigger>/<style>`` layout under one root, and this cache is keyed
    # flat by content so two reels can share a bake. The checks are the
    # same ones, in ``_resolve`` below.
    folder = Path(root) / BAKES_DIRNAME / bake_key(slug, style, size, fps)
    return _resolve(folder, fps=fps, size=size)


def _resolve(folder: Path, *, fps: int, size: int
             ) -> tuple[str, int, int] | None:
    import json

    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        frames = int(meta["frames"])
        canvas = int(meta["canvas"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if int(meta.get("fps", -1)) != fps or int(meta.get("size", -1)) != size:
        return None
    if frames <= 0 or len(list(folder.glob("frame-*.png"))) != frames:
        return None
    return str(folder / "frame-%03d.png"), frames, canvas


def ensure_baked(slug: str, *, root: Path, size: int, fps: int) -> None:
    """Bake both styles of one icon into the cache, if they are not there.

    Raises ``ValueError`` when the icon has a pocket of trapped white --
    ``bake_one``'s own refusal, passed straight through, because an icon
    that would render with a white blob in it is a choice to reject rather
    than a failure to swallow.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from engine.assembly.stickers import STYLES
    from scripts.bake_stickers import bake_one

    root = Path(root)
    source = _source(slug, root)
    for style in STYLES:
        folder = root / BAKES_DIRNAME / bake_key(slug, style, size, fps)
        if _resolve(folder, fps=fps, size=size) is not None:
            continue
        bake_one(source, folder, style=style, size=size, fps=fps)


def preview_png(slug: str, *, root: Path, style: str = "punchy",
                px: int = 96) -> Path:
    """One matted, graded frame of an icon, for browsing.

    A bake is 42 frames in two styles and takes about sixteen seconds. A
    preview is one frame and takes a fraction of one, and browsing has to be
    cheap or nobody browses.
    """
    from PIL import Image

    from engine.assembly.sticker_art import apply_style, ground, matte

    root = Path(root)
    folder = root / PREVIEWS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{slug}-{style}-{px}.png"
    if dest.exists():
        return dest

    source = _source(slug, root)
    with Image.open(source) as src:
        src.seek(getattr(src, "n_frames", 1) // 2)
        frame = src.convert("RGB")
    art = ground(apply_style(matte(frame), style), style)
    art.resize((px, px), Image.LANCZOS).save(dest)
    return dest
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_choices.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Prove the refusal test can fail**

Temporarily make `ensure_baked` swallow the `ValueError` from `bake_one`.
Run `test_an_icon_with_a_trapped_white_pocket_is_refused_by_name` and watch
it FAIL. Restore, watch it pass. Put both outputs in your report.

- [ ] **Step 6: Commit**

```bash
git add engine/assembly/sticker_choices.py tests/test_sticker_choices.py
git commit -m "feat: cache a chosen icon's bake, keyed by what changes its pixels"
```

---

### Task 4: Resolving a choice in `prepare()`

**Files:**
- Modify: `engine/assembly/stickers.py` (`prepare`)
- Test: `tests/test_stickers.py`

**Interfaces:**
- Consumes: `sticker_choices.cached_sequence`.
- Produces: `prepare(plan, settings, *, cache_dir=None, choices: dict[str, str] | None = None)`. Task 5 passes `choices`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stickers.py`, above the real-renders banner:

```python
# --- chosen art ------------------------------------------------------------

def test_a_chosen_slug_wins_over_the_committed_art(tmp_path, monkeypatch):
    from engine.assembly import sticker_choices as sc
    from scripts.bake_stickers import bake_one

    plan = _timed(beats=4, captions=[
        "Jungle mein ek kankaal mila tha"] * 4)
    settings = shipped_settings()
    settings.work_dir = tmp_path
    size = stk.sticker_size(settings.width, settings.sticker_scale)

    # Bake a stand-in for the chosen slug straight into the cache, so this
    # test needs no network.
    src = Path("assets/lordicon/witness.gif")
    if not src.exists():                       # pragma: no cover - env
        pytest.skip("run scripts/fetch_sticker_art.py first")
    key = sc.bake_key("2813-creepy-eye-ball", "punchy", size,
                      int(settings.fps))
    bake_one(src, tmp_path / sc.BAKES_DIRNAME / key, style="punchy",
             size=size, fps=int(settings.fps))

    chosen = stk.prepare(plan, settings,
                         choices={"death": "2813-creepy-eye-ball"})
    assert chosen, "expected a sticker"
    assert chosen[0].baked is True
    assert sc.bake_key("2813-creepy-eye-ball", "punchy", size,
                       int(settings.fps)) in chosen[0].pattern


def test_no_choices_behaves_exactly_as_before(tmp_path):
    plan = _timed(beats=4, captions=[
        "Jungle mein ek kankaal mila tha"] * 4)
    settings = shipped_settings()
    settings.work_dir = tmp_path

    without = stk.prepare(plan, settings)
    with_empty = stk.prepare(plan, settings, choices={})
    with_none = stk.prepare(plan, settings, choices=None)
    assert [s.pattern for s in without] == [s.pattern for s in with_empty]
    assert [s.pattern for s in without] == [s.pattern for s in with_none]


def test_a_choice_whose_cache_is_gone_falls_through_and_does_not_raise(
        tmp_path):
    plan = _timed(beats=4, captions=[
        "Jungle mein ek kankaal mila tha"] * 4)
    settings = shipped_settings()
    settings.work_dir = tmp_path

    prepared = stk.prepare(plan, settings,
                           choices={"death": "9999-never-baked"})
    assert prepared, "must fall back, not vanish"
    # Falls to the committed art, which is still baked designed art.
    assert prepared[0].baked is True


def test_a_choice_for_a_trigger_that_did_not_fire_is_ignored(tmp_path):
    plan = _timed(beats=4, captions=[
        "Jungle mein ek kankaal mila tha"] * 4)
    settings = shipped_settings()
    settings.work_dir = tmp_path

    prepared = stk.prepare(plan, settings,
                           choices={"mountain": "1875-planet"})
    assert [s.name for s in prepared] == ["death"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_stickers.py -k chosen -v`
Expected: FAIL with `TypeError: prepare() got an unexpected keyword argument 'choices'`.

- [ ] **Step 3: Add the rung**

In `engine/assembly/stickers.py`, change `prepare`'s signature and the
resolution inside its loop:

```python
def prepare(plan: ReelPlan, settings, *,
            cache_dir: str | Path | None = None,
            choices: dict[str, str] | None = None) -> list[Sticker]:
```

Extend the docstring with a paragraph:

```
    ``choices`` maps a trigger to a Lordicon slug someone picked for this
    reel in the panel. It is the top rung: a chosen icon beats the committed
    art, which beats the emoji glyph. A slug whose bake is missing falls
    through to the rung below rather than failing, so cleaning the cache
    costs a nicer sticker and never a render.
```

Then, inside the loop, replace the `found = baked_sequence(...)` line:

```python
        found = None
        chosen = (choices or {}).get(cue.name)
        if chosen:
            from engine.assembly.sticker_choices import cached_sequence
            found = cached_sequence(
                chosen, style, fps=fps, size=size,
                root=Path(getattr(settings, "work_dir", ".")))
        if found is None:
            found = baked_sequence(cue.name, style, fps=fps, size=size)
```

The rest of the loop body is unchanged: `found is not None` still decides
`baked`, so a chosen icon is designed art and still owes the Lordicon credit.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_stickers.py tests/test_sticker_art.py tests/test_audio.py -v`
Expected: PASS. Every pre-existing test must still pass untouched.

- [ ] **Step 5: Prove the top rung is really on top**

Temporarily reverse the order — try `baked_sequence` first and the choice
second. Run `test_a_chosen_slug_wins_over_the_committed_art` and watch it
FAIL. Restore, watch it pass. Both outputs in your report.

- [ ] **Step 6: Commit**

```bash
git add engine/assembly/stickers.py tests/test_stickers.py
git commit -m "feat: let a chosen icon outrank the committed art"
```

---

### Task 5: The two routes

**Files:**
- Modify: `engine/app.py` (two new routes beside the clip picker, plus one preview route)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `sticker_catalog.refresh/load/search`, `sticker_choices.ensure_baked/preview_png`, `Store.choose_sticker/sticker_choices`, `stickers.find_cues`.
- Produces: three routes. Nothing later depends on them in this plan.

**Before you write anything:** run `git status`. `engine/app.py` is shared with another Claude session. If it has uncommitted changes, stop and report rather than editing around them.

- [ ] **Step 1: Add search terms to the trigger map**

In `engine/data/stickers.json`, add a `search` array to each of the five
triggers that already carry `art`. The terms are what the catalogue is
queried with — the trigger's own name is a bad query, which is why these are
data:

| trigger | search |
|---------|--------|
| `death` | `["skull", "bone", "grave"]` |
| `question` | `["question", "chat-question"]` |
| `science` | `["dna", "molecule", "microscope"]` |
| `time` | `["clock", "calendar", "history"]` |
| `witness` | `["eye", "eyeball"]` |

Add the same to the ten without art, so the picker works for them too:

| trigger | search |
|---------|--------|
| `water` | `["water", "wave", "lake", "drop"]` |
| `fire` | `["fire", "flame"]` |
| `money` | `["money", "coin", "treasure"]` |
| `ghost` | `["ghost"]` |
| `secret` | `["lock", "secret", "hidden"]` |
| `shock` | `["shock", "warning", "alert"]` |
| `danger` | `["danger", "warning", "hazard"]` |
| `night` | `["moon", "night"]` |
| `mountain` | `["mountain", "hill"]` |
| `location` | `["location", "pin", "map"]` |

In `engine/assembly/stickers.py`, parse it into `Trigger`:

```python
    search: tuple[str, ...] = ()
```

and in `load_triggers`:

```python
            search=tuple(t.strip().lower()
                         for t in entry.get("search", []) if t),
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_sticker_candidates_lists_one_row_per_fired_cue(tmp_path, monkeypatch):
    from engine.assembly import sticker_catalog as cat
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: (
        "2130-skull-poison", "2841-crashed-skull", "1195-earthworm"))

    body = client.get(f"/api/plan/{plan_id}/stickers").json()

    assert body["rows"], "a cue should have fired"
    row = body["rows"][0]
    assert row["trigger"] == "death"
    assert row["word"]
    assert row["candidates"]
    assert all("slug" in c and "preview" in c for c in row["candidates"])
    assert "1195-earthworm" not in [c["slug"] for c in row["candidates"]]


def test_sticker_candidates_say_so_when_the_catalogue_is_unreachable(
        tmp_path, monkeypatch):
    from engine.assembly import sticker_catalog as cat
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    def boom(*a, **k):
        raise cat.CatalogUnavailable("no route to host")

    monkeypatch.setattr(cat, "refresh", boom)

    body = client.get(f"/api/plan/{plan_id}/stickers").json()
    assert body["rows"] == [] or all(
        r["candidates"] == [] for r in body["rows"])
    assert "no route to host" in body.get("note", "")


def test_choosing_a_slug_outside_the_catalogue_is_refused_without_fetching(
        tmp_path, monkeypatch):
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: ("2130-skull-poison",))

    fetched = []
    monkeypatch.setattr(sc, "ensure_baked",
                        lambda *a, **k: fetched.append(a))

    resp = client.post(f"/api/plan/{plan_id}/sticker/death",
                       json={"slug": "http://evil.test/x.gif"})
    assert resp.status_code == 400
    assert fetched == [], "nothing may be fetched for an unknown slug"
    assert store.sticker_choices(plan_id) == {}


def test_a_refused_icon_leaves_the_previous_choice_alone(tmp_path,
                                                          monkeypatch):
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: (
        "2130-skull-poison", "2816-skull-halloween"))
    monkeypatch.setattr(sc, "ensure_baked", lambda *a, **k: None)
    client.post(f"/api/plan/{plan_id}/sticker/death",
                json={"slug": "2130-skull-poison"})

    def holed(*a, **k):
        raise ValueError("2816-skull-halloween has a pocket of white")

    monkeypatch.setattr(sc, "ensure_baked", holed)
    resp = client.post(f"/api/plan/{plan_id}/sticker/death",
                       json={"slug": "2816-skull-halloween"})

    assert resp.status_code == 422
    assert "pocket of white" in resp.json()["detail"]
    assert store.sticker_choices(plan_id) == {"death": "2130-skull-poison"}
```

`_approved_plan_with_voice` is a helper you write: build a plan whose caption
fires `death`, give its beats word timings with
`engine.media.voice.caption_timings`, save it, and return
`(client, store, plan_id)`. Model it on whatever the neighbouring tests in
that file already do to get a client and a stored plan; do not invent a new
app fixture if one exists.

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m pytest tests/test_app.py -k sticker -v`
Expected: FAIL with 404s — the routes do not exist.

- [ ] **Step 4: Write the routes**

In `engine/app.py`, beside the clip picker routes:

```python
    class StickerChoice(BaseModel):
        slug: str = Field(min_length=1, max_length=120)

    @app.get("/api/plan/{plan_id}/stickers")
    def sticker_candidates(plan_id: str) -> dict:
        """Every sticker that fired, with icons that could replace it.

        Per fired cue, not per trigger: a trigger that did not fire has
        nothing to show, and showing it anyway would invite choosing art for
        a sticker this reel will never render.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")

        cache = Path(settings.work_dir) / "_lordicon"
        note = ""
        slugs: tuple[str, ...] = ()
        try:
            sticker_catalog.refresh(cache)
            slugs = sticker_catalog.load(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            # A picker that cannot list icons is an inconvenience. The
            # committed art still renders, so this is a note, not an error.
            note = f"catalogue unavailable: {exc}"

        chosen = store.sticker_choices(plan_id)
        triggers = {t.name: t for t in stickers_mod.load_triggers()}

        rows = []
        for cue in stickers_mod.find_cues(plan,
                                          cap=int(settings.sticker_max)):
            trigger = triggers.get(cue.name)
            seen: list[str] = []
            for term in (trigger.search if trigger else ()):
                for slug in sticker_catalog.search(term, slugs):
                    if slug not in seen:
                        seen.append(slug)
            rows.append({
                "trigger": cue.name,
                "word": cue.word,
                "start": round(cue.start, 2),
                "beat_id": plan.script.beats[cue.beat_index].beat_id,
                "chosen": chosen.get(cue.name),
                "candidates": [
                    {"slug": slug,
                     "preview": f"/api/sticker-preview/{slug}"}
                    for slug in seen[:8]],
                "choose": f"/api/plan/{plan_id}/sticker/{cue.name}",
            })
        return {"plan_id": plan_id, "rows": rows, "note": note}

    @app.get("/api/sticker-preview/{slug}")
    def sticker_preview(slug: str) -> FileResponse:
        """One matted frame of an icon, for the picker to show."""
        cache = Path(settings.work_dir) / "_lordicon"
        slugs = sticker_catalog.load(cache)
        if slug not in slugs:
            raise HTTPException(404, "not a catalogue slug")
        try:
            png = sticker_choices_mod.preview_png(slug, root=cache)
        except Exception as exc:
            raise HTTPException(502, f"preview failed: {exc}") from exc
        return FileResponse(png, media_type="image/png")

    @app.post("/api/plan/{plan_id}/sticker/{trigger}")
    def choose_sticker(plan_id: str, trigger: str,
                       body: StickerChoice) -> dict:
        """Bake one icon for this reel and remember it.

        The slug is checked against the catalogue before anything is
        fetched. Without that check this route is an arbitrary URL fetcher
        with the panel's network access.
        """
        if store.get_plan(plan_id) is None:
            raise HTTPException(404, "no such plan")

        cache = Path(settings.work_dir) / "_lordicon"
        try:
            sticker_catalog.refresh(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            raise HTTPException(503, f"catalogue unavailable: {exc}") from exc
        if body.slug not in sticker_catalog.load(cache):
            raise HTTPException(400, "not a catalogue slug")

        size = stickers_mod.sticker_size(settings.width,
                                         settings.sticker_scale)
        try:
            sticker_choices_mod.ensure_baked(
                body.slug, root=cache, size=size, fps=int(settings.fps))
        except ValueError as exc:
            # The icon has a pocket of trapped white and would render with a
            # blob in it. Refusing keeps whatever was chosen before.
            raise HTTPException(422, str(exc)) from exc

        store.choose_sticker(plan_id, trigger, body.slug)
        return {"plan_id": plan_id, "trigger": trigger, "slug": body.slug,
                "preview": f"/api/sticker-preview/{body.slug}"}
```

Add the imports at the top of `engine/app.py`:

```python
from engine.assembly import sticker_catalog
from engine.assembly import sticker_choices as sticker_choices_mod
```

and confirm `FileResponse` is imported from `fastapi.responses` — the media
route nearby will already have it; reuse rather than re-import.

- [ ] **Step 5: Pass the choices into the render**

`build_command` calls `stickers_mod.prepare(plan, settings)` at
`engine/assembly/render.py:573`, and `render()` at `:618` already threads a
`report` dict down to it. `choices` takes exactly the same path — mirror
`report` rather than inventing a second mechanism.

Three edits:

`engine/assembly/render.py`, `build_command`'s signature and its `prepare` call:

```python
def build_command(plan: ReelPlan, settings, out_path: Path, *,
                  ...,
                  report: dict | None = None,
                  choices: dict[str, str] | None = None
                  ) -> list[str]:
```
```python
    prepared = stickers_mod.prepare(plan, settings, choices=choices)
```

`engine/assembly/render.py`, `render`'s signature and its `build_command` call:

```python
def render(plan: ReelPlan, settings, out_path: str | Path, *,
           ass_path: str | None = None, music_path: str | None = None,
           progress=None, report: dict | None = None,
           choices: dict[str, str] | None = None) -> str:
```

passing `choices=choices` through, beside the existing `report=report`.

`engine/pipeline.py`, `render_stage`, beside the `used: dict = {}` line the
attribution work added:

```python
    # What the panel picked for this reel, if anything. Read once here
    # rather than inside the render, because the render takes a plan and
    # settings and has no store to ask.
    choices = store.sticker_choices(plan.plan_id)
```

and pass `choices=choices` in the `render(...)` call.

**`engine/pipeline.py` is shared with another session** — run `git status`
first. If it has uncommitted changes, stop and report rather than editing
around them. Report exactly what you changed there either way.

Add a test to `tests/test_stickers.py` that this hand-off is real, in the
shape of the one that guards `report`:

```python
def test_render_passes_a_chosen_slug_all_the_way_down(tmp_path,
                                                       monkeypatch):
    """render() -> build_command() -> prepare(choices=...).

    Cutting any link here silently reverts every reel to the committed art
    while the suite stays green, which is the same hole the `report`
    hand-off test exists to close.
    """
    from engine.assembly import render as render_mod

    seen = {}
    real = stk.prepare

    def spy(plan, settings, **kwargs):
        seen["choices"] = kwargs.get("choices")
        return real(plan, settings, **kwargs)

    monkeypatch.setattr(render_mod.stickers_mod, "prepare", spy)

    settings = _render_settings(tmp_path)
    plan = _sticker_plan(tmp_path, settings)
    render_mod.render(plan, settings, tmp_path / "o.mp4",
                      choices={"death": "2130-skull-poison"})

    assert seen["choices"] == {"death": "2130-skull-poison"}
```

Prove it too: drop `choices=choices` from the `build_command` call, watch it
fail, restore, watch it pass.

- [ ] **Step 6: Run everything**

Run: `python -m pytest tests/test_app.py tests/test_stickers.py tests/test_sticker_art.py tests/test_sticker_choices.py tests/test_sticker_catalog.py tests/test_store.py tests/test_audio.py -v`
Expected: PASS.

- [ ] **Step 7: Prove the slug check can fail**

Temporarily remove the `if body.slug not in sticker_catalog.load(cache)`
guard. Run `test_choosing_a_slug_outside_the_catalogue_is_refused_without_fetching`
and watch it FAIL. Restore, watch it pass. Both outputs in your report — this
is the one that keeps the route from fetching arbitrary URLs.

- [ ] **Step 8: Commit**

```bash
git add engine/app.py engine/assembly/stickers.py engine/data/stickers.json \
        engine/pipeline.py tests/test_app.py
git commit -m "feat: offer and record sticker art choices from the panel"
```

---

### Task 6: Prove it against the real catalogue

**Files:** none — an acceptance check.

- [ ] **Step 1: Fetch the real catalogue and search it**

```bash
python -c "
from pathlib import Path
from engine.assembly import sticker_catalog as cat
cache = Path('outputs/_lordicon'); cat.refresh(cache)
slugs = cat.load(cache); print(len(slugs), 'icons')
for term in ['skull','dna','clock','eye','water','fire','moon']:
    print(f'{term:<8}', cat.search(term, slugs)[:5])
"
```

Expected: about 3,578 icons, and each term returning plausible slugs.

- [ ] **Step 2: Look at what the picker would offer**

Render a contact sheet of the first five candidates for `water` and `fire` —
the two triggers with no committed art and the ones most likely to be picked
first — using `sticker_choices.preview_png`. Open it.

- [ ] **Step 3: Report, do not quietly tune**

Say whether the candidates are worth offering. If a term returns junk, the
fix is the `search` array in `engine/data/stickers.json`, which is data — say
which term and what you would change it to, and let Alex decide.

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
    _sitemap(tmp_path / cat.CACHE_NAME)
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


def test_a_corrupt_cache_reads_as_no_catalogue(tmp_path):
    """An unreadable cache is the same answer as a missing one.

    The panel shows no candidates and the committed art still renders;
    raising here would take a screen down over a file the next refresh
    replaces anyway.
    """
    (tmp_path / cat.CACHE_NAME).write_bytes(b"\xff\xfe not utf-8 at all")
    assert cat.load(tmp_path) == ()


def test_a_failed_move_is_named_and_leaves_no_stage_behind(tmp_path,
                                                            monkeypatch):
    def fine(url, dest):
        Path(dest).write_text("<urlset></urlset>", encoding="utf-8")

    def cannot_move(self, target):
        raise PermissionError("destination is locked")

    monkeypatch.setattr(cat.urllib.request, "urlretrieve", fine)
    monkeypatch.setattr(Path, "replace", cannot_move)

    with pytest.raises(cat.CatalogUnavailable, match="locked"):
        cat.refresh(tmp_path)
    assert not list(tmp_path.glob("*.part")), "staging file left behind"

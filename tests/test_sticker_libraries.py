"""Both icon libraries behind one interface.

The picker asks two questions -- "what matches this word?" and "is this a
real icon?" -- and neither answer should depend on the caller knowing how
many libraries exist. One place knows; the routes do not.
"""

from __future__ import annotations

import json

import pytest

from engine.assembly import noto_catalog, sticker_catalog
from engine.assembly import sticker_libraries as libs

LORDICON_SLUGS = ("1414-circle", "27-globe", "2130-skull-poison",
                  "2816-skull-halloween", "440-dna")
NOTO_MANIFEST = {"icons": [
    {"codepoint": "1f480", "popularity": 500, "tags": [":skull:"],
     "categories": ["Smileys and emotions"]},
    {"codepoint": "1f30d", "popularity": 200, "tags": [":globe-showing-europe-africa:"],
     "categories": ["Travel and places"]},
]}


@pytest.fixture()
def cache(tmp_path):
    locs = "".join(
        f"<url><loc>https://lordicon.com/icons/wired/flat/{s}</loc></url>"
        for s in LORDICON_SLUGS)
    (tmp_path / sticker_catalog.CACHE_NAME).write_text(
        f"<urlset>{locs}</urlset>", encoding="utf-8")
    (tmp_path / noto_catalog.CACHE_NAME).write_text(
        json.dumps(NOTO_MANIFEST), encoding="utf-8")
    return tmp_path


def test_search_returns_qualified_slugs_from_both(cache):
    found = libs.search("skull", cache)
    assert any(s.startswith("lordicon:") for s in found), found
    assert any(s.startswith("noto:") for s in found), found


def test_search_interleaves_rather_than_letting_one_library_win(cache):
    """Lordicon has 3,578 icons and Noto 881, so taking the best eight
    overall would bury the smaller library. A picker that only ever shows
    one of them is a picker with one library."""
    found = libs.search("skull", cache, limit=4)
    assert found[0].split(":")[0] != found[1].split(":")[0]


def test_a_term_neither_library_has_returns_nothing(cache):
    assert libs.search("farewell", cache) == []


def test_known_accepts_a_real_icon_from_either_library(cache):
    assert libs.known("lordicon:27-globe", cache)
    assert libs.known("noto:1f480", cache)


def test_known_refuses_an_icon_that_is_not_in_its_catalogue(cache):
    """The gate that stops the choose route fetching an arbitrary URL. A
    well-formed slug that simply is not in the manifest must fail."""
    assert not libs.known("noto:9999", cache)
    assert not libs.known("lordicon:9999-not-real", cache)


def test_known_refuses_a_slug_from_the_wrong_library(cache):
    """`1f480` is a real Noto codepoint and not a Lordicon slug. Checking
    it against the wrong catalogue must not pass it."""
    assert not libs.known("lordicon:1f480", cache)


def test_known_refuses_a_malformed_slug_without_raising(cache):
    for hostile in ("noto:../../evil", "elsewhere:1f480", "", "a b"):
        assert not libs.known(hostile, cache)


def test_a_bare_slug_is_still_read_as_lordicon(cache):
    assert libs.known("27-globe", cache)

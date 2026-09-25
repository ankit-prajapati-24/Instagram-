"""The Noto animated-emoji catalogue: fetched once, searched locally.

Shaped to match `sticker_catalog` so the picker can hold both libraries
without caring which one a candidate came from. Nothing here talks to the
network except `refresh`, and its test writes the manifest itself.

Why a second library at all: Lordicon's names are objects, so a script that
asks for "dream" or "farewell" finds nothing. Noto's entries carry tags and
categories, which reach some of the same ideas from the other side.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.assembly import noto_catalog as noto

MANIFEST = {
    "host": "https://fonts.gstatic.com",
    "icons": [
        {"name": "emoji_u1f480", "codepoint": "1f480", "popularity": 500,
         "categories": ["Smileys and emotions"], "tags": [":skull:"]},
        {"name": "emoji_u2620_fe0f", "codepoint": "2620_fe0f", "popularity": 90,
         "categories": ["Symbols"], "tags": [":skull-and-crossbones:"]},
        {"name": "emoji_u1f4a7", "codepoint": "1f4a7", "popularity": 300,
         "categories": ["Animals and nature"], "tags": [":droplet:"]},
        {"name": "emoji_u1f319", "codepoint": "1f319", "popularity": 400,
         "categories": ["Animals and nature"], "tags": [":crescent-moon:"]},
    ],
}


def _cache(tmp_path) -> Path:
    (tmp_path / noto.CACHE_NAME).write_text(json.dumps(MANIFEST),
                                            encoding="utf-8")
    return tmp_path


def test_the_gif_url_is_built_from_the_codepoint():
    assert noto.gif_url("1f480") == (
        "https://fonts.gstatic.com/s/e/notoemoji/latest/1f480/512.gif")


def test_search_finds_an_emoji_by_its_tag(tmp_path):
    icons = noto.load(_cache(tmp_path))
    assert noto.search("skull", icons)[0] == "1f480"


def test_the_plain_thing_outranks_the_compound(tmp_path):
    """`skull` must find the skull before skull-and-crossbones, the same
    rule `sticker_catalog.search` uses for `globe` over `globe-honeymoon`."""
    icons = noto.load(_cache(tmp_path))
    found = noto.search("skull", icons)
    assert found.index("1f480") < found.index("2620_fe0f")


def test_a_term_that_matches_nothing_returns_nothing(tmp_path):
    icons = noto.load(_cache(tmp_path))
    assert noto.search("farewell", icons) == []


def test_a_multi_word_term_finds_nothing(tmp_path):
    """Single words only, the same contract as the Lordicon catalogue, so
    a beat's `terms` list works against either library unchanged."""
    icons = noto.load(_cache(tmp_path))
    assert noto.search("crescent moon", icons) == []


def test_a_corrupt_manifest_reads_as_an_empty_catalogue(tmp_path):
    """A cache we cannot parse is a cache we do not have. Raising here
    would take the picker down over a file the next refresh rewrites."""
    (tmp_path / noto.CACHE_NAME).write_text("{ not json", encoding="utf-8")
    assert noto.load(tmp_path) == ()


def test_load_without_a_cache_is_empty_not_an_error(tmp_path):
    assert noto.load(tmp_path) == ()


def test_refresh_does_not_refetch_what_is_already_cached(tmp_path,
                                                         monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("refresh went to the network with a warm cache")

    _cache(tmp_path)
    monkeypatch.setattr(noto.urllib.request, "urlretrieve", boom)
    noto.refresh(tmp_path)
    assert not calls


def test_the_real_catalogue_reaches_words_lordicon_cannot(tmp_path):
    """The reason for the second library, measured rather than asserted.

    Skips when the manifest has not been fetched; the synthetic tests above
    still hold the ranking rules.
    """
    cache = Path("work/_lordicon")
    if not (cache / noto.CACHE_NAME).exists():
        pytest.skip("noto manifest not cached; run noto_catalog.refresh")
    icons = noto.load(cache)
    assert len(icons) > 800, f"only {len(icons)} icons; manifest looks short"
    for word in ("skull", "fire", "moon", "eyes", "money"):
        assert noto.search(word, icons), f"{word} found nothing"

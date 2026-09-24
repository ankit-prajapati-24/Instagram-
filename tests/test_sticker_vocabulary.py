"""What the icon catalogue can and cannot be asked for.

Measured against the real cached sitemap, not a synthetic one: the claim is
about the shape of 3,578 real icon names, and ten fixtures cannot carry it.
Skips rather than fetches when the cache is cold -- a test that pulls 5.4MB
is a test people learn to skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.assembly import sticker_catalog as cat

# Every word on the left is an abstract noun and finds nothing; every word on
# the right is a thing you could photograph and finds an icon. This is why
# `sticker.terms` is a list of concrete objects rather than one word for the
# idea, and why the prompt forbids abstract nouns.
ABSTRACT = ("dream", "journey", "memory", "farewell", "goodbye", "job",
            "interview", "resume", "effort", "strength", "college",
            "friendship", "study", "failure", "luggage")
CONCRETE = ("cloud", "star", "road", "train", "photo", "camera",
            "briefcase", "office", "book", "graduation", "student",
            "code", "laptop", "heart", "muscle", "sad")


@pytest.fixture(scope="module")
def catalogue():
    cache = Path("work/_lordicon")
    if not (cache / cat.CACHE_NAME).exists():
        pytest.skip("catalogue not cached; run sticker_catalog.refresh first")
    slugs = cat.load(cache)
    assert len(slugs) > 3000, f"only {len(slugs)} slugs; cache looks truncated"
    return slugs


def test_the_catalogue_is_named_for_things_not_for_ideas(catalogue):
    hit = [w for w in ABSTRACT if cat.search(w, catalogue)]
    missed = [w for w in CONCRETE if not cat.search(w, catalogue)]
    assert not hit, f"these abstract words unexpectedly hit: {hit}"
    assert not missed, f"these concrete words unexpectedly missed: {missed}"


def test_a_multi_word_term_finds_nothing(catalogue):
    """`search` ranks against hyphen-separated slug name parts, which is why
    `terms` is a list of single words rather than one phrase."""
    assert cat.search("moon night", catalogue) == []

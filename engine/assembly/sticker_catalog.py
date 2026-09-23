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

"""Google's animated Noto emoji: the manifest, cached, and a search over it.

Deliberately the same shape as ``sticker_catalog`` -- ``refresh``, ``load``,
``search``, ``gif_url`` -- so the picker can offer both libraries without
knowing which one a candidate came from.

Why a second library. Lordicon's 3,578 icons are named for objects, which is
what makes them findable and also what makes them miss: a script that asks
for "dream", "journey" or "farewell" gets nothing back, because nobody drew
an icon called that. Noto's 881 entries carry tags and category names, which
reach some of those ideas from the other side.

What it costs. The art is CC BY 4.0, so a reel that uses one owes Google a
credit -- the same obligation Lordicon's free tier carries, not a saving.
That credit is recorded per bake in its ``meta.json`` and read back at
render time, never inferred.

Nothing here talks to the network except ``refresh``.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CATALOG_URL = ("https://googlefonts.github.io/noto-emoji-animation/"
               "data/api.json")
CACHE_NAME = "noto-emoji.json"

# The same predictable pattern ``sticker_catalog.gif_url`` relies on for
# Lordicon: a codepoint is enough to build the URL, so nothing has to be
# stored beyond the manifest itself.
ASSET_ROOT = "https://fonts.gstatic.com/s/e/notoemoji/latest"

# What a reel owes for using this art. Spelled here beside the catalogue it
# describes; ``stickers.LICENCES`` carries the copy the render records.
LICENCE = "Animated emoji by Google, Noto Emoji (CC BY 4.0)"


class CatalogUnavailable(RuntimeError):
    """The manifest could not be fetched. The picker says so and carries
    on with whatever it already has; a render never needs this."""


@dataclass(frozen=True)
class Icon:
    """One animated emoji, as much of it as searching needs."""

    codepoint: str
    tags: tuple[str, ...]        # ":skull:" arrives, "skull" is stored
    categories: tuple[str, ...]
    popularity: int


def gif_url(codepoint: str) -> str:
    """The animation's URL. The same 512px GIF the bake already consumes,
    so nothing downstream has to learn a new format."""
    return f"{ASSET_ROOT}/{codepoint}/512.gif"


def refresh(cache_dir: str | Path, *, force: bool = False) -> Path:
    """Fetch the manifest once and cache it. Returns the cached path.

    A warm cache is left alone, because this is called on every visit to
    the picker and the catalogue changes about as often as Google ships
    emoji.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / CACHE_NAME
    if target.exists() and not force:
        return target
    part = target.with_suffix(target.suffix + ".part")
    try:
        urllib.request.urlretrieve(CATALOG_URL, part)
    except Exception as exc:                       # noqa: BLE001
        part.unlink(missing_ok=True)
        raise CatalogUnavailable(
            f"could not fetch the Noto manifest: {exc}") from exc
    # Renamed last, so a half-written download is never a cache hit.
    part.replace(target)
    return target


def load(cache_dir: str | Path) -> tuple[Icon, ...]:
    """Every icon in the cached manifest, or ``()``.

    Empty rather than raising for every kind of absence -- no cache, a
    truncated download, a manifest whose shape moved. A picker with no
    candidates is an inconvenience; one that raises takes the board down.
    """
    path = Path(cache_dir) / CACHE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw["icons"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()

    icons: list[Icon] = []
    for entry in entries:
        try:
            codepoint = str(entry["codepoint"]).strip().lower()
        except (KeyError, TypeError):
            continue
        if not codepoint:
            continue
        tags = tuple(
            str(t).strip().strip(":").lower()
            for t in (entry.get("tags") or []) if str(t).strip())
        cats = tuple(
            str(c).strip().lower()
            for c in (entry.get("categories") or []) if str(c).strip())
        try:
            popularity = int(entry.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0
        icons.append(Icon(codepoint, tags, cats, popularity))
    return tuple(icons)


def search(term: str, icons: tuple[Icon, ...], *, limit: int = 8) -> list[str]:
    """Codepoints matching ``term``, best first.

    One word, like ``sticker_catalog.search``: a beat's ``terms`` list is
    written once and has to work against either library, and matching a
    phrase against hyphenated tags would find nothing anyway.

    An exact word in a tag beats a prefix, a prefix beats a category, and
    among equals the plainer tag wins -- so ``skull`` finds ``:skull:``
    before ``:skull-and-crossbones:``. Popularity breaks the last tie,
    because the common emoji is the one a viewer reads fastest.
    """
    term = term.strip().lower()
    if not term or " " in term:
        return []

    scored: list[tuple[int, int, int, str]] = []
    for icon in icons:
        rank = None
        words = 99
        for tag in icon.tags:
            parts = tag.split("-")
            if term in parts:
                rank, words = 0, min(words, len(parts))
            elif rank is None or rank > 1:
                if any(p.startswith(term) for p in parts):
                    rank, words = 1, min(words, len(parts))
        if rank is None:
            for category in icon.categories:
                if term in category.split():
                    rank, words = 2, 1
                    break
        if rank is None:
            continue
        # Negated popularity: the sort is ascending and the common emoji
        # should come first.
        scored.append((rank, words, -icon.popularity, icon.codepoint))

    scored.sort()
    return [codepoint for _, _, _, codepoint in scored[:limit]]

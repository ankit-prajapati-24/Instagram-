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
import re
import sys
import urllib.request
from pathlib import Path

from engine.assembly.sticker_catalog import gif_url

BAKES_DIRNAME = "bakes"
PREVIEWS_DIRNAME = "previews"
SOURCES_DIRNAME = "sources"

# A Lordicon slug is `<number>-<words>`; nothing else may become a path
# component here. The digest in bake_key is what actually distinguishes two
# bakes, so the readable prefix is a convenience -- and a convenience is not
# worth letting `../` or an absolute path through into a directory name.
# Task 5 checks the slug against the catalogue as well; this is the check
# that does not depend on a caller remembering to.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def safe_slug(slug: str) -> str:
    """The slug, or raise. The only way a slug becomes part of a path."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"not a Lordicon slug: {slug!r}")
    return slug


def bake_key(slug: str, style: str, size: int, fps: int) -> str:
    """A directory name for one baked sequence.

    Hashed rather than concatenated because a slug is free-form text from a
    sitemap and this becomes a path. The slug is kept in front of the digest
    anyway, so a human can read the cache.
    """
    slug = safe_slug(slug)
    digest = hashlib.sha256(
        f"{slug}|{style}|{size}|{fps}".encode()).hexdigest()[:12]
    return f"{slug}-{style}-{digest}"


def _download(slug: str, dest: Path) -> None:
    """Fetch one icon's GIF. Split out so tests can replace it."""
    urllib.request.urlretrieve(gif_url(slug), dest)


def _source(slug: str, root: Path) -> Path:
    """The cached source GIF, downloaded on first use."""
    slug = safe_slug(slug)
    folder = Path(root) / SOURCES_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{slug}.gif"
    if not dest.exists():
        staged = dest.with_suffix(".part")
        try:
            _download(slug, staged)
            staged.replace(dest)
        except Exception:
            # Same cleanup as sticker_catalog.refresh: no half-written
            # file left behind for the next call to trip over.
            staged.unlink(missing_ok=True)
            raise
    return dest


def cached_sequence(slug: str, style: str, *, fps: int, size: int,
                    root: Path) -> tuple[str, int, int] | None:
    """``(pattern, frames, canvas)`` for a chosen icon, or None.

    Returns None for every kind of absence -- never baked, cache cleaned,
    baked at another size -- so the caller falls to the rung below rather
    than rendering something wrong. The same contract as
    ``stickers.baked_sequence``, deliberately: the caller treats them alike.

    A slug that is not a slug answers ``None`` rather than raising: this is
    a lookup, and the honest answer to "is there a bake for this?" is no.
    The write paths still refuse it -- ``bake_key`` and ``_source`` are
    about to create something at that path, and a read is not.
    """
    # Not reusing ``stickers.baked_sequence``: it expects a
    # ``<trigger>/<style>`` layout under one root, and this cache is keyed
    # flat by content so two reels can share a bake. The checks are the
    # same ones, in ``_resolve`` below.
    try:
        folder = Path(root) / BAKES_DIRNAME / bake_key(slug, style, size, fps)
    except ValueError:
        return None
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
        if int(meta["fps"]) != fps or int(meta["size"]) != size:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        # A cache entry we cannot read is a cache entry we do not have.
        # Raising would take down a render over a file the next bake
        # rewrites anyway.
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
    # Guarded, not unconditional: this runs on every call, and Task 5 calls
    # it from a long-lived server process where an unconditional insert
    # would grow sys.path by one duplicate entry per request forever.
    repo_root = str(Path(__file__).resolve().parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from engine.assembly.sticker_art import STYLES
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

    slug = safe_slug(slug)
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

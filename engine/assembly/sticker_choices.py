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

# Everything this module caches for one engine lives under here, inside the
# work directory. A constant rather than a literal because the panel writes
# these bakes and the renderer reads them, and when the two sides each spelled
# the path themselves they drifted -- the feature shipped inert for exactly
# that reason: four literals, two of which said `work_dir/_lordicon` and two
# of which said `work_dir`. Anyone who needs this root calls `cache_root`.
# (Not to be confused with `stickers.CACHE_DIRNAME`, "_stickers", which is
# that module's own cache of rendered emoji pops. Different owner, different
# contents, deliberately a different directory.)
CACHE_DIRNAME = "_lordicon"


def cache_root(settings) -> Path:
    """Where this engine's chosen-sticker cache lives.

    Takes the settings object rather than a path so that every caller --
    three routes in the panel and one rung of ``prepare`` -- reaches the
    same directory by construction instead of by agreement.
    """
    return Path(getattr(settings, "work_dir", ".")) / CACHE_DIRNAME


# A Lordicon slug is `<number>-<words>`; nothing else may become a path
# component here. The digest in bake_key is what actually distinguishes two
# bakes, so the readable prefix is a convenience -- and a convenience is not
# worth letting `../` or an absolute path through into a directory name.
# Task 5 checks the slug against the catalogue as well; this is the check
# that does not depend on a caller remembering to.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def safe_slug(slug: str) -> str:
    """A bare Lordicon slug, or raise."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"not a Lordicon slug: {slug!r}")
    return slug


# Two libraries, two shapes. Lordicon serves `1875-planet`; Noto serves a
# codepoint, which for skin tones and variation selectors is several hex
# groups joined by underscores -- `1f44b_1f3ff`, `2620_fe0f`. One regex
# covering both would also accept each library's nonsense as the other's.
DEFAULT_LIBRARY = "lordicon"
_SLUG_RULES = {
    "lordicon": _SLUG,
    "noto": re.compile(r"^[0-9a-f]+(?:_[0-9a-f]+)*$"),
}
LIBRARIES = tuple(_SLUG_RULES)
# How each library is spelled when refusing something. The name a person
# reads, not the key the code uses.
_LIBRARY_NAMES = {"lordicon": "Lordicon", "noto": "Noto"}


def split_slug(qualified: str) -> tuple[str, str]:
    """``("noto", "1f480")`` from ``"noto:1f480"``, or raise.

    The only way a stored choice becomes a path, and the only place the
    ``library:slug`` spelling is understood. Everything downstream gets the
    two parts separately, so the separator never reaches a filename -- ``:``
    is not legal in one on Windows.

    A value with no separator is Lordicon's. Every choice stored before a
    second library existed is bare, and those rows are never rewritten.
    """
    if not isinstance(qualified, str):
        raise ValueError(f"not a sticker slug: {qualified!r}")
    library, sep, slug = qualified.partition(":")
    if not sep:
        library, slug = DEFAULT_LIBRARY, qualified
    rule = _SLUG_RULES.get(library)
    if rule is None:
        raise ValueError(
            f"unknown icon library {library!r}; "
            f"expected one of {', '.join(LIBRARIES)}")
    if not rule.fullmatch(slug):
        raise ValueError(
            f"not a {_LIBRARY_NAMES[library]} slug: {slug!r}")
    return library, slug


def bake_key(slug: str, style: str, size: int, fps: int) -> str:
    """A directory name for one baked sequence.

    Hashed rather than concatenated because a slug is free-form text from a
    catalogue and this becomes a path. The library and slug are kept in
    front of the digest anyway, so a human can read the cache.

    ``slug`` may be qualified (``noto:1f480``). The library is part of the
    digest, so the same characters under two libraries are two bakes rather
    than one that quietly serves the wrong art.
    """
    from engine.assembly.sticker_bake import BAKE_VERSION

    library, bare = split_slug(slug)
    # BAKE_VERSION is in the digest because the frame count and the size
    # cannot see a change in how a frame is *drawn*. A sequence redrawn
    # with the pop has the same count and size as one without it, so
    # without this every warm cache would keep serving the old look.
    digest = hashlib.sha256(
        f"v{BAKE_VERSION}|{library}|{bare}|{style}|{size}|{fps}"
        .encode()).hexdigest()[:12]
    return f"{library}-{bare}-{style}-{digest}"


def _download(library: str, slug: str, dest: Path) -> None:
    """Fetch one icon's GIF. Split out so tests can replace it.

    Takes the library and the bare slug rather than the qualified string,
    because the caller has already split it and rejoining only to split
    again is where a separator gets mishandled.
    """
    bare = slug
    if library == "noto":
        from engine.assembly.noto_catalog import gif_url as noto_gif_url
        url = noto_gif_url(bare)
    else:
        url = gif_url(bare)
    urllib.request.urlretrieve(url, dest)


def _source(slug: str, root: Path) -> Path:
    """The cached source GIF, downloaded on first use.

    Filed under its library, so two codepoints that happen to spell the
    same thing never share a file.
    """
    library, bare = split_slug(slug)
    folder = Path(root) / SOURCES_DIRNAME / library
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{bare}.gif"
    if not dest.exists():
        staged = dest.with_suffix(".part")
        try:
            _download(library, bare, staged)
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
             ) -> tuple[str, int, int, str | None] | None:
    import json

    # Imported here, like json above, so that importing this module never
    # drags in the renderer -- `stickers` imports this one back, lazily,
    # inside `prepare`. The constant is read rather than copied: a second
    # spelling of the hold window is the same drift that made this feature
    # inert once already.
    from engine.assembly.stickers import HOLD_SECONDS

    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        frames = int(meta["frames"])
        canvas = int(meta["canvas"])
        # Read, never assumed: the credit a render owes has to come from
        # the art it actually used.
        #
        # A bake written before this field existed cannot say what it was
        # made from, and the safe answer there is Lordicon rather than
        # nothing: every bake that predates a second library came from it,
        # so crediting nothing would drop a credit genuinely owed. Once a
        # bake records its own licence this fallback never fires.
        from engine.assembly.stickers import LICENCES
        licence = meta.get("licence") or LICENCES["lordicon"]
        if int(meta["fps"]) != fps or int(meta["size"]) != size:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        # A cache entry we cannot read is a cache entry we do not have.
        # Raising would take down a render over a file the next bake
        # rewrites anyway.
        return None
    if frames <= 0:
        return None
    # The same window check `stickers.baked_sequence` makes, for the same
    # reason: `sticker_chain` trims to HOLD_SECONDS and gates `enable` to
    # that span, so a sequence of any other length plays truncated.
    # `bake_key` hashes slug, style, size and fps and *not* HOLD_SECONDS, so
    # without this a bake made at the old window would keep its key and stay
    # a cache hit forever, while the committed art beside it was rejected
    # and re-baked -- the chosen icon, which is the rung that wins, would be
    # the only sticker playing truncated.
    if frames != round(HOLD_SECONDS * fps):
        return None
    if len(list(folder.glob("frame-*.png"))) != frames:
        return None
    return str(folder / "frame-%03d.png"), frames, canvas, licence


def ensure_baked(slug: str, *, root: Path, size: int, fps: int,
                 style: str) -> None:
    """Bake one style of one icon into the cache, if it is not there.

    ``style`` is required. This used to bake every style in ``STYLES``,
    because a choice keyed by trigger name could be used by beats of
    different roles and ``style_for_role`` could not be resolved in advance.
    A choice keyed by a beat has exactly one role and therefore exactly one
    style, which halves the wait on the click that commits.

    Raises ``ValueError`` when the icon has a pocket of trapped white --
    ``bake_one``'s own refusal, passed straight through, because an icon
    that would render with a white blob in it is a choice to reject rather
    than a failure to swallow.
    """
    # Guarded, not unconditional: this runs on every call, from a
    # long-lived server process where an unconditional insert would grow
    # sys.path by one duplicate entry per request forever.
    repo_root = str(Path(__file__).resolve().parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from engine.assembly.sticker_art import STYLES
    from scripts.bake_stickers import bake_one

    if style not in STYLES:
        raise ValueError(f"unknown sticker style {style!r}")

    root = Path(root)
    library, _bare = split_slug(slug)
    folder = root / BAKES_DIRNAME / bake_key(slug, style, size, fps)
    if _resolve(folder, fps=fps, size=size) is not None:
        return
    # The credit is recorded from the library the art came from, never left
    # to bake_one's default. That default is Lordicon's, so an emoji baked
    # without this would claim a credit it does not owe and hide the one it
    # does.
    from engine.assembly.stickers import LICENCES
    bake_one(_source(slug, root), folder, style=style, size=size, fps=fps,
             licence=LICENCES[library])


def source_gif(slug: str, *, root: Path) -> Path:
    """The cached source animation, downloaded on first use.

    Public because the picker serves it: a wall of stills says nothing
    about how an icon moves, and a spinning lock and a wobbling lock are
    the same picture. Same file the bake already pulls, so hovering a
    card costs at most one download that was going to happen anyway.
    """
    return _source(slug, Path(root))


def preview_png(slug: str, *, root: Path, style: str = "punchy",
                px: int = 96) -> Path:
    """One matted, graded frame of an icon, for browsing.

    A bake is 42 frames in two styles and takes about sixteen seconds. A
    preview is one frame and takes a fraction of one, and browsing has to be
    cheap or nobody browses.
    """
    from PIL import Image

    from engine.assembly.sticker_art import apply_style, ground, matte

    library, bare = split_slug(slug)
    root = Path(root)
    folder = root / PREVIEWS_DIRNAME / library
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{bare}-{style}-{px}.png"
    if dest.exists():
        return dest

    source = _source(slug, root)
    with Image.open(source) as src:
        src.seek(getattr(src, "n_frames", 1) // 2)
        frame = src.convert("RGBA")
    # The same rule the bake follows: a source that brought its own
    # transparency keeps it, and only a source on an opaque ground gets the
    # flood fill. Matting a Noto emoji does nothing -- its ground is pale
    # blue, not white -- and the preview would show the opaque square the
    # reel would not.
    if frame.getchannel("A").getextrema()[0] >= 16:
        frame = matte(frame.convert("RGB"))
    art = ground(apply_style(frame, style), style)
    art.resize((px, px), Image.LANCZOS).save(dest)
    return dest

"""Both icon libraries behind one interface.

The picker asks two questions -- what matches this word, and is this a real
icon -- and neither answer should depend on the caller knowing how many
libraries there are. This module is the one place that knows.

Lordicon's 3,578 icons are named for objects, which makes them findable and
also makes them miss: a script asking for "dream" or "farewell" gets
nothing. Noto's 881 animated emoji carry tags and categories, reaching some
of those ideas from the other side. Neither covers the other, so both are
offered and the person picks.

Slugs are qualified -- ``lordicon:27-globe``, ``noto:1f480`` -- because both
end up as filenames and a bare string cannot say which library it belongs
to. An unqualified slug is Lordicon's: every choice stored before a second
library existed is bare, and those rows are never rewritten.
"""

from __future__ import annotations

from pathlib import Path

from engine.assembly import noto_catalog, sticker_catalog
from engine.assembly.sticker_choices import split_slug


def refresh(cache_dir: str | Path) -> str:
    """Make sure both catalogues are cached. Returns a note, or ``""``.

    One library being unreachable must not cost the other its candidates,
    so each is attempted separately and the failure is reported rather than
    raised. A picker with half its icons is worth more than an error page.
    """
    notes: list[str] = []
    cache_dir = Path(cache_dir)
    for name, module in (("Lordicon", sticker_catalog),
                         ("Noto", noto_catalog)):
        try:
            module.refresh(cache_dir)
        except Exception as exc:                       # noqa: BLE001
            # Deliberately broad. This function exists to keep one
            # library's trouble off the other, and catching only each
            # module's own CatalogUnavailable let anything else through --
            # including the sibling library's class, which is a different
            # type for the same meaning. Nothing is swallowed: whatever
            # happened goes into the note the board shows.
            notes.append(f"{name} unavailable: {exc}")
    return "; ".join(notes)


def search(term: str, cache_dir: str | Path, *, limit: int = 8) -> list[str]:
    """Qualified slugs matching ``term``, best first, from both libraries.

    Interleaved rather than concatenated. Lordicon is four times the size,
    so taking the best ``limit`` overall would bury Noto on almost every
    term -- and a picker that only ever shows one library is a picker with
    one library.
    """
    cache_dir = Path(cache_dir)
    lordicon = [f"lordicon:{s}" for s in sticker_catalog.search(
        term, sticker_catalog.load(cache_dir), limit=limit)]
    noto = [f"noto:{s}" for s in noto_catalog.search(
        term, noto_catalog.load(cache_dir), limit=limit)]

    merged: list[str] = []
    for pair in zip(lordicon, noto):
        merged.extend(pair)
    # Whichever list was longer finishes on its own rather than being cut
    # short: if one library has nothing for this word, the other fills the
    # row instead of leaving it half empty.
    longer = lordicon if len(lordicon) > len(noto) else noto
    merged.extend(longer[min(len(lordicon), len(noto)):])
    return merged[:limit]


def known(slug: str, cache_dir: str | Path) -> bool:
    """Is this a real icon in its own library's catalogue?

    The gate that stops the choose route fetching whatever URL a caller
    names. Checked against the catalogue the slug claims to come from, so a
    real Noto codepoint offered as a Lordicon slug is still refused.

    Never raises: a malformed slug is simply not a known icon, and the
    caller answers 400 either way.
    """
    try:
        library, bare = split_slug(slug)
    except ValueError:
        return False
    cache_dir = Path(cache_dir)
    if library == "noto":
        return any(icon.codepoint == bare
                   for icon in noto_catalog.load(cache_dir))
    return bare in sticker_catalog.load(cache_dir)

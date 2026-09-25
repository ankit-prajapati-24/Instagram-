r"""Getting a bundled font in front of libass.

`render` runs ffmpeg with its cwd set to the .ass file's directory and
hands the filter a bare filename, because a Windows drive-letter colon
inside a filtergraph is read as an option separator -- "C:/x/a.ass"
parses as the ``original_size`` option and fails. ``fontsdir`` has
exactly the same problem, so the look's fonts are copied next to the
.ass and the directory is named relatively.

Staging rather than pointing at ``assets/fonts`` directly for the same
reason: that path is absolute and carries the colon.

A font that is named but not committed is the quiet failure this
module exists to make loud. libass substitutes a system face without
complaint, so the reel renders in the wrong type and nothing anywhere
says why.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from engine.assembly.looks import Look

FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"

# ASS family name -> the file that provides it. The family name is what
# a Style line carries and what libass matches on; it is not always the
# filename.
FONT_FILES = {
    "Bungee": "Bungee.ttf",
    "Outfit Black": "Outfit-Black.ttf",
    "Playfair Display Black": "PlayfairDisplay-Black.ttf",
    "Staatliches": "Staatliches.ttf",
    "Teko": "Teko.ttf",
    "Chakra Petch": "ChakraPetch.ttf",
    "Noto Sans Devanagari": "NotoSansDevanagari.ttf",
}

STAGED_DIRNAME = "fonts"

# The only bundled face that draws Devanagari.
DEVANAGARI_FONT = "Noto Sans Devanagari"


def devanagari_face(name: str) -> str:
    """The face to draw a Devanagari caption in, given the configured one.

    A name this module does not ship cannot be staged, and an unstaged
    name turns ``fontsdir`` off for the whole reel -- libass then
    resolves the caption against the host's own fonts, exit code 0,
    nothing on stderr. That is the failure this module exists to stop,
    so a configured face that is not bundled is refused out loud rather
    than passed through. ``RAHASYA_FONT_DEVA`` still chooses, but only
    among faces that travel with the repository.
    """
    if name in FONT_FILES:
        return name
    print(f"[looks] {name!r} is not bundled, so it cannot be staged and "
          f"libass would substitute silently. Drawing Devanagari in "
          f"{DEVANAGARI_FONT} instead. Set RAHASYA_FONT_DEVA to one of "
          f"{', '.join(sorted(FONT_FILES))} to choose.",
          file=sys.stderr, flush=True)
    return DEVANAGARI_FONT


def stage_fonts(look: Look, target_dir: str | Path) -> str | None:
    """Put this look's fonts beside the captions; name them relatively.

    Returns the relative directory for ``fontsdir``, or None when the
    look needs no bundled font and when one is missing -- in both cases
    libass is left to its own resolution, which is right for Arial and
    is at least loud for the other.
    """
    wanted = {name for name in (look.font, look.punch_font)
              if name in FONT_FILES}
    # Devanagari can appear in a Devanagari-sourced caption, and only
    # one bundled face draws it; ship the fallback whenever anything is
    # staged at all.
    if wanted:
        wanted.add("Noto Sans Devanagari")
    if not wanted:
        return None

    missing = [n for n in sorted(wanted)
               if not (FONT_DIR / FONT_FILES[n]).is_file()]
    if missing:
        print(f"[looks] not rendering in {look.look_id}: "
              f"{', '.join(missing)} is named by the look but missing "
              f"from {FONT_DIR}. libass would substitute silently.",
              file=sys.stderr, flush=True)
        return None

    staged = Path(target_dir) / STAGED_DIRNAME
    staged.mkdir(parents=True, exist_ok=True)
    for name in sorted(wanted):
        source = FONT_DIR / FONT_FILES[name]
        destination = staged / FONT_FILES[name]
        if not destination.is_file() or \
                destination.stat().st_size != source.stat().st_size:
            shutil.copyfile(source, destination)
    return STAGED_DIRNAME

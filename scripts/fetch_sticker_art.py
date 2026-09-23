"""Download the designed sticker art named in the trigger map.

Run by hand when the art changes, never during a render. The renderer only
ever sees the baked PNG sequences that scripts/bake_stickers.py produces
from these files.

    python scripts/fetch_sticker_art.py           # only what is missing
    python scripts/fetch_sticker_art.py --force   # re-download everything

Why the GIF and not the .lottie or .li: the .li payload Lordicon serves is
an obfuscated container, and decoding it would be working around their
access control. The GIF is what Lordicon itself puts in the page's
og:image, so it is served for public consumption, and Pillow already reads
it -- which is also how this feature avoids adding a dependency.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly.stickers import load_triggers  # noqa: E402

ART_DIR = Path(__file__).resolve().parent.parent / "assets" / "lordicon"

# Only one source is implemented. An unknown one raises rather than guessing
# a URL, because a guessed URL that 404s writes an HTML error page over the
# art and the failure surfaces later, in the bake, wearing the wrong name.
_TEMPLATES = {
    "lordicon": ("https://media.lordicon.com/icons/"
                 "{family}/{variant}/{slug}.gif"),
}


def art_url(art: dict) -> str:
    """The download URL for one manifest entry."""
    source = art.get("source")
    template = _TEMPLATES.get(source)
    if template is None:
        raise ValueError(f"unknown art source {source!r}")
    return template.format(family=art["family"], variant=art["variant"],
                           slug=art["slug"])


def verify_gif(path: Path) -> tuple[int, int]:
    """``(frames, side)`` for a usable source GIF, or raise.

    A 404 from a CDN is an HTML page with a 200-shaped body as often as not,
    and a one-frame GIF is a still that would bake into the same frozen
    sticker this whole change exists to remove. Both are caught here, at the
    only point where the fix is obvious: pick another icon.
    """
    from PIL import Image

    try:
        with Image.open(path) as im:
            if im.format != "GIF":
                raise ValueError(f"{path.name} is not a GIF ({im.format})")
            frames = getattr(im, "n_frames", 1)
            width, height = im.size
    except ValueError:
        raise
    except Exception as exc:                      # unreadable bytes
        raise ValueError(f"{path.name} is not a GIF: {exc}") from exc

    if frames < 2:
        raise ValueError(f"{path.name} has a single frame, not an animation")
    if width != height:
        raise ValueError(f"{path.name} is {width}x{height}, not square")
    return frames, width


def fetch(force: bool = False, art_dir: Path | None = None) -> int:
    art_dir = art_dir or ART_DIR
    art_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for trigger in load_triggers():
        if not trigger.art:
            continue
        dest = art_dir / f"{trigger.name}.gif"
        if dest.exists() and not force:
            print(f"[art] {trigger.name}: already here")
            continue
        url = art_url(trigger.art)
        # Download beside the target and only move it into place once it
        # verifies, so a failure can never leave a half-written file that
        # the bake would happily read.
        staged = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, staged)
        try:
            frames, side = verify_gif(staged)
        except ValueError:
            staged.unlink(missing_ok=True)
            raise
        staged.replace(dest)
        written += 1
        print(f"[art] {trigger.name}: {frames} frames at {side}px  <- {url}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="re-download art that is already present")
    args = parser.parse_args()
    fetch(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

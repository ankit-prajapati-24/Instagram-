"""Fetching and baking designed sticker art.

Nothing here talks to the network. The download is one URL built from the
trigger map, so the URL is tested as a pure function and the verification
is tested against GIFs written on the spot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from scripts.fetch_sticker_art import art_url, verify_gif


def _write_gif(path: Path, frames: int, side: int = 32,
               colour=(10, 20, 30)) -> Path:
    # Pillow's GIF encoder collapses byte-identical consecutive frames into
    # one, extending the previous frame's duration instead of writing a new
    # one -- unconditionally, not just under optimize=True. A fixed colour
    # for every frame would therefore always write a 1-frame file no matter
    # how many `images` are appended, so each frame nudges the colour to
    # stay distinct.
    images = [Image.new("RGB", (side, side),
                        tuple((c + i) % 256 for c in colour))
             for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=40, loop=0)
    return path


def test_the_url_is_built_from_the_manifest():
    art = {"source": "lordicon", "family": "wired",
           "variant": "flat", "slug": "2130-skull-poison"}
    assert art_url(art) == (
        "https://media.lordicon.com/icons/wired/flat/"
        "2130-skull-poison.gif")


def test_an_unknown_source_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="source"):
        art_url({"source": "elsewhere", "family": "wired",
                 "variant": "flat", "slug": "x"})


def test_a_real_gif_verifies_and_reports_its_shape(tmp_path):
    path = _write_gif(tmp_path / "ok.gif", frames=12, side=48)
    assert verify_gif(path) == (12, 48)


def test_an_html_error_page_saved_as_gif_is_refused(tmp_path):
    path = tmp_path / "bad.gif"
    path.write_bytes(b"<!DOCTYPE html><html><head><title>404")
    with pytest.raises(ValueError, match="not a GIF"):
        verify_gif(path)


def test_a_single_frame_gif_is_refused(tmp_path):
    path = _write_gif(tmp_path / "still.gif", frames=1)
    with pytest.raises(ValueError, match="single frame"):
        verify_gif(path)

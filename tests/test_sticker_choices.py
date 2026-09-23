"""The cache that stands between a chosen slug and a rendered sticker.

Content-addressed on purpose: two reels that pick the same icon share one
bake, and picking an icon a second time costs nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from engine.assembly import sticker_choices as sc


def _gif(path: Path, frames: int = 9, side: int = 64) -> Path:
    """A GIF on a white ground, like the ones Lordicon serves.

    Colour varies per frame because Pillow merges byte-identical
    consecutive frames when writing a GIF, which would collapse this to a
    still.
    """
    from PIL import ImageDraw
    images = []
    for i in range(frames):
        im = Image.new("RGB", (side, side), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse((8, 8, side - 8, side - 8), fill=(20 + i * 3, 40, 60))
        images.append(im)
    # format="GIF" because some tests point this at a staged ".part" path
    # (ensure_baked's atomic-download rename target, the same pattern
    # sticker_catalog.refresh uses) -- urlretrieve never sniffs an
    # extension, but Image.save() does, and refuses an unknown one.
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=40, loop=0, format="GIF")
    return path


def test_the_key_separates_everything_that_changes_the_pixels():
    a = sc.bake_key("27-globe", "dark", 184, 30)
    assert a != sc.bake_key("27-globe", "punchy", 184, 30)
    assert a != sc.bake_key("27-globe", "dark", 60, 30)
    assert a != sc.bake_key("27-globe", "dark", 184, 60)
    assert a != sc.bake_key("1875-planet", "dark", 184, 30)
    assert a == sc.bake_key("27-globe", "dark", 184, 30)


def test_nothing_cached_resolves_to_none(tmp_path):
    assert sc.cached_sequence("27-globe", "dark", fps=30, size=60,
                              root=tmp_path) is None


def test_a_baked_slug_resolves_like_the_committed_art_does(tmp_path,
                                                           monkeypatch):
    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(sc, "_download", lambda slug, dest: src.replace(dest))

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)

    found = sc.cached_sequence("27-globe", "punchy", fps=30, size=60,
                               root=tmp_path)
    assert found is not None
    pattern, frames, canvas = found
    from engine.assembly.stickers import HOLD_SECONDS, sticker_canvas
    assert frames == round(HOLD_SECONDS * 30)
    assert canvas == sticker_canvas(60)
    assert Path(pattern % 0).exists()


def test_baking_the_same_slug_twice_does_not_download_twice(tmp_path,
                                                             monkeypatch):
    src = _gif(tmp_path / "src.gif")
    calls = []

    def fake_download(slug, dest):
        calls.append(slug)
        _gif(Path(dest))

    monkeypatch.setattr(sc, "_download", fake_download)

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)
    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)
    assert len(calls) == 1


def test_a_preview_is_one_frame_not_a_bake(tmp_path, monkeypatch):
    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(sc, "_download", lambda slug, dest: _gif(Path(dest)))

    png = sc.preview_png("27-globe", root=tmp_path, px=96)
    assert png.exists()
    with Image.open(png) as im:
        assert im.size == (96, 96)
        assert im.mode == "RGBA"
    # A preview must not have produced a sequence.
    assert not list((tmp_path / "bakes").rglob("frame-000.png"))


def test_an_icon_with_a_trapped_white_pocket_is_refused_by_name(tmp_path,
                                                                monkeypatch):
    from PIL import ImageDraw
    frames = []
    for i in range(4):
        im = Image.new("RGB", (64, 64), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse((8, 8, 56, 56), fill=(20 + i, 30, 40))
        d.ellipse((26, 26, 38, 38), fill=(255, 255, 255))
        frames.append(im)
    holed = tmp_path / "holed.gif"
    frames[0].save(holed, save_all=True, append_images=frames[1:],
                   duration=40, loop=0)

    monkeypatch.setattr(sc, "_download",
                        lambda slug, dest: holed.replace(Path(dest)))

    with pytest.raises(ValueError, match="2816-skull-halloween"):
        sc.ensure_baked("2816-skull-halloween", root=tmp_path, size=60,
                        fps=30)


@pytest.mark.parametrize("slug", [
    "../../../evil", "/etc/passwd", "C:/Windows/evil", "a/b",
    "a\\b", "with space", "UPPER", "", "null\x00byte",
])
def test_a_slug_that_is_not_a_slug_never_becomes_a_path(slug, tmp_path):
    """The slug reaches this module from an HTTP body.

    `bake_key`'s digest is what distinguishes two bakes; the readable
    prefix is a convenience, and a convenience is not worth a directory
    name that can leave the cache.
    """
    with pytest.raises(ValueError, match="not a Lordicon slug"):
        sc.bake_key(slug, "dark", 184, 30)
    with pytest.raises(ValueError, match="not a Lordicon slug"):
        sc.preview_png(slug, root=tmp_path)


def test_every_shipped_slug_still_passes():
    """The guard must not reject the real catalogue's own names."""
    for slug in ("27-globe", "2130-skull-poison", "196-clock-arrow-rotate-left",
                 "1195-earthworm", "440-dna"):
        assert sc.safe_slug(slug) == slug


@pytest.mark.parametrize("meta", [
    {"frames": 42, "canvas": 78, "fps": None, "size": 60},
    {"frames": [42], "canvas": 78, "fps": 30, "size": 60},
    {"canvas": 78, "fps": 30, "size": 60},
], ids=["null-fps", "non-int-frames", "missing-frames"])
def test_a_corrupt_meta_resolves_to_none_not_a_crash(meta, tmp_path):
    """A torn write, a hand edit, schema drift -- none of it may raise.

    ``cached_sequence``'s whole contract is None for every kind of absence;
    a render must never go down because one cache entry's meta.json is bad,
    when the next bake overwrites it anyway.
    """
    folder = tmp_path / "bakes" / sc.bake_key("27-globe", "dark", 60, 30)
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert sc.cached_sequence("27-globe", "dark", fps=30, size=60,
                              root=tmp_path) is None


def test_a_failed_download_leaves_no_stage_behind(tmp_path, monkeypatch):
    """Match ``sticker_catalog.refresh``: a fetch that fails must not leave
    a ``.part`` file behind for the next call to trip over.
    """
    def boom(slug, dest):
        raise OSError("no route to host")

    monkeypatch.setattr(sc, "_download", boom)

    with pytest.raises(OSError, match="no route to host"):
        sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30)
    assert not list((tmp_path / "sources").glob("*.part")), \
        "staging file left behind"

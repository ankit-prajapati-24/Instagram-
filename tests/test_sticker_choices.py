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
    monkeypatch.setattr(sc, "_download", lambda library, slug, dest: src.replace(dest))

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30,
                    style="punchy")

    found = sc.cached_sequence("27-globe", "punchy", fps=30, size=60,
                               root=tmp_path)
    assert found is not None
    pattern, frames, canvas, _licence = found
    from engine.assembly.stickers import HOLD_SECONDS, sticker_canvas
    assert frames == round(HOLD_SECONDS * 30)
    assert canvas == sticker_canvas(60)
    assert Path(pattern % 0).exists()


def test_choosing_the_same_slug_twice_re_bakes_nothing(tmp_path,
                                                       monkeypatch):
    """The point of a content-addressed cache, asserted on the expensive
    half.

    Counting downloads alone does not do it: a download is 0.4s and is
    guarded by ``_source``'s own ``dest.exists()``, so that count stays at
    one even with ``ensure_baked``'s cache check deleted -- while every
    re-choose silently pays eight seconds to bake the style again. The
    spy wraps the real ``bake_one`` rather than replacing it, because a
    stub that writes nothing would leave the cache empty and make the
    second round bake legitimately.
    """
    import scripts.bake_stickers as bakery

    downloads = []
    bakes = []
    real_bake_one = bakery.bake_one

    def fake_download(library, slug, dest):
        downloads.append((library, slug))
        _gif(Path(dest))

    def spy_bake_one(gif, out_dir, **kwargs):
        bakes.append(Path(out_dir).name)
        return real_bake_one(gif, out_dir, **kwargs)

    monkeypatch.setattr(sc, "_download", fake_download)
    monkeypatch.setattr(bakery, "bake_one", spy_bake_one)

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30, style="dark")
    first = list(bakes)
    assert len(first) == 1, f"only the one style bakes the first time: {first}"

    sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30, style="dark")

    assert bakes == first, f"the second choose must bake nothing: {bakes}"
    assert downloads == [("lordicon", "27-globe")],         f"and fetch nothing: {downloads}"


def test_a_preview_is_one_frame_not_a_bake(tmp_path, monkeypatch):
    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(sc, "_download", lambda library, slug, dest: _gif(Path(dest)))

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
                        lambda library, slug, dest: holed.replace(Path(dest)))

    with pytest.raises(ValueError, match="2816-skull-halloween"):
        sc.ensure_baked("2816-skull-halloween", root=tmp_path, size=60,
                        fps=30, style="dark")


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
    # Two refusals, both correct: anything before a `:` is read as a
    # library name, so "C:/Windows/evil" is turned away as an unknown
    # library rather than as a bad slug. What matters is that neither
    # reaches a path.
    refused = "not a Lordicon slug|unknown icon library"
    with pytest.raises(ValueError, match=refused):
        sc.bake_key(slug, "dark", 184, 30)
    with pytest.raises(ValueError, match=refused):
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


def test_a_bake_that_does_not_span_the_hold_window_resolves_to_none(
        tmp_path):
    """The same window check ``stickers.baked_sequence`` makes.

    ``bake_key`` hashes slug, style, size and fps -- not ``HOLD_SECONDS``.
    So if the hold window ever moves, every cached bake keeps its key and
    stays a cache hit at the old length, while the committed art beside it
    is correctly rejected and re-baked. The chosen icon would then be the
    only sticker that plays truncated, and it is the rung that wins, so the
    committed art's correctness would not save it.

    The frame files are all written here: without this check the existing
    count guard is satisfied and the bake resolves.
    """
    from engine.assembly.stickers import HOLD_SECONDS

    fps, size = 30, 60
    wrong = round(HOLD_SECONDS * fps) - 12
    assert wrong > 0 and wrong != round(HOLD_SECONDS * fps)

    folder = tmp_path / sc.BAKES_DIRNAME / sc.bake_key(
        "27-globe", "dark", size, fps)
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(
        json.dumps({"frames": wrong, "canvas": 78, "fps": fps,
                    "size": size}), encoding="utf-8")
    for i in range(wrong):
        Image.new("RGBA", (8, 8)).save(folder / f"frame-{i:03d}.png")

    assert sc.cached_sequence("27-globe", "dark", fps=fps, size=size,
                              root=tmp_path) is None, (
        "a bake that does not fill the hold window would play truncated")


def test_a_failed_download_leaves_no_stage_behind(tmp_path, monkeypatch):
    """Match ``sticker_catalog.refresh``: a fetch that fails must not leave
    a ``.part`` file behind for the next call to trip over.
    """
    def boom(library, slug, dest):
        raise OSError("no route to host")

    monkeypatch.setattr(sc, "_download", boom)

    with pytest.raises(OSError, match="no route to host"):
        sc.ensure_baked("27-globe", root=tmp_path, size=60, fps=30,
                        style="dark")
    assert not list((tmp_path / "sources").glob("*.part")), \
        "staging file left behind"


def test_a_nonsense_slug_reads_as_no_bake_rather_than_raising(tmp_path):
    """A lookup answers; only the write paths refuse.

    prepare() falls through this to the committed art and then to the
    emoji, so raising here would turn a bad row in the choices table into
    a failed render -- the one thing a decoration must never cost.
    """
    for slug in ("../../../evil", "/etc/passwd", "", "UPPER"):
        assert sc.cached_sequence(slug, "dark", fps=30, size=184,
                                  root=tmp_path) is None

    # The write paths still refuse the same slugs.
    with pytest.raises(ValueError):
        sc.bake_key("../../../evil", "dark", 184, 30)


def _fake_bake_into(folder, *, size, fps, style="punchy", slug="fake"):
    """Write a bake of the right shape without running the real one.

    The ``meta.json`` is not optional: ``_resolve`` returns None without it,
    so a fixture of frames alone reads as "nothing cached" and every test
    built on it fails for the wrong reason.
    """
    from engine.assembly import stickers as stk
    frames = round(stk.HOLD_SECONDS * fps)
    canvas = stk.sticker_canvas(size)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(frames):
        Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0)).save(
            folder / f"frame-{n:03d}.png")
    (folder / "meta.json").write_text(json.dumps({
        "source": f"{slug}.gif", "style": style, "frames": frames,
        "fps": fps, "size": size, "canvas": canvas, "licence": "test",
    }), encoding="utf-8")
    return folder


def _fake_bake(root, slug, style, *, size, fps):
    return _fake_bake_into(
        Path(root) / sc.BAKES_DIRNAME / sc.bake_key(slug, style, size, fps),
        size=size, fps=fps)


def test_only_the_style_the_beat_needs_is_baked(tmp_path, monkeypatch):
    """A bake is ~8s a style. Keyed by trigger, both had to be made because
    the role was not knowable; keyed by beat, one is."""
    from engine.assembly import sticker_art
    made = []

    def _fake_bake_one(source, folder, *, style, size, fps,
                       licence=None):
        made.append(style)
        _fake_bake_into(folder, size=size, fps=fps, style=style)

    monkeypatch.setattr("scripts.bake_stickers.bake_one", _fake_bake_one)
    monkeypatch.setattr(sc, "_source",
                        lambda slug, root: tmp_path / "src.gif")
    (tmp_path / "src.gif").write_bytes(b"GIF89a")

    sc.ensure_baked("27-globe", root=tmp_path, size=220,
                    fps=30, style="dark")
    assert made == ["dark"], f"baked {made}, wanted only the one needed"
    assert set(sticker_art.STYLES) == {"punchy", "dark"}, \
        "this test is only meaningful while there is another style to skip"


def test_two_beats_resolve_their_own_chosen_icons(tmp_path):
    """The defect the beat key fixes, proven where it is actually visible.

    Two `water` cues in one reel. Keyed by trigger name they shared one
    answer; keyed by beat they each get their own. Deliberately trigger-rung
    cues: on the model rung `cue.name` already IS the beat id, so a test
    built from `sticker` fields cannot tell the old code from the new.
    """
    from engine.media.voice import caption_timings
    from engine.assembly import stickers as stk
    from tests.factories import make_plan, shipped_settings

    plan = make_plan(beats=4, measured=4.0)
    for beat, text in zip(plan.script.beats, [
            "ek ladki college gayi thi yahan",
            "usne paani piya tha wahan",
            "phir ek aur line yahan par",
            "aur paani gira tha neeche"]):
        beat.caption_text = text
        beat.words = caption_timings(text, 4.0)

    cues = [c for c in stk.find_cues(plan) if c.name == "water"]
    assert len(cues) == 2, f"needed two water cues, got {cues}"
    first, second = cues[0].beat_id, cues[1].beat_id
    assert first != second

    settings = shipped_settings(work_dir=str(tmp_path), stickers=True)
    size = stk.sticker_size(settings.width, settings.sticker_scale)
    fps = int(settings.fps)
    root = sc.cache_root(settings)
    by_id = {b.beat_id: b for b in plan.script.beats}
    picks = {first: "27-globe", second: "1875-planet"}
    for beat_id, slug in picks.items():
        _fake_bake(root, slug, stk.style_for_role(by_id[beat_id].role),
                   size=size, fps=fps)

    prepared = stk.prepare(plan, settings, choices=picks)
    index = {b.beat_id: n for n, b in enumerate(plan.script.beats)}
    patterns = {s.beat_index: s.pattern for s in prepared}
    for beat_id, slug in picks.items():
        want = sc.bake_key(slug, stk.style_for_role(by_id[beat_id].role),
                           size, fps)
        assert want in patterns[index[beat_id]], \
            f"{beat_id} did not resolve to {slug}"
    assert all(s.baked for s in prepared), "the Lordicon credit depends on this"


# --- two libraries in one cache -------------------------------------------
#
# A stored choice has to say which library it came from. Lordicon slugs look
# like `1875-planet` and Noto codepoints like `1f480` or `2620_fe0f`, and
# both end up as a filename and a cache directory. Qualifying the stored
# value is what keeps them apart; splitting it before any path is built is
# what keeps the separator off the filesystem.


def test_a_bare_slug_still_means_lordicon():
    """Every choice stored before a second library existed is unqualified,
    and those rows are never rewritten."""
    assert sc.split_slug("1875-planet") == ("lordicon", "1875-planet")


def test_a_qualified_slug_names_its_library():
    assert sc.split_slug("noto:1f480") == ("noto", "1f480")
    assert sc.split_slug("lordicon:27-globe") == ("lordicon", "27-globe")


def test_a_noto_codepoint_with_an_underscore_is_accepted():
    """Skin tones and variation selectors arrive as `1f44b_1f3ff`, which
    the Lordicon rule rejects. Judged per library, not by one regex."""
    assert sc.split_slug("noto:1f44b_1f3ff") == ("noto", "1f44b_1f3ff")


def test_each_library_rejects_the_other_shape():
    with pytest.raises(ValueError):
        sc.split_slug("noto:1875-planet")      # hyphens are not a codepoint
    with pytest.raises(ValueError):
        sc.split_slug("lordicon:1f44b_1f3ff")  # underscores are not a slug


def test_an_unknown_library_is_refused():
    with pytest.raises(ValueError):
        sc.split_slug("elsewhere:1f480")


def test_the_separator_never_reaches_a_path():
    """`:` is not legal in a Windows filename and `..` must never be one.
    Both are refused before anything is built."""
    for hostile in ("noto:../../evil", "noto:/etc/passwd", "../../evil",
                    "noto:1f480:extra", "noto:"):
        with pytest.raises(ValueError):
            sc.split_slug(hostile)


def test_the_two_libraries_cannot_collide_in_the_bake_cache():
    """The same characters under two libraries are two different bakes."""
    a = sc.bake_key("lordicon:1f480", "punchy", 184, 30)
    b = sc.bake_key("noto:1f480", "punchy", 184, 30)
    assert a != b
    assert ":" not in a and ":" not in b


def test_a_noto_bake_records_notos_licence_not_lordicons(tmp_path,
                                                         monkeypatch):
    """The credit follows the art through the bake, not the default.

    `ensure_baked` used to let `bake_one` stamp its own constant, which was
    Lordicon's. With a second library that is a false claim on every emoji
    a reel uses, and a missing one on the art that actually owes it.
    """
    import json

    from engine.assembly import noto_catalog

    src = _gif(tmp_path / "src.gif")
    monkeypatch.setattr(
        sc, "_download",
        lambda library, slug, dest: src.replace(Path(dest)))

    sc.ensure_baked("noto:1f480", root=tmp_path, size=60, fps=30,
                    style="punchy")

    folder = (tmp_path / sc.BAKES_DIRNAME
              / sc.bake_key("noto:1f480", "punchy", 60, 30))
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["licence"] == noto_catalog.LICENCE
    assert "Lordicon" not in meta["licence"]

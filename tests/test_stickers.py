"""Emoji sticker pop-ups: the trigger map, the cap, and a REAL render.

Why this file leans on ffmpeg instead of on strings:

Three bugs in this branch shipped past a green suite because every assertion
was made against the filtergraph *text*. A graph is a string until ffmpeg
accepts it, and an ``overlay`` that silently composites nothing produces
exactly the same string as one that works. So the load-bearing tests here
render twice -- stickers off, stickers on -- and compare pixels at the
trigger time. Everything above the "real renders" banner is cheap unit
coverage that exists to make a regression *name itself*; it is not the proof.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from engine.assembly import stickers as stk
from engine.assembly.captions import build_ass, write_ass
from engine.assembly.render import build_filter_graph, plan_inputs, render
from engine.media.voice import caption_timings
from tests.factories import make_plan, shipped_settings

# The fifteen concepts the starter vocabulary has to cover.
STARTER_CONCEPTS = ("money", "shock", "death", "question", "fire", "night",
                    "witness", "danger", "secret", "ghost", "science",
                    "location", "time", "water", "mountain")


def _timed(beats=4, measured=4.0, captions=None):
    """A plan whose beats carry caption-aligned word timings."""
    plan = make_plan(beats=beats, measured=measured)
    if captions:
        for beat, text in zip(plan.script.beats, captions):
            beat.caption_text = text
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, measured)
    return plan


# --- the trigger map is data, not source ----------------------------------

def test_the_trigger_map_is_a_data_file_not_python():
    path = stk.TRIGGER_PATH
    assert path.suffix == ".json", \
        "the map must be extensible without a code change"
    raw = json.loads(path.read_text(encoding="utf-8"))
    names = {t["name"] for t in raw["triggers"]}
    missing = [c for c in STARTER_CONCEPTS if c not in names]
    assert not missing, f"starter vocabulary is missing {missing}"
    for entry in raw["triggers"]:
        assert entry["emoji"], entry
        assert entry["match"], entry
        assert isinstance(entry["weight"], int), entry


def test_a_new_trigger_needs_no_code_change(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"triggers": [
        {"name": "telescope", "emoji": "\U0001F52D", "weight": 9,
         "match": ["doorbeen*"]}]}), encoding="utf-8")
    plan = _timed(beats=1, captions=["Usne doorbeen se dekha tha"])
    cues = stk.find_cues(plan, triggers=stk.load_triggers(path), cap=3)
    assert [c.name for c in cues] == ["telescope"]


def test_devanagari_aliases_are_carried_for_mixed_script_captions():
    """Timings are caption-aligned, so Roman is what we match -- but a
    caption that mixes in Devanagari must not go silently unmatched."""
    triggers = stk.load_triggers()
    deva = [t for t in triggers
            if any(any('ऀ' <= ch <= 'ॿ' for ch in alias)
                   for alias in t.match)]
    assert len(deva) >= 10, "most concepts should carry a Devanagari alias"
    plan = _timed(beats=1, captions=["वहाँ एक "
                                     "कंकाल "
                                     "मिला"])
    assert [c.name for c in stk.find_cues(plan, cap=3)] == ["death"]


# --- matching, the cap, and restraint --------------------------------------

def test_matches_roman_hinglish_caption_words():
    plan = _timed(beats=3, captions=[
        "Roopkund jheel mein 500 kankaal mile",
        "Raat ko wahan kisi ne kuch dekha nahi",
        "Ye raaz aaj tak band hai"])
    names = [c.name for c in stk.find_cues(plan, cap=9, min_gap=0.0)]
    # kankaal (death, the strongest word in beat 0) beats jheel (water).
    assert names[0] == "death"
    assert "secret" in names


def test_the_cap_limits_how_many_fire_and_is_configurable():
    plan = _timed(beats=10)
    assert stk.DEFAULT_CAP == 3, "restraint is the feature; 3 is the default"
    assert len(stk.find_cues(plan, cap=3, min_gap=0.0)) == 3
    assert len(stk.find_cues(plan, cap=1, min_gap=0.0)) == 1
    assert stk.find_cues(plan, cap=0) == []


def test_at_most_one_sticker_per_beat():
    plan = _timed(beats=10)
    cues = stk.find_cues(plan, cap=99, min_gap=0.0)
    beats = [c.beat_index for c in cues]
    assert len(beats) == len(set(beats)), f"two stickers in one beat: {beats}"


def test_the_strongest_moment_wins_when_the_cap_bites():
    plan = _timed(beats=2, captions=["Ye ek shaant jheel hai",
                                     "Wahan maut hui thi"])
    assert [c.name for c in stk.find_cues(plan, cap=1, min_gap=0.0)] == \
        ["death"]


def test_two_stickers_never_land_on_top_of_each_other():
    plan = _timed(beats=10, measured=1.0)
    cues = stk.find_cues(plan, cap=5, min_gap=2.5)
    gaps = [b.start - a.start for a, b in zip(cues, cues[1:])]
    assert all(gap >= 2.5 - 1e-6 for gap in gaps), gaps


def test_cues_come_back_in_timeline_order():
    plan = _timed(beats=10)
    starts = [c.start for c in stk.find_cues(plan, cap=3)]
    assert starts == sorted(starts)


def test_a_beat_without_word_timings_gets_no_sticker():
    plan = _timed(beats=3)
    for beat in plan.script.beats:
        beat.words = []
    assert stk.find_cues(plan, cap=3) == []


def test_a_cue_too_close_to_the_end_is_dropped():
    """The pop needs room; half a sticker at the last frame is a glitch."""
    plan = _timed(beats=1, measured=0.1, captions=["Maut"])
    assert stk.find_cues(plan, cap=3) == []


# --- placement, against the captions' own numbers --------------------------

def test_the_sticker_sits_clear_of_the_burned_in_captions():
    """Read the caption band out of captions.py rather than restating it."""
    margin_v = inspect.signature(build_ass).parameters["margin_v"].default
    width, height = 1080, 1920
    size = stk.sticker_size(width)
    # The Default style is Alignment 2 (bottom centre) at MarginV; allow
    # three wrapped lines of the 72px caption font at libass' ~1.2 spacing.
    caption_top = height - margin_v - 3 * 72 * 1.2
    for slot in range(6):
        x, y, w, h = stk.sticker_box(slot, width, height, size)
        assert y + h < caption_top, f"slot {slot} collides with the captions"
        # The Punch style is Alignment 5 -- dead centre. Stay above it.
        assert y + h < height * 0.42, f"slot {slot} collides with the punch"
        assert y > height * 0.05, f"slot {slot} is under the platform chrome"
        assert 0 <= x and x + w <= width, f"slot {slot} is off-frame"


def test_consecutive_stickers_do_not_reuse_the_same_spot():
    boxes = [stk.sticker_box(slot, 1080, 1920, stk.sticker_size(1080))
             for slot in range(3)]
    assert len({b[0] for b in boxes}) == 3


# --- the pop curve ---------------------------------------------------------

def test_the_pop_overshoots_and_then_settles():
    peak = max(stk.pop_scale(t / 1000) for t in range(0, 300))
    assert peak > 1.15, "no overshoot: this will read as a fade, not a pop"
    assert stk.pop_scale(0.0) < 0.05
    assert stk.pop_scale(stk.POP_SECONDS + 0.01) == pytest.approx(1.0)
    assert stk.pop_scale(5.0) == pytest.approx(1.0)
    assert 0.15 <= stk.POP_SECONDS <= 0.25, "the brief says roughly 0.2s"


# --- graph shape (cheap regression naming, NOT the proof) ------------------

def _graph_with_stickers(tmp_path, ass_path=None):
    plan = _timed(beats=3, measured=4.0)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"still{i}.png"
    settings = shipped_settings()
    settings.work_dir = tmp_path
    prepared = stk.prepare(plan, settings)
    if not prepared:                      # pragma: no cover - env guard
        pytest.skip("no colour emoji font on this machine")
    offset = len(plan_inputs(plan)) + len(plan.script.beats)
    graph, _total, _label = build_filter_graph(
        plan, audio_offset=len(plan_inputs(plan)), ass_path=ass_path,
        stickers=prepared, sticker_offset=offset)
    return graph, prepared


def test_the_overlay_is_applied_before_the_captions_are_burned(tmp_path):
    graph, _ = _graph_with_stickers(tmp_path, ass_path="captions.ass")
    assert graph.index("overlay=") < graph.index("subtitles="), (
        "captions must burn last and on top: the sticker is decoration, "
        "the caption is the information")


def test_every_sticker_label_declares_the_same_timebase(tmp_path):
    graph, prepared = _graph_with_stickers(tmp_path)
    parts = [p for p in graph.split(";")
             if "overlay=" in p or "]format=rgba" in p]
    assert len(parts) >= 2 * len(prepared)
    for part in parts:
        assert "settb=1/30" in part, \
            f"sticker label without a timebase: {part}"


def test_no_stickers_means_a_graph_identical_to_the_old_one():
    plan = _timed(beats=3, measured=4.0)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"still{i}.png"
    offset = len(plan_inputs(plan))
    bare, _, _ = build_filter_graph(plan, audio_offset=offset)
    empty, _, _ = build_filter_graph(plan, audio_offset=offset, stickers=[])
    assert bare == empty


def test_sticker_inputs_land_after_the_music(tmp_path):
    """The one thing that would quietly break every other graph label.

    `audio_offset` and `music_index` are positions in the argv. Inserting a
    sticker input anywhere but the end would renumber the narration streams
    and the music bed underneath them, and the graph would still be a
    perfectly valid string pointed at the wrong inputs.
    """
    import tempfile
    from engine.assembly.render import build_command
    from engine.config import Settings

    plan = _timed(beats=3, measured=4.0)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"C:/tmp/img{i}.png"
        beat.audio_path = f"C:/tmp/a{i}.mp3"
    settings = Settings()
    settings.work_dir = tmp_path
    settings.stickers = True
    # The sound effects append after the stickers, for the same reason the
    # stickers append after the music. Off here so that "every input past
    # the music is a sticker" stays the exact claim this test makes; where
    # the sounds land is pinned in tests/test_audio.py.
    settings.sfx = False
    if not stk.prepare(plan, settings):          # pragma: no cover
        pytest.skip("no colour emoji font on this machine")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        handle.write(b"x")
        music = handle.name
    try:
        command, _, _ = build_command(plan, settings,
                                      Path("C:/tmp/out.mp4"),
                                      music_path=music)
        inputs = [command[i + 1] for i, arg in enumerate(command)
                  if arg == "-i"]
        assert len(inputs) > 7, "no sticker inputs were added"
        # 3 stills, 3 narration, then the music -- exactly where they were.
        assert [Path(p).name for p in inputs[3:6]] == ["a0.mp3", "a1.mp3",
                                                       "a2.mp3"]
        assert Path(inputs[6]).name == Path(music).name
        graph = command[command.index("-filter_complex") + 1]
        assert "[3:a][4:a][5:a]concat=n=3" in graph
        assert "[6:a]volume=" in graph
        # ...and every sticker is one of the inputs after it.
        for name in inputs[7:]:
            assert name.endswith(".png")
        assert f"[{len(inputs) - 1}:v]format=rgba" in graph
        assert all(Path(p).is_absolute() for p in inputs)
    finally:
        Path(music).unlink()


# --- configuration ---------------------------------------------------------

def test_the_off_switch_mirrors_rahasya_video_grade(monkeypatch):
    from engine.config import Settings
    monkeypatch.setenv("RAHASYA_STICKERS", "0")
    assert Settings().stickers is False
    monkeypatch.setenv("RAHASYA_STICKERS", "off")
    assert Settings().stickers is False
    monkeypatch.setenv("RAHASYA_STICKERS", "1")
    assert Settings().stickers is True


def test_the_cap_is_configurable_by_environment(monkeypatch):
    from engine.config import Settings
    monkeypatch.setenv("RAHASYA_STICKER_MAX", "5")
    assert Settings().sticker_max == 5


def test_env_example_documents_every_sticker_key():
    text = Path(".env.example").read_text(encoding="utf-8")
    for key in ("RAHASYA_STICKERS", "RAHASYA_STICKER_MAX",
                "RAHASYA_STICKER_FONT", "RAHASYA_STICKER_SCALE"):
        assert key in text, f"{key} is undocumented"


def test_switching_stickers_off_prepares_nothing(tmp_path):
    plan = _timed(beats=4)
    settings = shipped_settings()
    settings.work_dir = tmp_path
    settings.stickers = False
    assert stk.prepare(plan, settings) == []


def test_a_missing_emoji_font_disables_stickers_instead_of_failing(tmp_path):
    plan = _timed(beats=4)
    settings = shipped_settings()
    settings.work_dir = tmp_path
    settings.sticker_font = str(tmp_path / "nope.ttf")
    assert stk.prepare(plan, settings) == []


# --- the PNGs ---------------------------------------------------------------

def test_sticker_pngs_are_full_colour_on_transparency_and_cached(tmp_path):
    from PIL import Image
    png = stk.render_sticker_png("\U0001F480", tmp_path, px=160)
    img = Image.open(png).convert("RGBA")
    raw = img.tobytes()
    pixels = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]
    opaque = {p[:3] for p in pixels if p[3] > 200}
    assert len(opaque) > 50, f"not a colour glyph: {len(opaque)} colours"
    assert any(p[3] == 0 for p in pixels), "no transparent background"
    assert img.getpixel((0, 0))[3] == 0
    stamp = Path(png).stat().st_mtime_ns
    assert stk.render_sticker_png("\U0001F480", tmp_path, px=160) == png
    assert Path(png).stat().st_mtime_ns == stamp, "cache was rewritten"


def test_every_emoji_in_the_map_actually_has_a_colour_glyph(tmp_path):
    """One tofu box shipped into a video is worse than no sticker at all.

    Three of the chosen emoji are variation sequences -- U+26A0 FE0F,
    U+1F3D4 FE0F, U+1F5D3 FE0F -- and a font that lacks the emoji-presentation
    form draws the monochrome text glyph, or an empty box, without ever
    raising. `render_sticker_png` only fails when the glyph is *entirely*
    blank, so the colour count is what separates a real emoji from a
    rectangle. Checked on every entry, because the map is meant to be
    extended by hand and a bad codepoint is the obvious way to break it.
    """
    from PIL import Image

    if not Path(stk.DEFAULT_FONT).exists():      # pragma: no cover - env
        pytest.skip("no colour emoji font on this machine")
    thin = []
    for trigger in stk.load_triggers():
        png = stk.render_sticker_png(trigger.emoji, tmp_path, px=128)
        raw = Image.open(png).convert("RGBA").tobytes()
        pixels = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]
        opaque = {p[:3] for p in pixels if p[3] > 200}
        if len(opaque) <= 50:
            thin.append(f"{trigger.name} ({stk._codepoints(trigger.emoji)}): "
                        f"{len(opaque)} colours")
    assert not thin, "monochrome or missing glyphs: " + "; ".join(thin)


# --- real renders ----------------------------------------------------------
# Everything below invokes ffmpeg. This is the part that can fail when the
# graph is well-formed but composites nothing.

def _run_ffmpeg(ffmpeg, args):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


def _render_settings(tmp_path, width=360, height=640):
    from engine.config import Settings
    settings = Settings()
    if not Path(settings.ffmpeg).exists():   # pragma: no cover - env guard
        pytest.skip("ffmpeg is not available in this environment")
    if not Path(settings.sticker_font).exists():   # pragma: no cover
        pytest.skip("no colour emoji font on this machine")
    settings.width, settings.height, settings.fps = width, height, 30
    settings.transition_duration = 0.4
    settings.work_dir = tmp_path
    settings.out_dir = tmp_path
    # The grain re-rolls every pixel of every frame, so a graded render
    # cannot be differenced against an ungraded one. The grade is proved in
    # tests/test_assembly.py; this file is about the overlay.
    settings.video_grade = False
    settings.stickers = True
    return settings


def _flat_still(ffmpeg, path, colour, width, height):
    _run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i",
                         f"color=c={colour}:size={width}x{height}",
                         "-frames:v", "1", str(path)])


def _source_audio(ffmpeg, path, seconds):
    _run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i",
                         f"sine=frequency=220:duration={seconds}",
                         "-ar", "48000", "-ac", "1", str(path)])


def _sticker_plan(tmp_path, settings, seconds=2.2):
    """Three flat-grey beats, each carrying one trigger word.

    Flat and motionless on purpose: the only thing that may differ between
    the stickers-on and stickers-off renders is the sticker itself.
    """
    captions = ["Jungle mein ek kankaal mila tha",
                "Raat ko wahan koi nahi jaata",
                "Ye raaz aaj tak band hai"]
    plan = make_plan(beats=3, measured=seconds)
    for index, (beat, text) in enumerate(zip(plan.script.beats, captions)):
        beat.role = "setup"      # no push: hold the picture dead still
        beat.caption_text = text
        beat.words = caption_timings(text, seconds)
        still = tmp_path / f"{beat.beat_id}.png"
        _flat_still(settings.ffmpeg, still,
                    ("0x202020", "0x2f2f2f", "0x262626")[index],
                    settings.width, settings.height)
        beat.image_path = str(still)
        audio = tmp_path / f"{beat.beat_id}.wav"
        _source_audio(settings.ffmpeg, audio, seconds)
        beat.audio_path = str(audio)
    return plan


def _frame_rgb(ffmpeg, path, at):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}",
         "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], check=True, capture_output=True)
    assert result.stdout, f"no frame at {at}s of {path}"
    return result.stdout


def _changed_span(left, right, row, width, threshold=24):
    """The x-range on ``row`` where two rgb24 frames disagree."""
    xs = [x for x in range(width)
          if max(abs(left[(row * width + x) * 3 + c]
                     - right[(row * width + x) * 3 + c]) for c in range(3))
          > threshold]
    return (min(xs), max(xs)) if xs else None


def _changed_pixels(left, right, threshold=24):
    return sum(1 for i in range(0, len(left), 3)
               if max(abs(left[i + c] - right[i + c]) for c in range(3))
               > threshold)


def test_a_real_render_with_stickers_still_equals_the_narration_total(
        tmp_path):
    """The invariant the whole pipeline rests on, with overlays in the graph.

    ``segment_lengths`` pads every beat by half an overlap per side so the
    picture lands exactly on the narration. An overlay must not move it.
    """
    from engine.assembly.render import probe_video

    settings = _render_settings(tmp_path)
    plan = _sticker_plan(tmp_path, settings)
    total = sum(beat.seconds() for beat in plan.script.beats)
    assert stk.prepare(plan, settings), "fixture produced no stickers"

    out = tmp_path / "stickered.mp4"
    render(plan, settings, out)

    probe = probe_video(out, settings.ffmpeg)
    assert probe["bytes"] > 0
    assert probe["duration"] == pytest.approx(total, abs=0.05)
    assert (probe["width"], probe["height"]) == (settings.width,
                                                 settings.height)
    assert probe["has_audio"]


def test_the_sticker_is_actually_visible_at_its_trigger_word(tmp_path):
    """Pixels, not strings.

    Render the same plan twice, stickers off and on, and difference the
    frames. Before the trigger the two renders must agree; a moment after it
    they must disagree, and the disagreement must sit in the box the sticker
    was placed in.
    """
    settings = _render_settings(tmp_path)
    plan = _sticker_plan(tmp_path, settings)
    cues = stk.prepare(plan, settings)
    assert cues, "fixture produced no stickers"
    cue = cues[0]
    size = stk.sticker_size(settings.width)
    x, y, w, h = stk.sticker_box(0, settings.width, settings.height, size)

    settings.stickers = False
    plain = tmp_path / "plain.mp4"
    render(plan, settings, plain)

    settings.stickers = True
    popped = tmp_path / "popped.mp4"
    render(plan, settings, popped)

    # Before the trigger the two renders are the same picture.
    before_at = max(cue.start - 0.25, 0.05)
    before = _changed_pixels(_frame_rgb(settings.ffmpeg, plain, before_at),
                             _frame_rgb(settings.ffmpeg, popped, before_at))
    assert before < 0.002 * settings.width * settings.height, (
        f"{before} pixels differ before the trigger: the sticker is early")

    # Settled at its resting size a moment later.
    at = cue.start + 0.40
    off = _frame_rgb(settings.ffmpeg, plain, at)
    on = _frame_rgb(settings.ffmpeg, popped, at)
    span = _changed_span(off, on, y + h // 2, settings.width)
    assert span is not None, "nothing changed: the overlay rendered nothing"
    assert span[0] >= x - 3 and span[1] <= x + w + 3, (
        f"the sticker is outside its box: changed {span}, box x={x} w={w}")
    settled = span[1] - span[0] + 1
    assert settled > 0.5 * w, f"the sticker is barely there: {settled}px"

    # It leaves again. A sticker that never goes away is not punctuation,
    # it is a watermark -- and `enable`, the fade and the trimmed hold are
    # three separate things that have to agree for it to end.
    after_at = cue.start + stk.HOLD_SECONDS + 0.20
    assert after_at < sum(b.seconds() for b in plan.script.beats), (
        "fixture is too short to see the sticker leave")
    after = _changed_pixels(_frame_rgb(settings.ffmpeg, plain, after_at),
                            _frame_rgb(settings.ffmpeg, popped, after_at))
    assert after < 0.002 * settings.width * settings.height, (
        f"{after} pixels still differ {after_at - cue.start:.1f}s in: "
        f"the sticker never leaves")

    # ...and nothing outside the box moved.
    outside = sum(
        1 for row in range(settings.height)
        for col in range(settings.width)
        if not (x - 4 <= col <= x + w + 4 and y - 4 <= row <= y + h + 4)
        and max(abs(off[(row * settings.width + col) * 3 + c]
                    - on[(row * settings.width + col) * 3 + c])
                for c in range(3)) > 40)
    assert outside < 0.003 * settings.width * settings.height, (
        f"{outside} pixels changed outside the sticker box")


def test_the_pop_overshoots_on_screen_not_just_in_the_expression(tmp_path):
    """The overshoot is what makes it read as a pop rather than a fade.

    Measure the sticker's width on screen mid-pop and at rest. If the graph
    ramps the size linearly -- or not at all -- these are equal.
    """
    settings = _render_settings(tmp_path)
    plan = _sticker_plan(tmp_path, settings)
    cues = stk.prepare(plan, settings)
    cue = cues[0]
    size = stk.sticker_size(settings.width)
    x, y, w, h = stk.sticker_box(0, settings.width, settings.height, size)
    row = y + h // 2

    settings.stickers = False
    plain = tmp_path / "plain.mp4"
    render(plan, settings, plain)
    settings.stickers = True
    popped = tmp_path / "popped.mp4"
    render(plan, settings, popped)

    def width_at(offset):
        at = cue.start + offset
        span = _changed_span(_frame_rgb(settings.ffmpeg, plain, at),
                             _frame_rgb(settings.ffmpeg, popped, at),
                             row, settings.width)
        return (span[1] - span[0] + 1) if span else 0

    settled = width_at(0.50)
    assert settled > 0.5 * w, \
        f"no settled sticker to compare against ({settled})"
    peaks = [width_at(off) for off in (0.10, 0.13, 0.16)]
    assert max(peaks) > settled * 1.08, (
        f"no overshoot on screen: mid-pop widths {peaks} vs "
        f"settled {settled}")


def test_a_real_render_survives_stickers_captions_and_mixed_clip_counts(
        tmp_path):
    """The production shape: video clips, a still fallback, burned captions
    and overlays in one graph. ffmpeg runs with cwd set to the caption
    directory, so every sticker PNG must be referenced absolutely."""
    from engine.assembly.render import probe_video
    from tests.test_assembly import _mixed_clip_count_plan

    settings = _render_settings(tmp_path)
    plan = _mixed_clip_count_plan(tmp_path, settings.ffmpeg)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, beat.seconds())
    total = sum(beat.seconds() for beat in plan.script.beats)
    assert stk.prepare(plan, settings), "fixture produced no stickers"

    ass = tmp_path / "captions" / "captions.ass"
    write_ass(plan, ass, font="Arial", font_size=28,
              width=settings.width, height=settings.height, margin_v=90)

    out = tmp_path / "full.mp4"
    render(plan, settings, out, ass_path=str(ass))

    probe = probe_video(out, settings.ffmpeg)
    assert probe["bytes"] > 0
    assert probe["duration"] == pytest.approx(total, abs=0.05)

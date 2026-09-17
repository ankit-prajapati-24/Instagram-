import pytest

from engine.assembly.captions import ass_time, build_ass, escape_ass
from engine.assembly.compile import to_generate_video_body
from engine.assembly.render import build_filter_graph, zoompan_expr
from engine.media.voice import caption_timings
from tests.factories import make_plan


# --- captions ---------------------------------------------------------------

def test_ass_time_format():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3.5) == "0:00:03.50"
    assert ass_time(65.25) == "0:01:05.25"
    assert ass_time(-2) == "0:00:00.00"


def test_escape_ass_neutralises_markup():
    assert escape_ass("a{b}c") == "a\\{b\\}c"
    assert escape_ass("line1\nline2") == "line1\\Nline2"


def _timed_plan(beats=3, measured=4.0):
    plan = make_plan(beats=beats, measured=measured)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, measured)
    return plan


def test_ass_header_declares_vertical_canvas():
    out = build_ass(_timed_plan())
    assert "[Script Info]" in out
    assert "PlayResX: 1080" in out
    assert "PlayResY: 1920" in out
    assert "[V4+ Styles]" in out
    assert "[Events]" in out


def test_one_dialogue_line_per_beat():
    out = build_ass(_timed_plan(beats=4))
    assert out.count("\nDialogue: 0,") == 4


def test_karaoke_tags_present_and_beats_are_sequential():
    plan = _timed_plan(beats=3, measured=4.0)
    out = build_ass(plan)
    assert "{\\k" in out
    assert "Dialogue: 0,0:00:00.00,0:00:04.00" in out
    assert "Dialogue: 0,0:00:04.00,0:00:08.00" in out
    assert "Dialogue: 0,0:00:08.00,0:00:12.00" in out


def test_karaoke_durations_sum_to_the_beat():
    import re
    plan = _timed_plan(beats=1, measured=4.0)
    tags = [int(t) for t in re.findall(r"\\k(\d+)", build_ass(plan))]
    assert sum(tags) == pytest.approx(400, abs=len(tags))


def test_untimed_beat_falls_back_to_a_plain_line():
    plan = make_plan(beats=2, measured=4.0)  # no words assigned
    out = build_ass(plan)
    assert "{\\k" not in out
    assert "Roopkund jheel mein 500 kankaal mile" in out


def test_devanagari_source_switch_emits_untagged_devanagari():
    plan = _timed_plan(beats=2)
    out = build_ass(plan, source="voice_text")
    assert "रूपकुंड" in out
    # Roman timings must not be applied to Devanagari words.
    assert "{\\k" not in out


def test_on_screen_punch_gets_its_own_layer():
    plan = _timed_plan(beats=2)
    plan.script.beats[1].on_screen_text = "SAB EK RAAT MEIN"
    out = build_ass(plan)
    assert "Dialogue: 1," in out
    assert "Punch" in out
    assert "SAB EK RAAT MEIN" in out


# --- zoompan ----------------------------------------------------------------

def test_zoom_in_grows_and_zoom_out_shrinks():
    assert "zoom+" in zoompan_expr("zoom_in", 3.0, 30)
    assert "1.30-" in zoompan_expr("zoom_out", 3.0, 30)


def test_pans_differ_by_direction():
    left = zoompan_expr("move_left", 3.0, 30)
    right = zoompan_expr("move_right", 3.0, 30)
    assert left != right
    assert "x='(iw-iw/zoom)*(1-on/90)'" in left
    assert "x='(iw-iw/zoom)*(on/90)'" in right


def test_frame_count_and_canvas_track_arguments():
    expr = zoompan_expr("zoom_in", 3.0, 30)
    assert ":d=90" in expr
    assert "s=1080x1920" in expr
    assert ":fps=30" in expr


def test_unknown_motion_degrades_to_a_static_hold():
    assert "1.06" in zoompan_expr("barrel_roll", 2.0, 30)


# --- filter graph -----------------------------------------------------------

def test_graph_has_one_segment_per_beat_and_chains_them():
    graph, total, label = build_filter_graph(_timed_plan(beats=3),
                                             audio_offset=3)
    assert graph.count("zoompan=") == 3
    assert graph.count("xfade=") == 2
    assert label == "x2"
    # 3 beats x 4s, minus two 0.5s overlaps
    assert total == pytest.approx(11.0)


def test_single_beat_graph_needs_no_xfade():
    graph, total, label = build_filter_graph(_timed_plan(beats=1),
                                             audio_offset=1)
    assert "xfade=" not in graph
    assert label == "v0"
    assert total == pytest.approx(4.0)


def test_audio_offset_shifts_narration_labels_past_the_images():
    graph, _, _ = build_filter_graph(_timed_plan(beats=3), audio_offset=3)
    assert "[3:a][4:a][5:a]concat=n=3" in graph
    assert "[0:a]" not in graph


def test_captions_are_burned_after_the_chain():
    graph, _, label = build_filter_graph(_timed_plan(beats=2),
                                         audio_offset=2,
                                         ass_path="C:/tmp/a.ass")
    assert "subtitles=" in graph
    assert label == "vout"


def test_loudnorm_is_always_applied():
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2)
    assert "loudnorm=I=-14" in graph


def test_music_bed_is_mixed_when_supplied():
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2,
                                     music_index=4)
    assert "[4:a]volume=-18.0dB" in graph
    assert "amix=inputs=2" in graph


def test_empty_plan_is_rejected():
    plan = make_plan(beats=1)
    plan.script.beats = []
    with pytest.raises(ValueError):
        build_filter_graph(plan)


# --- legacy endpoint compiler ----------------------------------------------

def test_compile_body_matches_the_existing_endpoint_shape():
    plan = _timed_plan(beats=3)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"img{i}.png"
        beat.audio_path = f"a{i}.mp3"

    body = to_generate_video_body(plan, "out.mp4")
    assert body["settings"]["frame_size"] == [1080, 1920]
    assert body["settings"]["layout_mode"] == "blur_bg"
    assert body["output_name"] == "out.mp4"
    assert len(body["images"]) == 3
    assert body["images"][0]["duration"] == pytest.approx(4.0)
    assert body["images"][0]["motion"] == "zoom_in"
    assert set(body["images"][0]) == {"path", "duration", "transition",
                                      "motion", "motion_speed"}


def test_compile_rejects_plans_without_images():
    with pytest.raises(ValueError, match="without an image"):
        to_generate_video_body(_timed_plan(beats=2))


def test_compile_maps_unsupported_values_to_legacy_vocabulary():
    plan = _timed_plan(beats=1)
    plan.script.beats[0].image_path = "a.png"
    plan.script.beats[0].audio_path = "a.mp3"
    plan.script.beats[0].transition = "blur"
    body = to_generate_video_body(plan)
    assert body["images"][0]["transition"] == "blur"

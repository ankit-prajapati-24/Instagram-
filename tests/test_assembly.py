import pytest

from engine.assembly.captions import ass_time, build_ass, escape_ass
from engine.assembly.compile import to_generate_video_body
from engine.assembly.render import (build_filter_graph, plan_inputs,
                                    segment_lengths, zoompan_expr)
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
    # 3 beats x 4s. The picture must land on the narration timeline, NOT be
    # compressed by one overlap per join — that drift truncated the last
    # beat's voice and desynced every caption after the first cut.
    assert total == pytest.approx(12.0)


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


# --- the A/V timeline, which this pipeline got wrong once -------------------
# The narration is a plain concat and the picture is an xfade chain. Every
# xfade consumes time from both inputs, so an unpadded chain ran 0.5s short
# per join: on a ten-beat reel the last 4.5s of voice was cut off by the
# output duration and every caption after the first cut drifted.

def test_segment_lengths_preserve_the_narration_timeline():
    durations = [4.0] * 10
    lengths, offsets, overlaps = segment_lengths(durations, 0.5)
    # composite length = last offset + last segment
    assert offsets[-1] + lengths[-1] == pytest.approx(sum(durations))


def test_segments_are_padded_by_half_an_overlap_per_join():
    lengths, _, _ = segment_lengths([4.0, 4.0, 4.0], 0.5)
    assert lengths[0] == pytest.approx(4.25)   # outgoing join only
    assert lengths[1] == pytest.approx(4.5)    # both sides
    assert lengths[2] == pytest.approx(4.25)   # incoming join only


def test_transitions_are_centred_on_the_audio_cut():
    _, offsets, overlaps = segment_lengths([3.0, 5.0, 4.0], 0.5)
    # cuts fall at 3.0 and 8.0; each blend straddles its cut
    assert offsets[0] + overlaps[0] / 2 == pytest.approx(3.0)
    assert offsets[1] + overlaps[1] / 2 == pytest.approx(8.0)


def test_overlap_never_eats_more_than_40_percent_of_a_short_beat():
    _, _, overlaps = segment_lengths([0.6, 4.0], 0.5)
    assert overlaps[0] == pytest.approx(0.24)


def test_uneven_beats_still_land_on_the_narration_total():
    durations = [2.1, 6.4, 3.3, 0.9, 5.0]
    lengths, offsets, _ = segment_lengths(durations, 0.5)
    assert offsets[-1] + lengths[-1] == pytest.approx(sum(durations))


def test_single_beat_needs_no_padding():
    lengths, offsets, overlaps = segment_lengths([4.0], 0.5)
    assert lengths == [4.0]
    assert offsets == [] and overlaps == []


def test_graph_total_always_equals_the_sum_of_beat_durations():
    for count in (1, 2, 3, 7, 12):
        plan = _timed_plan(beats=count, measured=4.0)
        _, total, _ = build_filter_graph(plan, audio_offset=count)
        assert total == pytest.approx(sum(b.seconds()
                                          for b in plan.script.beats))


def test_caption_offsets_match_the_graph_timeline():
    """Captions accumulate raw beat lengths, so the graph must too."""
    plan = _timed_plan(beats=5, measured=4.0)
    _, total, _ = build_filter_graph(plan, audio_offset=5)
    ass = build_ass(plan)
    last = [line for line in ass.splitlines()
            if line.startswith("Dialogue: 0,")][-1]
    end = last.split(",")[2]
    assert ass_time(total) == end


def _command_for(beats=3, measured=4.0, music=None):
    from pathlib import Path
    from engine.assembly.render import build_command
    from engine.config import Settings
    plan = _timed_plan(beats=beats, measured=measured)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"C:/tmp/img{i}.png"
        beat.audio_path = f"C:/tmp/a{i}.mp3"
    return build_command(plan, Settings(), Path("C:/tmp/out.mp4"),
                         music_path=music)


def test_images_are_fed_as_single_frames_not_looped():
    """zoompan's `d` is output frames per input frame.

    With `-loop 1 -t X` the image2 demuxer supplies many input frames, and a
    4-second beat became a 400-second segment that only looked right because
    the output -t truncated it.
    """
    command, _, _ = _command_for()
    assert "-loop" not in command
    # -t appears exactly once, as the output duration
    assert command.count("-t") == 1
    assert command[command.index("-t") + 1] == "12.000"


def test_input_order_is_images_then_audio():
    """The graph's [N:v] and [N:a] labels depend on this exact order."""
    from pathlib import Path
    command, _, _ = _command_for(beats=3)
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert len(inputs) == 6
    assert [Path(p).name for p in inputs[:3]] == ["img0.png", "img1.png",
                                                  "img2.png"]
    assert [Path(p).name for p in inputs[3:]] == ["a0.mp3", "a1.mp3",
                                                  "a2.mp3"]
    assert all(Path(p).is_absolute() for p in inputs)


def test_music_input_lands_after_every_beat_stream():
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        handle.write(b"x")
        music = handle.name
    command, _, _ = _command_for(beats=3, music=music)
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert Path(inputs[6]).name == Path(music).name
    graph = command[command.index("-filter_complex") + 1]
    # 3 images + 3 audio, so music is input 6
    assert "[6:a]volume=" in graph
    Path(music).unlink()


def test_output_duration_matches_the_narration_total():
    command, total, _ = _command_for(beats=7, measured=4.4)
    assert total == pytest.approx(7 * 4.4)
    assert command[command.index("-t") + 1] == f"{7 * 4.4:.3f}"


# --- clips: one segment per clip, beats still own the timeline -------------

def _clipped(plan, per_beat=2):
    """Give every beat `per_beat` video clips filling its measured span."""
    from engine.contract import Clip
    from engine.media.clips import slot_durations
    for beat in plan.script.beats:
        beat.measured_seconds = beat.measured_seconds or 4.0
        for i, d in enumerate(slot_durations(beat.seconds(), per_beat)):
            beat.clips.append(Clip(path=f"{beat.beat_id}-{i}.mp4", query="q",
                                   provider="pexels", duration=d))
    return plan


def test_one_input_per_clip_not_per_beat():
    plan = _clipped(make_plan(), per_beat=3)
    inputs = plan_inputs(plan)
    assert len(inputs) == 3 * len(plan.script.beats)
    assert all(is_video for _, is_video in inputs)


def test_a_still_fallback_input_is_flagged_as_not_video():
    plan = _clipped(make_plan(), per_beat=2)
    plan.script.beats[0].clips[0].path = "still.png"
    plan.script.beats[0].clips[0].provider = "placeholder"
    inputs = plan_inputs(plan)
    assert inputs[0] == ("still.png", False)
    assert inputs[1][1] is True


def test_video_inputs_drop_their_audio():
    plan = _clipped(make_plan(), per_beat=2)
    inputs = plan_inputs(plan)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(inputs))
    concats = [p for p in graph.split(";") if "concat=n=" in p]
    building_audio = [p for p in concats if "a=1" in p]
    assert len(building_audio) == 1, "only the narration concat builds audio"
    assert all("a=0" in p for p in concats if p not in building_audio), \
        "every per-beat concat must drop audio"
    # No video input's audio stream is referenced anywhere in the graph.
    assert all(f"[{i}:a]" not in graph for i in range(len(inputs)))


def test_no_zoompan_on_a_video_clip():
    plan = _clipped(make_plan(), per_beat=2)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert "zoompan" not in graph


def test_zoompan_survives_on_a_still_fallback():
    plan = _clipped(make_plan(), per_beat=2)
    plan.script.beats[0].clips[0].path = "still.png"
    plan.script.beats[0].clips[0].provider = "placeholder"
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert graph.count("zoompan") == 1


def test_xfade_count_matches_beat_joins_not_clip_joins():
    """Hard cuts inside a beat; crossfade only where beats meet."""
    plan = _clipped(make_plan(), per_beat=3)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert graph.count("xfade=") == len(plan.script.beats) - 1


def test_total_runtime_still_equals_the_narration():
    plan = _clipped(make_plan(), per_beat=4)
    _, total, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert total == pytest.approx(
        sum(b.seconds() for b in plan.script.beats), abs=1e-6)


def test_clip_spans_fill_their_beat_segment_exactly():
    """The clips of a beat must cover the padded segment, not the raw span.

    `lengths[i]` includes the half-overlap padding on each side that has a
    transition; laying the stored narration slots down directly would leave
    the picture short by exactly the drift segment_lengths exists to remove.
    """
    import re
    plan = _clipped(make_plan(beats=3), per_beat=2)
    lengths, _, _ = segment_lengths([b.seconds() for b in plan.script.beats],
                                    0.5)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    spans = [float(v) for v in re.findall(r"trim=duration=([\d.]+)", graph)]
    assert len(spans) == 6
    for index, length in enumerate(lengths):
        assert sum(spans[index * 2:index * 2 + 2]) == pytest.approx(
            length, abs=2e-3)


def test_a_clip_segment_is_handed_to_xfade_at_a_constant_rate():
    """setpts marks its output 1/0, and xfade refuses a variable rate.

    Caught by a real render, not by a string: ffmpeg died at graph setup
    with "The inputs needs to be a constant frame rate; current rate of 1/0
    is invalid" before a single frame was written. The fps filter after
    setpts is what re-declares the rate.
    """
    plan = _clipped(make_plan(beats=2), per_beat=2)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert graph.count("setpts=PTS-STARTPTS") == 4
    assert graph.count("setpts=PTS-STARTPTS,fps=30") == 4


def test_a_beat_with_no_clips_still_renders_from_its_image():
    """Backwards compatibility: an older stored plan has image_path only."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.image_path = f"{beat.beat_id}.png"
    inputs = plan_inputs(plan)
    assert len(inputs) == len(plan.script.beats)
    assert all(is_video is False for _, is_video in inputs)


def test_command_feeds_one_input_per_clip_and_offsets_the_audio():
    """audio_offset is the total input count once beats hold several clips."""
    from pathlib import Path
    from engine.assembly.render import build_command
    from engine.config import Settings
    plan = _clipped(_timed_plan(beats=3, measured=4.0), per_beat=2)
    for i, beat in enumerate(plan.script.beats):
        beat.audio_path = f"C:/tmp/a{i}.mp3"
    command, _, _ = build_command(plan, Settings(), Path("C:/tmp/out.mp4"))
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    assert len(inputs) == 9
    assert [Path(p).name for p in inputs[6:]] == ["a0.mp3", "a1.mp3",
                                                  "a2.mp3"]
    graph = command[command.index("-filter_complex") + 1]
    assert "[6:a][7:a][8:a]concat=n=3" in graph

"""ffmpeg render graph.

Direct filter graphs rather than moviepy. Verified on this machine: moviepy is
not installed and has no Python 3.14 wheel, while the bundled ffmpeg v7.1 has
every filter this needs — ``zoompan`` (Ken Burns), ``xfade`` (transitions),
``subtitles`` (libass burn-in), ``loudnorm`` and ``concat``. ffmpeg is also
roughly an order of magnitude faster here, because nothing crosses into Python
per frame.

The existing EDITOR-_BACKEND ``/generate-video`` endpoint stays usable as an
alternative renderer through ``engine.assembly.compile``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from engine.contract import ReelPlan

# Ken Burns strength. Deliberately gentle: a 45-second Reel with ten beats
# already has a cut every ~4s, so strong zoom on top reads as seasickness.
ZOOM_RATE = 0.0012
PAN_ZOOM = 1.18
STATIC_ZOOM = 1.06


def segment_lengths(durations: list[float],
                    transition_duration: float) -> tuple[list[float],
                                                         list[float],
                                                         list[float]]:
    """Work out how long each image must be on screen, and where cuts land.

    The narration is a plain concat, so beat i's audio starts at the running
    sum of the beats before it. That timeline is the source of truth and the
    picture has to match it.

    Every xfade consumes time from both of its inputs, so a chain of them
    compresses the picture by one overlap per join. The fix is to centre each
    transition on the audio cut and pad each segment by half an overlap on
    each side that has one. Then the composite picture is exactly as long as
    the narration, and the blend straddles the cut the way an edit normally
    does.

    Returns ``(lengths, offsets, overlaps)``: how long each segment runs, and
    the offset and duration for each xfade join.
    """
    count = len(durations)
    if count == 0:
        return [], [], []
    if count == 1:
        return [durations[0]], [], []

    # Never let a transition eat more than 40% of the shorter neighbour.
    overlaps = [
        max(min(transition_duration,
                durations[i] * 0.4,
                durations[i + 1] * 0.4), 0.0)
        for i in range(count - 1)
    ]

    cuts: list[float] = []
    running = 0.0
    for duration in durations[:-1]:
        running += duration
        cuts.append(running)

    lengths: list[float] = []
    for i, duration in enumerate(durations):
        incoming = overlaps[i - 1] if i > 0 else 0.0
        outgoing = overlaps[i] if i < count - 1 else 0.0
        lengths.append(duration + incoming / 2 + outgoing / 2)

    # xfade's offset is measured on the composite built so far, which starts
    # at absolute zero, so the offset is just the cut minus half the overlap.
    offsets = [cuts[i] - overlaps[i] / 2 for i in range(count - 1)]
    return lengths, offsets, overlaps


VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def plan_inputs(plan: ReelPlan) -> list[tuple[str, bool]]:
    """Every visual input in render order, as ``(path, is_video)``.

    A beat with no clips falls back to its ``image_path``, so plans stored
    before clips existed still render.
    """
    inputs: list[tuple[str, bool]] = []
    for beat in plan.script.beats:
        if beat.clips:
            for clip in beat.clips:
                inputs.append((clip.path,
                               Path(clip.path).suffix.lower()
                               in VIDEO_SUFFIXES))
        else:
            inputs.append((beat.image_path or "", False))
    return inputs


def zoompan_expr(motion: str, duration: float, fps: int = 30,
                 width: int = 1080, height: int = 1920) -> str:
    """One beat's Ken Burns move as a zoompan filter string.

    ``d`` is output frames per *input* frame, so this expects a single-frame
    input — ``-i image.png`` with no ``-loop``. Feeding it a looped stream
    multiplies the segment length by the input frame count: a 4-second beat
    came out as a 400-second clip that only looked right because the output
    ``-t`` truncated it.
    """
    frames = max(int(round(duration * fps)), 1)

    if motion == "zoom_in":
        zoom = f"'min(zoom+{ZOOM_RATE},1.30)'"
        x, y = "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif motion == "zoom_out":
        zoom = f"'if(lte(zoom,1.0),1.30,max(1.30-{ZOOM_RATE}*on,1.0))'"
        x, y = "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif motion == "move_left":
        # Frame starts right and travels left across the held zoom.
        zoom = f"'{PAN_ZOOM}'"
        x = f"'(iw-iw/zoom)*(1-on/{frames})'"
        y = "'ih/2-(ih/zoom/2)'"
    elif motion == "move_right":
        zoom = f"'{PAN_ZOOM}'"
        x = f"'(iw-iw/zoom)*(on/{frames})'"
        y = "'ih/2-(ih/zoom/2)'"
    else:
        zoom = f"'{STATIC_ZOOM}'"
        x, y = "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"

    return (f"zoompan=z={zoom}:x={x}:y={y}:d={frames}:"
            f"s={width}x{height}:fps={fps}")


def _loop_frames(span: float, fps: int) -> int:
    """How many frames ``loop`` must hold to fill a ``span``-second slot.

    ``loop`` buffers this many frames and then stops reading the source, so
    the cap is what bounds the filter's memory: a full-length Pexels clip
    buffered at 1080x1920 is gigabytes, while one slot's worth is tens of
    megabytes. It cannot shorten anything — the frames past the cap are the
    ones ``trim`` was going to drop anyway, and a source that ends early is
    looped from whatever it did supply. The spare frame absorbs the rounding
    between a fractional span and a whole frame.
    """
    return max(int(round(span * fps)), 1) + 1


def _xfade_name(transition: str) -> str:
    """Map our contract's transition names onto xfade's own vocabulary."""
    return {
        "fade": "fade",
        "slide_left": "slideleft",
        "slide_right": "slideright",
        "zoom": "zoomin",
        "blur": "fadeblack",
    }.get(transition, "fade")


def build_filter_graph(plan: ReelPlan, *, fps: int = 30,
                       width: int = 1080, height: int = 1920,
                       transition_duration: float = 0.5,
                       ass_path: str | None = None,
                       audio_offset: int = 0,
                       music_index: int | None = None,
                       music_gain_db: float = -18.0
                       ) -> tuple[str, float, str]:
    """Build the filter_complex string, total duration and video out-label.

    Total runtime is the sum of beat durations — the same timeline the
    narration concat produces. See ``segment_lengths`` for how the xfade chain
    is padded to land on it instead of compressing by one overlap per join.

    ``audio_offset`` is the input index where the narration streams begin. The
    render command lists every visual first — one input per clip, or one per
    beat for a plan with no clips — and then every audio file, so beat k's
    audio is input ``audio_offset + k``, passed in explicitly rather than
    rewritten afterwards, because a post-hoc regex would also catch the music
    input's label.
    """
    beats = plan.script.beats
    if not beats:
        raise ValueError("plan has no beats to render")

    parts: list[str] = []
    durations = [beat.seconds() for beat in beats]
    # The narration timeline decides everything; see segment_lengths.
    total = sum(durations)
    lengths, offsets, overlaps = segment_lengths(durations,
                                                 transition_duration)

    # --- per-clip video segments, grouped by beat ------------------------
    #
    # Beats own the timeline; clips subdivide the span a beat already holds.
    # `lengths[i]` is the beat's segment *including* its half-overlap
    # padding, so the clip slots are scaled into it proportionally rather
    # than used directly — the stored durations are the narration-timeline
    # slots, which is what the timeline invariant is asserted on, and they
    # must come back out of here untouched.
    inputs = plan_inputs(plan)
    beat_labels: list[str] = []
    cursor = 0
    # Every segment is fitted to the canvas the same way, still or video.
    common = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},setsar=1")

    for beat_index, beat in enumerate(beats):
        slots = [clip.duration for clip in beat.clips]
        slot_total = sum(slots)
        if not slots:
            # No clips: one segment, handed `lengths[i]` verbatim rather
            # than through a scale factor that round-trips it. A one-ulp
            # difference is a whole frame to zoompan, which rounds
            # `span * fps`, whenever that product lands on a .5 tie — and
            # it does at this repo's own defaults: a 4.4s beat with a 0.5s
            # transition gives 4.65 * 30 = 139.5 exactly.
            spans = [lengths[beat_index]]
        elif slot_total:
            spans = [slot * lengths[beat_index] / slot_total
                     for slot in slots]
        else:
            # Degenerate: clips exist but no slot claims any time. Share
            # the segment out evenly rather than emit zero-length
            # segments, which are empty streams ffmpeg fails on obscurely.
            spans = [lengths[beat_index] / len(slots)] * len(slots)
        clip_labels: list[str] = []

        for span in spans:
            _path, is_video = inputs[cursor]
            # A one-clip beat keeps the old label, so a plan with no clips
            # still comes out of here as v0, v1, ... exactly as before.
            label = f"v{cursor}"
            if is_video:
                # Fit the source to its slot: trim if longer, loop if
                # shorter. The source's own length never moves the timeline.
                #
                # Both `fps` filters earn their place. The first normalises
                # the source rate, so `size` below is a frame count we can
                # work out exactly; it is deliberately absent from the still
                # branch, where zoompan already emits at fps and one extra
                # frame out of an fps filter would multiply the segment (see
                # zoompan_expr). The second restores a constant frame rate
                # after setpts, which marks its output 1/0 — xfade refuses
                # to configure against that and the whole render dies at
                # graph setup.
                parts.append(
                    f"[{cursor}:v]{common},fps={fps},"
                    f"loop=loop=-1:size={_loop_frames(span, fps)}:start=0,"
                    f"trim=duration={span:.3f},setpts=PTS-STARTPTS,"
                    f"fps={fps},format=yuv420p[{label}]")
            else:
                parts.append(
                    f"[{cursor}:v]{common},"
                    f"{zoompan_expr(beat.motion, span, fps, width, height)},"
                    f"format=yuv420p[{label}]")
            clip_labels.append(label)
            cursor += 1

        # Hard cuts inside the beat: a plain concat, no overlap to pay for.
        if len(clip_labels) == 1:
            beat_labels.append(clip_labels[0])
        else:
            joined = "".join(f"[{c}]" for c in clip_labels)
            parts.append(f"{joined}concat=n={len(clip_labels)}:v=1:a=0"
                         f"[b{beat_index}]")
            beat_labels.append(f"b{beat_index}")

    # --- crossfade between beats -----------------------------------------
    if len(beats) == 1:
        video_label = beat_labels[0]
    else:
        current = beat_labels[0]
        for index in range(1, len(beats)):
            parts.append(
                f"[{current}][{beat_labels[index]}]xfade="
                f"transition={_xfade_name(beats[index].transition)}:"
                f"duration={overlaps[index - 1]:.3f}:"
                f"offset={offsets[index - 1]:.3f}[x{index}]")
            current = f"x{index}"
        video_label = current

    # --- captions --------------------------------------------------------
    if ass_path:
        # ``ass_path`` must be a bare filename here: ``render`` runs ffmpeg
        # with cwd set to the file's directory.
        #
        # A Windows absolute path cannot be escaped reliably inside a
        # filtergraph. The drive-letter colon is read as an option separator,
        # so "C:/x/a.ass" is parsed as filter option ``original_size`` and
        # fails with "Unable to parse option value ... as image size".
        # Removing the path from the graph is the only robust fix here.
        parts.append(f"[{video_label}]subtitles=filename={ass_path}[vout]")
        video_label = "vout"

    # --- audio -----------------------------------------------------------
    narration_inputs = "".join(f"[{audio_offset + i}:a]"
                               for i in range(len(beats)))
    parts.append(f"{narration_inputs}concat=n={len(beats)}:v=0:a=1[narr]")

    if music_index is not None:
        parts.append(
            f"[{music_index}:a]volume={music_gain_db}dB,"
            f"aloop=loop=-1:size=2e9,atrim=0:{total:.3f}[bed]")
        parts.append("[narr][bed]amix=inputs=2:duration=first:"
                     "dropout_transition=0,loudnorm=I=-14:TP=-1.5:LRA=11"
                     "[aout]")
    else:
        parts.append("[narr]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    return ";".join(parts), total, video_label


def build_command(plan: ReelPlan, settings, out_path: Path, *,
                  ass_path: str | None = None,
                  music_path: str | None = None
                  ) -> tuple[list[str], float, str | None]:
    """Assemble the ffmpeg argv. Split out so it can be asserted on.

    Returns ``(command, total_seconds, cwd)``. ``cwd`` is the caption
    directory when captions are burned, because a Windows absolute path cannot
    be escaped inside a filtergraph.
    """
    beats = plan.script.beats
    command: list[str] = [settings.ffmpeg, "-hide_banner", "-y"]

    # Both kinds are plain inputs. A still is still supplied as exactly one
    # frame — no -loop — because zoompan's `d` counts output frames per
    # input frame, so a looped still would multiply its segment length. Video
    # is fitted to its slot inside the graph instead. cwd moves below, so
    # paths are absolute.
    inputs = plan_inputs(plan)
    for path, _is_video in inputs:
        command += ["-i", str(Path(path).resolve())]
    for beat in beats:
        command += ["-i", str(Path(beat.audio_path).resolve())]

    music_index = None
    if music_path and Path(music_path).exists():
        music_index = len(inputs) + len(beats)
        command += ["-stream_loop", "-1", "-i",
                    str(Path(music_path).resolve())]

    ass_name = Path(ass_path).name if ass_path else None
    run_cwd = str(Path(ass_path).parent) if ass_path else None

    graph, total, video_label = build_filter_graph(
        plan, fps=settings.fps, width=settings.width, height=settings.height,
        transition_duration=settings.transition_duration, ass_path=ass_name,
        audio_offset=len(inputs), music_index=music_index)

    command += [
        "-filter_complex", graph,
        "-map", f"[{video_label}]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(settings.fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        str(out_path.resolve()),
    ]
    return command, total, run_cwd


def render(plan: ReelPlan, settings, out_path: str | Path, *,
           ass_path: str | None = None, music_path: str | None = None,
           progress=None) -> str:
    """Produce the MP4. Returns the output path."""
    beats = plan.script.beats
    missing = [b.beat_id for b in beats if not b.image_path]
    if missing:
        raise ValueError(f"beats without an image: {missing}")
    missing_audio = [b.beat_id for b in beats if not b.audio_path]
    if missing_audio:
        raise ValueError(f"beats without audio: {missing_audio}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    command, total, run_cwd = build_command(
        plan, settings, out_path, ass_path=ass_path, music_path=music_path)

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=run_cwd)
    tail: list[str] = []
    assert process.stderr is not None
    for raw in process.stderr:
        tail.append(raw)
        if len(tail) > 40:
            tail.pop(0)
        if progress:
            found = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", raw)
            if found:
                h, m, s = found.groups()
                done = int(h) * 3600 + int(m) * 60 + float(s)
                progress(min(done / total, 1.0) if total else 0.0)
    process.wait()

    if process.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(tail))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced no output at {out_path}")
    return str(out_path)


def probe_video(path: str | Path, ffmpeg: str) -> dict:
    """Read back duration and resolution from the file we just wrote."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    info: dict = {"path": str(path),
                  "bytes": Path(path).stat().st_size
                  if Path(path).exists() else 0}
    found = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if found:
        h, m, s = found.groups()
        info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
    found = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", result.stderr)
    if found:
        info["width"], info["height"] = int(found.group(1)), int(
            found.group(2))
    info["has_audio"] = "Audio:" in result.stderr
    return info

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


def zoompan_expr(motion: str, duration: float, fps: int = 30,
                 width: int = 1080, height: int = 1920) -> str:
    """One beat's Ken Burns move as a zoompan filter string."""
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

    Beats overlap by ``transition_duration`` during an xfade, so total runtime
    is the sum of beat durations minus one overlap per join.

    ``audio_offset`` is the input index where the narration streams begin. The
    render command lists every image first and then every audio file, so
    beat k's audio is input ``audio_offset + k`` — passed in explicitly rather
    than rewritten afterwards, because a post-hoc regex would also catch the
    music input's label.
    """
    beats = plan.script.beats
    if not beats:
        raise ValueError("plan has no beats to render")

    parts: list[str] = []

    # --- per-beat video segments -----------------------------------------
    for index, beat in enumerate(beats):
        duration = beat.seconds()
        parts.append(
            f"[{index}:v]scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"{zoompan_expr(beat.motion, duration, fps, width, height)},"
            f"format=yuv420p[v{index}]")

    # --- chain them with xfade -------------------------------------------
    if len(beats) == 1:
        video_label = "v0"
        total = beats[0].seconds()
    else:
        current = "v0"
        elapsed = beats[0].seconds()
        for index in range(1, len(beats)):
            nxt = f"v{index}"
            out = f"x{index}"
            overlap = min(transition_duration,
                          beats[index].seconds() * 0.5,
                          elapsed * 0.5)
            offset = max(elapsed - overlap, 0.0)
            parts.append(
                f"[{current}][{nxt}]xfade="
                f"transition={_xfade_name(beats[index].transition)}:"
                f"duration={overlap:.3f}:offset={offset:.3f}[{out}]")
            elapsed = offset + beats[index].seconds()
            current = out
        video_label = current
        total = elapsed

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

    command: list[str] = [settings.ffmpeg, "-hide_banner", "-y"]
    # cwd moves to the caption directory, so every other path must be
    # absolute.
    for beat in beats:
        command += ["-loop", "1", "-t", f"{beat.seconds():.3f}",
                    "-i", str(Path(beat.image_path).resolve())]
    for beat in beats:
        command += ["-i", str(Path(beat.audio_path).resolve())]

    music_index = None
    if music_path and Path(music_path).exists():
        music_index = len(beats) * 2
        command += ["-stream_loop", "-1", "-i",
                    str(Path(music_path).resolve())]

    # See build_filter_graph: the subtitles filter gets a bare filename and
    # ffmpeg is run from that directory.
    ass_name = Path(ass_path).name if ass_path else None
    run_cwd = str(Path(ass_path).parent) if ass_path else None

    graph, total, video_label = build_filter_graph(
        plan, fps=settings.fps, width=settings.width, height=settings.height,
        transition_duration=settings.transition_duration, ass_path=ass_name,
        audio_offset=len(beats), music_index=music_index)

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

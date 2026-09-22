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

from engine.assembly import audio as audio_mod
from engine.assembly import stickers as stickers_mod
from engine.contract import ReelPlan

# Ken Burns strength. Deliberately gentle: a 45-second Reel with ten beats
# already has a cut every ~4s, so strong zoom on top reads as seasickness.
ZOOM_RATE = 0.0012
PAN_ZOOM = 1.18
STATIC_ZOOM = 1.06

# --- the look ---------------------------------------------------------------
#
# The spec's own stated risk is that "twenty unrelated stock clips can read as
# a generic template rather than one piece". A 50-second Reel draws its 20-26
# clips from as many different Pexels creators, each shot on different glass,
# in different light, with a different camera's colour science. One grade over
# every frame is what makes them a single video: cold, desaturated, crushed
# blacks, a vignette pulling the eye to centre, and grain over the top — grain
# especially, because a shared noise floor is the thing the eye reads as "one
# piece of film" across an otherwise mismatched cut.
#
# Chosen by the user from rendered comparison frames. Do not retune by taste.
GRADE_CHAIN = ("colorbalance=rs=-0.08:gs=-0.02:bs=0.10:rm=-0.04:bm=0.06,"
               "eq=contrast=1.20:saturation=0.62:gamma=0.90,"
               "vignette=PI/4")
GRAIN_DEFAULT = 9.0

# Beats that carry a turn in the story get a slow push; every other beat is
# held still. Constant motion everywhere reads as noise and stops signifying
# anything, so the push has to be the exception to mean "look here".
PUSH_ROLES = frozenset({"hook", "reveal", "twist"})
PUSH_AMOUNT = 0.08


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


def grade_chain(grain: float = GRAIN_DEFAULT) -> str:
    """The approved grade, with the grain amount dialled in.

    ``grain`` of zero keeps the colour work and drops the noise filter
    entirely rather than passing ``alls=0``, which still costs a full pass
    over every plane to add nothing.
    """
    if grain > 0:
        return f"{GRADE_CHAIN},noise=alls={grain:g}:allf=t+u"
    return GRADE_CHAIN


def push_expr(span: float, fps: int = 30, width: int = 1080,
              height: int = 1920, amount: float = PUSH_AMOUNT) -> str:
    """A slow push from 1.0 to ``1 + amount`` over ``span`` seconds.

    Deliberately NOT ``zoompan``, even though zoompan is what the still
    branch uses. zoompan's ``d`` is output frames per *input* frame, so on a
    multi-frame video input it multiplies the segment: the beat that should
    run 4 seconds emits 4 seconds of output for every frame it is fed (see
    ``zoompan_expr``). ``d=1`` would avoid that, but zoompan then rounds its
    crop origin to whole pixels every frame, and at this zoom rate — eight
    percent over four seconds, well under a pixel per frame — that rounding
    is the whole movement, so the push arrives as a visible step rather than
    a drift. It also re-declares the frame rate, which is exactly the kind of
    thing that broke the timebase before.

    ``scale`` with ``eval=frame`` re-evaluates its size expression per frame
    without touching a timestamp, and the fixed ``crop`` behind it hands the
    chain a constant ``width x height`` frame, so xfade sees the same picture
    geometry it always did. Frame count in equals frame count out, verified
    on a real render: the timeline cannot move.

    The scaled size is forced even, because an odd dimension is not
    representable in yuv420p.
    """
    # `t` is seconds into the beat, so the ramp is wall-clock, not frame
    # index: whatever rate the source arrived at, the push lands on the cut.
    span = max(span, 1.0 / fps)
    zoom = f"(1+{amount:g}*min(t/{span:.3f},1))"
    return (f"scale=w='trunc({width}*{zoom}/2)*2':"
            f"h='trunc({height}*{zoom}/2)*2':eval=frame,"
            f"crop={width}:{height}")


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
                       music_gain_db: float = -18.0,
                       duck: bool = True,
                       grade: bool = True,
                       grain: float = GRAIN_DEFAULT,
                       stickers: list | None = None,
                       sticker_offset: int = 0,
                       sfx: list | None = None,
                       sfx_offset: int = 0
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

    ``grade`` applies the house look (see ``GRADE_CHAIN``) and ``grain`` sets
    its noise strength. Both are applied *per beat*, on the one label every
    beat hands onward, which is what guarantees footage and fallback stills
    get identical treatment: the grade sits downstream of the point where the
    three source branches have already merged, so there is no path into the
    xfade chain that can miss it. An ungraded fallback still would be the one
    shot in the video that stands out, which is the opposite of the point.

    ``stickers`` are emoji pop-ups (see ``engine.assembly.stickers``), each
    of which is one extra input -- a short PNG sequence holding its whole
    animation -- starting at ``sticker_offset``. They are composited after
    the xfade chain, so their times are absolute on the finished timeline,
    and *before* the caption burn, so a caption always draws over a sticker
    rather than under it. ``overlay`` copies its first input's timestamps
    through untouched, so none of this moves the narration invariant.

    ``sfx`` are the sound effects (see ``engine.assembly.audio``), one extra
    input each, starting at ``sfx_offset`` -- which is after the stickers,
    so this new class of input cannot renumber ``audio_offset``,
    ``music_index`` or ``sticker_offset``. ``duck`` keys a sidechain
    compressor on the narration, so the bed drops under speech and rises in
    the pauses; with it off the bed is mixed at a flat gain, which is what
    this renderer did before and which cannot rise in a pause at all.

    Nothing in the audio branch touches a video label, and the mix ends on
    ``duration=first`` -- the narration -- so the picture-equals-narration
    invariant ``segment_lengths`` maintains is untouched by any of it.
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
    # ...and every segment declares its own timebase, rather than inheriting
    # whichever one the last filter on its branch happens to emit. Three
    # different branches feed the xfade chain and they do NOT agree: `fps`
    # (video slots) and `zoompan` (stills) both emit 1/fps, while `concat`
    # re-declares its output as 1/1000000. xfade refuses to configure when
    # its two inputs disagree, so the first real run — which had one-clip
    # beats and multi-clip beats in the same plan — died at graph setup with
    #
    #   [Parsed_xfade_247] First input link main timebase (1/1000000) do not
    #   match the corresponding second input link xfade timebase (1/30)
    #
    # before a frame was written. `settb` rescales timestamps into the new
    # base, so it changes no timing: it only fixes the units they are
    # counted in. It is applied per clip segment *and* after each beat's
    # concat, so every label handed onward is 1/fps whatever produced it.
    timebase = f"settb=1/{fps}"

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

        # --- the beat's finishing chain ----------------------------------
        #
        # Grade and push are applied once, to the whole beat, at the last
        # point before its label goes out. Every visual — a video slot, a
        # still slot inside a multi-clip beat, or a no-clip beat's fallback
        # image — passes through here exactly once, so the three branches
        # cannot diverge in look. It also means the push ramps across the
        # beat rather than restarting on every hard cut inside it.
        #
        # The push is measured against `lengths[beat_index]`, the beat's
        # padded segment, so it finishes on the segment and not somewhere
        # inside it. It reads `t`, not a frame index, and neither filter
        # touches a timestamp, so segment_lengths' arithmetic is untouched.
        look: list[str] = []
        if beat.role in PUSH_ROLES:
            look.append(push_expr(lengths[beat_index], fps, width, height))
        if grade:
            look.append(grade_chain(grain))
        # Re-declared on the way out: `scale` is free to negotiate another
        # pixel format, and xfade wants both of its inputs in the same one.
        finish = ("," + ",".join(look) + ",format=yuv420p") if look else ""

        # A one-clip beat has no concat to hang the finish on, so it goes on
        # that single clip's own chain — same filters, same one application
        # per beat, and the label it emits keeps its old name.
        tail = finish if len(spans) == 1 else ""

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
                    f"fps={fps},format=yuv420p{tail},{timebase}[{label}]")
            else:
                parts.append(
                    f"[{cursor}:v]{common},"
                    f"{zoompan_expr(beat.motion, span, fps, width, height)},"
                    f"format=yuv420p{tail},{timebase}[{label}]")
            clip_labels.append(label)
            cursor += 1

        # Hard cuts inside the beat: a plain concat, no overlap to pay for.
        if len(clip_labels) == 1:
            beat_labels.append(clip_labels[0])
        else:
            joined = "".join(f"[{c}]" for c in clip_labels)
            # concat resets the timebase to 1/1000000 regardless of what its
            # inputs carried, so it is re-declared on the way out.
            parts.append(f"{joined}concat=n={len(clip_labels)}:v=1:a=0"
                         f"{finish},{timebase}[b{beat_index}]")
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

    # --- emoji stickers ---------------------------------------------------
    #
    # After the xfade chain, so a sticker's `enable` window is measured on
    # the finished timeline and lines up with the narration; before the
    # caption burn, so libass draws over the sticker and not under it. The
    # sticker is decoration, the caption is the information, and the
    # placement (see engine.assembly.stickers) keeps them apart anyway.
    #
    # It also leaves `subtitles` as the last filter in the graph, which is
    # what the bare-filename-plus-cwd arrangement depends on.
    if stickers:
        sticker_parts, video_label = stickers_mod.sticker_chain(
            stickers, video_label, sticker_offset, fps=fps, width=width,
            height=height)
        parts.extend(sticker_parts)

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

    # The narration is always the FIRST input to the mix, because the mix
    # ends on `duration=first` and the narration is the timeline.
    mix_labels = ["narr"]

    if music_index is not None:
        if duck:
            # The narration has to reach two places: the mix, and the
            # compressor's key input. `asplit` is how a label is consumed
            # twice -- referencing [narr] from both would be a graph error,
            # not a silent fallback, but the split is also what makes the
            # routing legible.
            parts.append("[narr]asplit=2[narrmix][duckkey]")
            # [duckkey] goes into the compressor exactly as the narration
            # concat emitted it, with no aformat in front of it. Putting
            # one there to tidy up the channel layout measured 2.2 dB off
            # the duck, because upmixing mono to stereo arrives ~3 dB
            # quieter at the detector. See audio.MIX_FORMAT.
            mix_labels = ["narrmix"]
        # Ducked, the loaded bed is an intermediate the compressor reads;
        # undicked it goes straight to the mix, so it takes the name the
        # mix expects and there is no extra filter on the path at all.
        loaded = "bed0" if duck else "bed"
        parts.append(
            f"[{music_index}:a]volume={music_gain_db}dB,"
            f"aloop=loop=-1:size=2e9,atrim=0:{total:.3f},"
            f"{audio_mod.MIX_FORMAT}[{loaded}]")
        if duck:
            # See engine.assembly.audio for where these numbers came from.
            # The bed is what gets compressed; the voice is what drives it.
            parts.append(f"[bed0][duckkey]{audio_mod.duck_filter()}[bed]")
        mix_labels.append("bed")

    if sfx:
        sfx_parts, sfx_labels = audio_mod.sfx_chain(sfx, sfx_offset)
        parts.extend(sfx_parts)
        mix_labels.extend(sfx_labels)

    if len(mix_labels) == 1:
        parts.append("[narr]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        joined = "".join(f"[{label}]" for label in mix_labels)
        # `normalize=0` is not a detail. amix's default divides the sum by
        # the number of inputs that are still running, so the moment a
        # half-second whoosh ends every other track steps up -- an audible
        # jump, on every sound effect, that no filtergraph string assertion
        # would ever show. Off, the tracks are summed at the levels the
        # gains already set, and `loudnorm` on the end is what brings the
        # result back to -14 LUFS with a true peak under -1.5 dB.
        parts.append(f"{joined}amix=inputs={len(mix_labels)}:duration=first:"
                     f"dropout_transition=0:normalize=0,"
                     f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

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
    music_gain = -18.0
    if music_path and Path(music_path).exists():
        music_index = len(inputs) + len(beats)
        command += ["-stream_loop", "-1", "-i",
                    str(Path(music_path).resolve())]
        # Measured off the file itself, so a track dropped into
        # assets/music/ at any mastering level lands in the same place in
        # the mix without a knob being touched. Falls back to the flat
        # -18dB this renderer used before if it cannot be read.
        music_gain = audio_mod.music_gain_db(music_path, settings)

    # Sticker inputs go LAST, after the music. `audio_offset` and
    # `music_index` are positional into this argv, and appending here is
    # what keeps both of them the numbers they already were.
    prepared = stickers_mod.prepare(plan, settings)
    sticker_offset = (len(inputs) + len(beats)
                      + (1 if music_index is not None else 0))
    if prepared:
        command += stickers_mod.sticker_inputs(prepared, settings.fps)

    # ...and the sound effects go after the stickers, for exactly the same
    # reason the stickers went after the music: every index above is a
    # position in this argv computed from the counts before it, so the only
    # place a new class of input can be added without renumbering a
    # narration stream is the end. Get this wrong and beat k's voice comes
    # out of beat k+1's slot -- silently, in a file nobody plays.
    sfx_cues = audio_mod.plan_sfx(plan, settings, stickers=prepared)
    sfx_offset = sticker_offset + len(prepared)
    if sfx_cues:
        command += audio_mod.sfx_inputs(sfx_cues)

    ass_name = Path(ass_path).name if ass_path else None
    run_cwd = str(Path(ass_path).parent) if ass_path else None

    graph, total, video_label = build_filter_graph(
        plan, fps=settings.fps, width=settings.width, height=settings.height,
        transition_duration=settings.transition_duration, ass_path=ass_name,
        audio_offset=len(inputs), music_index=music_index,
        music_gain_db=music_gain,
        duck=bool(getattr(settings, "music_duck", True)),
        grade=settings.video_grade, grain=settings.video_grain,
        stickers=prepared, sticker_offset=sticker_offset,
        sfx=sfx_cues, sfx_offset=sfx_offset)

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
    # A beat needs some visual to render, but which kind no longer matters:
    # clips are the normal case now and image_path is the fallback (see
    # plan_inputs). Rejecting only a beat with neither catches an
    # unrendered plan early, same as before, without also rejecting every
    # clips-only plan that the clip stage was built to produce.
    missing = [b.beat_id for b in beats if not b.image_path and not b.clips]
    if missing:
        raise ValueError(f"beats without a clip or an image: {missing}")
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

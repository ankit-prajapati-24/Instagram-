"""Hindi voice.

Two engines live behind one entry point. Piper is the default, chosen by ear
after a side-by-side of twelve edge-tts voices, three Piper voices and several
tuning passes; edge-tts is the fallback when Piper cannot run.

Neither goes through OmniRoute. The gateway's /v1/audio/speech documents only
``openai/tts-1``, which speaks Hindi with a foreign accent, and on this
install it answers "No credentials for provider: openai" anyway.

Timing, and why captions do not use the engine's own word timings:

  * edge-tts 7.2.8 emits **only** ``SentenceBoundary`` events — verified
    against hi-IN-MadhurNeural, hi-IN-SwaraNeural and en-US-GuyNeural, none
    of which produced a single ``WordBoundary``.
  * Piper reports no timings at all.

Either way the narration is Devanagari while the burned caption is Roman
Hinglish, so their word counts do not line up and the spoken timings could
not be reused for captions regardless. Each beat is its own audio file, so
its span is measured exactly and caption words are interpolated inside it by
character length. For 3-5 second beats that is visually indistinguishable
from true per-word timing, and it stays deterministic.

That interpolation is still the default, and still the fallback. With
``RAHASYA_ALIGN=1`` the timings are instead measured back off the synthesised
audio with faster-whisper — see ``engine/media/align.py``, which owns both the
model and the policy for what to do when its token count disagrees with ours.
Any failure there returns interpolated timings rather than raising, and the
source of every beat's timings is recorded on ``Beat.word_timing_source``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from engine.contract import ReelPlan, WordTiming
from engine.media.align import INTERPOLATED, WHISPER, build_aligner

TICKS_PER_SECOND = 10_000_000  # edge-tts reports 100-nanosecond ticks


def distribute_words(text: str, start: float, end: float) -> list[WordTiming]:
    """Spread a sentence's words across its measured span.

    Weighted by character count, so "Bhumadhya" holds the screen longer than
    "ye" — which tracks how long each actually takes to say far better than an
    even split would.
    """
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words or end <= start:
        return []

    weights = [max(len(w), 1) for w in words]
    total = sum(weights)
    span = end - start

    timings: list[WordTiming] = []
    cursor = start
    for word, weight in zip(words, weights):
        width = span * (weight / total)
        timings.append(WordTiming(word=word, start=cursor,
                                  end=cursor + width))
        cursor += width
    return timings


def offsets_to_timings(chunks: list[dict]) -> list[WordTiming]:
    """Turn edge-tts boundary events into word timings.

    Prefers real ``WordBoundary`` events; falls back to interpolating inside
    each ``SentenceBoundary``, which is all edge-tts 7.2.8 actually emits.
    """
    words = [c for c in chunks if c.get("type") == "WordBoundary"]
    if words:
        return [
            WordTiming(
                word=c.get("text", ""),
                start=c.get("offset", 0) / TICKS_PER_SECOND,
                end=(c.get("offset", 0) + c.get("duration", 0))
                / TICKS_PER_SECOND,
            )
            for c in words
        ]

    timings: list[WordTiming] = []
    for chunk in chunks:
        if chunk.get("type") != "SentenceBoundary":
            continue
        start = chunk.get("offset", 0) / TICKS_PER_SECOND
        end = (chunk.get("offset", 0)
               + chunk.get("duration", 0)) / TICKS_PER_SECOND
        timings.extend(distribute_words(chunk.get("text", ""), start, end))
    return timings


async def synth_beat(text: str, out_path: str | Path, *,
                     voice: str = "hi-IN-MadhurNeural",
                     rate: str = "-8%",
                     pitch: str = "-6Hz") -> list[WordTiming]:
    """Synthesise one beat and return timings for the spoken text."""
    import edge_tts

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    chunks: list[dict] = []
    with out_path.open("wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            else:
                chunks.append(chunk)
    return offsets_to_timings(chunks)


def caption_timings(caption_text: str, duration: float) -> list[WordTiming]:
    """Timings for the words we actually burn on screen.

    The voice track is Devanagari and the caption is Roman Hinglish, so their
    word counts do not line up and the spoken timings cannot be reused
    directly. Each beat is its own audio file, so its span is known exactly —
    only the positions inside it need interpolating.
    """
    return distribute_words(caption_text, 0.0, duration)


def probe_duration(path: str | Path, ffmpeg: str) -> float:
    """Seconds of audio, read back from the file we just wrote.

    Measured duration is the single source of truth for beat length; the
    model's target_seconds only ever steers script length.
    """
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not match:
        raise RuntimeError(f"could not read duration of {path}: "
                           f"{result.stderr[-300:]}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --- narration a human supplied -------------------------------------------
#
# Piper writes 24000 Hz mono mp3, and ``stitch_narration`` glues the beats
# together with ffmpeg's concat demuxer, which requires every input to agree
# on codec, rate and channel count. A 44.1 kHz stereo upload dropped in
# beside nine Piper beats is therefore not a cosmetic mismatch — it either
# fails the concat or plays at the wrong speed. Every upload is rewritten
# into this exact shape before anything else looks at it.
NARRATION_SAMPLE_RATE = 24000
NARRATION_CHANNELS = 1

# Measured off Piper's own output on 2026-09-23: three beats of a real plan
# came in at -18.2, -15.9 and -16.3 LUFS. -16 is the middle of the band it
# already occupies, so an uploaded beat lands inside Piper's own spread
# rather than beside it.
#
# This has to happen per beat. The render's own ``loudnorm`` runs on the
# concatenated narration, by which point one quiet beat and nine loud ones
# are a single stream and the difference between them is baked in.
UPLOAD_LOUDNESS = -16.0
UPLOAD_TRUE_PEAK = -2.0
UPLOAD_LOUDNESS_RANGE = 11.0

# The engine name written onto a beat whose audio a human supplied. Sits
# alongside "piper" and "edge" for the reason those are written down at all:
# a run that used something other than the default must never be
# indistinguishable from one that did not.
UPLOAD_ENGINE = "upload"

# Below this a file is not a beat of narration. A 0.2s blip decodes fine,
# passes every signature check, and would silently shrink the beat it
# replaced — taking its clip slots and its caption words down with it.
MIN_UPLOAD_SECONDS = 0.5

# loudnorm reports -inf for digital silence and something near it for a file
# holding nothing but noise floor. Either way there is no speech to
# normalise, and the second pass would apply enormous gain to hiss.
SILENCE_LUFS = -60.0


class UploadRejected(Exception):
    """An uploaded narration file this pipeline will not accept.

    Separate from ``RuntimeError`` so the HTTP layer can turn it into a 4xx
    the user can act on rather than a 500 that reads like a bug in the
    engine. Every message is written to be shown to whoever picked the file.
    """


@dataclass
class CleanupReport:
    """What the cleanup chain did to one upload.

    Nothing about an upload changes silently -- this is the record of it,
    the same rule ``voice_engine``, ``Clip.provider`` and
    ``word_timing_source`` already follow for their own transforms. A plain
    dataclass, not a pydantic model: it never crosses the contract/store
    boundary, only lives inside one ingest call.
    """

    seconds_before: float
    seconds_after: float
    loudness_before: float
    loudness_after: float
    filters_applied: str
    # ``filters_applied`` alone cannot distinguish "cleanup was off" from
    # "cleanup ran, emptied the file, and was abandoned for this write" --
    # both would otherwise report the same values. This tells them apart:
    # False for both "off" and "ran normally"; True only for the fallback
    # in ``ingest_narration``, and then ``filters_applied`` still names the
    # fragment that was attempted, not written over with "".
    cleanup_abandoned: bool = False


# How much silence a trimmed edge keeps. Cut to nothing, one sentence
# starts on the syllable the previous one ended on; the seam wants to be
# tight, not absent.
#
# silenceremove keeps exactly this much: asked for 0.04s it leaves a
# 0.04s region, measured. mp3 encoder padding then adds a further ~0.06s
# that silencedetect does not flag, so the audible seam is about 0.10s.
#
# That is the floor the pause-cap comment calls a natural inter-word
# gap, and it is the relationship worth keeping: the gap between two
# sentences ends up shorter than a pause inside one (measured at
# 0.22-0.37s), so a reel reads as continuous speech rather than as a
# list of lines.
EDGE_SILENCE_KEPT = 0.04


def edge_trim_filter(settings) -> str:
    """Trim the silence a synthesised beat opens and closes on.

    Beats are concatenated, so a tail and the next head meet at every
    seam. Measured across a real ten-beat reel, every beat ended on
    0.20-0.31s of nothing and six opened on 0.05-0.09s, which put about a
    third of a second of silence between every pair of sentences.

    Only the edges. The pauses *inside* a beat are speech rhythm, and
    ``voice_pause_cap``'s own comment exists to protect them -- it calls
    0.1-0.3s a natural inter-word gap that must survive. The measured
    mid-beat gaps were 0.05-0.37s, squarely that range, so a cap low
    enough to shorten them would be cutting rhythm and calling it dead
    air.

    Deliberately *not* ``cleanup_filters``. That chain highpasses,
    denoises and de-clicks because a phone recording has room noise and
    mouth bumps; synthesised speech has neither. Measured on one Piper
    beat, running the whole chain over it cost 0.5 LU (-15.8 to -16.3
    LUFS) and trimmed no more than the trim alone: what the denoiser took
    out of clean TTS was signal.

    The trailing edge is done by reversing, trimming the new leading
    edge, and reversing back. ``silenceremove``'s stop_periods would
    reach every pause in the file, including the ones this must not
    touch.
    """
    if not settings.voice_trim_edges:
        return ""
    threshold = f"{settings.voice_silence_threshold}dB"
    one_edge = (f"silenceremove=start_periods=1"
                f":start_silence={EDGE_SILENCE_KEPT}"
                f":start_threshold={threshold}"
                f":detection=peak")
    return f"{one_edge},areverse,{one_edge},areverse"


def cleanup_filters(settings) -> str:
    """The cleanup filter fragment for one upload, or ``""`` when cleanup is
    off.

    Comma-joined ahead of ``loudnorm`` by the caller (Task 2); returns a
    fragment with no leading or trailing comma so it can be joined
    unconditionally, and returns ``""`` rather than ``None`` for the same
    reason.

    Order: a highpass to clear room rumble under any voiced fundamental,
    ``afftdn`` to pull down a steady noise floor, ``adeclick`` for mouth
    clicks and mic bumps, then ``silenceremove`` to cap how long any pause
    survives.

    ``silenceremove``'s parameters were verified against ffmpeg on
    2026-09-23, not assumed, per this task's brief:

      * ``start_silence``/``stop_silence`` are the *maximum duration of
        silence kept after trimming*, not a trigger duration. Measured: a
        2.0s internal gap between two tones, with ``pause_cap=0.35``, came
        out to ~0.37s kept (``silencedetect`` on the result) and the file's
        total duration dropped from 4.00s to 2.37s -- almost exactly
        ``4.0 - (2.0 - 0.35)``. A run of 0.2s natural gaps (already under
        the cap) passed through a 3.40s file untouched, still 3.40s.
        Leading/trailing silence is capped the same way: 1s of silence on
        each side of a 2s tone came out to 0.35s on each side, 2.70s total.
      * ``start_threshold``/``stop_threshold`` need an explicit ``dB``
        suffix. The brief's template writes the bare number; passed as a
        bare negative value ffmpeg rejects it outright ("Value -45.000000
        ... out of range [0 - 1.79769e+308]") because unsuffixed the
        parameter is an amplitude ratio, which cannot be negative. The
        fragment below appends ``dB`` to make the configured threshold a
        dB value, which is what every other number in this task assumes it
        is.
    """
    if not settings.voice_clean:
        return ""
    pause_cap = settings.voice_pause_cap
    threshold_db = f"{settings.voice_silence_threshold}dB"
    return (
        f"highpass=f={settings.voice_clean_highpass},"
        f"afftdn=nf={settings.voice_clean_denoise},"
        f"adeclick,"
        f"silenceremove=start_periods=1"
        f":start_silence={pause_cap}"
        f":start_threshold={threshold_db}"
        f":stop_periods=-1"
        f":stop_silence={pause_cap}"
        f":stop_threshold={threshold_db}"
        f":detection=peak"
    )


def _loudnorm_measurement(path: Path, settings, filters: str = "") -> dict:
    """Run loudnorm's analysis pass and return what it measured.

    Two passes rather than one because single-pass loudnorm is a *dynamic*
    normaliser: it rides the gain as it goes, which over three seconds of
    speech is audible as pumping. Measuring first and then applying one
    linear gain preserves the delivery, which is the entire reason somebody
    recorded their own narration instead of using Piper's.

    ``filters``, when given, is a cleanup fragment (see ``cleanup_filters``)
    prepended ahead of ``loudnorm`` -- Global Constraint 2's reason: the
    measured values have to describe the signal that ends up written, not
    the noisy one that came off the microphone. Left empty (the default),
    this is the same raw measurement ``ingest_narration`` always took.
    """
    af = f"{filters}," if filters else ""
    af += (f"loudnorm=I={UPLOAD_LOUDNESS}:TP={UPLOAD_TRUE_PEAK}"
           f":LRA={UPLOAD_LOUDNESS_RANGE}:print_format=json")
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", af, "-f", "null", "-"],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise UploadRejected(
            "ffmpeg could not read any audio out of that file. The "
            "extension and the content type are not trusted here, only "
            "what the bytes decode to.")
    blobs = re.findall(r"\{[^{}]+\}", result.stderr)
    if not blobs:
        raise UploadRejected(
            "ffmpeg decoded that file but reported no loudness for it, "
            "which means it carries no audio stream.")
    try:
        return json.loads(blobs[-1])
    except ValueError as exc:                      # pragma: no cover
        raise UploadRejected(
            f"could not read loudness back from ffmpeg: {exc}") from exc


def _input_i(measured: dict) -> float:
    """``input_i`` off a loudnorm measurement, or ``-inf`` if it can't be
    read -- the same fallback the silence check has always used, now
    shared by both the raw check and the post-write probe."""
    try:
        return float(measured["input_i"])
    except (KeyError, TypeError, ValueError):
        return float("-inf")


def ingest_narration(source: str | Path, target: str | Path, settings, *,
                     raw_target: Path | None = None,
                     clean: bool | None = None) -> tuple[float, CleanupReport]:
    """Accept one beat of human-supplied narration.

    Returns ``(seconds, CleanupReport)``. ``seconds`` keeps its original
    meaning — the length of the file actually written — and the report is
    the record Global Constraint 4 asks for: nothing an upload goes through
    here happens silently, the same rule ``voice_engine``, ``Clip.provider``
    and ``word_timing_source`` already follow.

    Does four jobs in one place because they are one question asked four
    ways — is this really usable narration:

      * it decodes at all, which a signature check cannot establish;
      * it says something, rather than being silence or a blip;
      * cleanup, if it is on, clears room noise, clicks and overlong
        pauses without touching what was actually said;
      * it comes out in Piper's format, at Piper's loudness.

    Refusals are ``UploadRejected`` carrying a sentence aimed at whoever
    picked the file. Nothing reaches ``target`` unless every check passes:
    the transcode lands on a temporary path and is moved into place last,
    so a rejected upload cannot leave a broken beat behind.

    Too-short, silent and does-not-decode are all judged on ``source``
    itself, before cleanup runs. Judging them on the cleaned signal instead
    breaks in both directions: a recording that is nothing but noise would
    have the denoiser quiet it below the silence floor and be accepted, and
    a real recording could have ``silenceremove`` eat its only content and
    then be refused as silent for a defect cleanup itself introduced.

    ``raw_target``, when given, receives a byte-identical copy of
    ``source`` once it has passed those checks — not transcoded, not
    cleaned, since that copy is what a later revert re-ingests and has to
    be the exact bytes handed over. ``raw_target=None`` (the default) keeps
    no copy, unchanged from before this parameter existed.

    ``clean`` overrides ``settings.voice_clean`` for this one call; ``None``
    (the default) uses the setting.
    """
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        seconds_before = probe_duration(source, settings.ffmpeg)
    except RuntimeError as exc:
        raise UploadRejected(
            "ffmpeg could not read a duration out of that file, so it is "
            "not audio this pipeline can use.") from exc
    if seconds_before < MIN_UPLOAD_SECONDS:
        raise UploadRejected(
            f"that file is {seconds_before:.2f}s, too short to be a beat "
            f"of narration (the minimum is {MIN_UPLOAD_SECONDS:g}s). A "
            f"beat's length sets its clip slots and its caption timings, "
            f"so a stray blip would take those down with it.")

    # Raw, unfiltered measurement -- this is what the too-short check above
    # already used implicitly (it is the same file) and what the silent
    # check below uses explicitly. See the docstring for why this must not
    # be the cleaned signal.
    raw_measured = _loudnorm_measurement(source, settings)
    if not _input_i(raw_measured) > SILENCE_LUFS:
        raise UploadRejected(
            f"that file is silent (measured {raw_measured.get('input_i')} "
            f"LUFS). It would render as a gap the length of the beat, with "
            f"the captions scrolling over nothing.")

    raw_partial: Path | None = None
    if raw_target is not None:
        raw_target = Path(raw_target)
        raw_target.parent.mkdir(parents=True, exist_ok=True)
        # Copied to a scratch path now, moved onto ``raw_target`` only once
        # the write pass below has actually succeeded (final review,
        # Important 1). ``raw_target`` is a deterministic per-beat path: a
        # second upload that clears every refusal above but then fails the
        # measure or write pass further down must not already have
        # clobbered the byte-identical raw copy the beat's *previous*
        # upload left there. Same discipline the transcode itself uses a
        # few lines below.
        raw_partial = raw_target.with_suffix(raw_target.suffix + ".part")
        shutil.copyfile(source, raw_partial)

    effective = (settings if clean is None
                else replace(settings, voice_clean=clean))
    requested_filters = cleanup_filters(effective)

    # The resample/downmix fragment every write goes through now, on
    # *every* call -- cleanup on or off (final review, Important 2).
    # Review round 1 (Critical 1, below) found that folding it into the
    # write pass only, and leaving the cleanup-off path to reach the same
    # format with the plain output-side ``-ar``/``-ac`` flags instead,
    # computes loudnorm's gain for a different signal than the one that
    # downmix actually produces: a decorrelated stereo upload measured 3+
    # dB off target with cleanup off. Global Constraint 8 ("cleanup off
    # behaves exactly as today") is about not regressing the off path, not
    # about preserving a mismatch that predates this feature, so the off
    # path now goes through the identical ``aresample``/``aformat``
    # fragment, ahead of ``loudnorm``, that the on path already needed.
    format_pipeline = (f"aresample={NARRATION_SAMPLE_RATE},"
                       f"aformat=channel_layouts=mono")
    pipeline = (f"{requested_filters},{format_pipeline}" if requested_filters
               else format_pipeline)
    # Verified against ffmpeg on 2026-09-23, and re-verified against this
    # repository's own ``settings.ffmpeg`` while fixing review round 1:
    # chaining ``silenceremove`` into ``loudnorm`` on a file with no
    # internal gap for ``silenceremove`` to actually trim (a plain
    # continuous tone, reproduced on both mono and stereo), while ffmpeg
    # also resamples/downmixes the *output* (the plain ``-ar``/``-ac``
    # flags below), crashes with "Assertion best_input >= 0 failed" in
    # ffmpeg_filter.c. A file with an actual gap for silenceremove to trim
    # does not trigger it. Review round 1's own repro covered five
    # fixtures, two of them gap-free continuous tones, and still came back
    # all green; re-running the exact same five fixtures here reproduced
    # the crash on both gap-free ones. So the crash is real and was
    # independently reproduced twice -- round 1's own repro simply did not
    # catch it, not because its fixtures were all gapped. Doing the
    # resample and channel mix explicitly, inside the filter graph and
    # ahead of ``loudnorm``, avoids it -- and doing it identically in
    # *both* passes, not just the write, is what review round 1 also
    # asked for: measuring a cleaned-but-still-stereo signal and then
    # downmixing only on write measured 3+ dB off target on a genuinely
    # decorrelated (hard-panned) two-channel recording, because loudnorm's
    # gain was computed for a different signal than the one it was applied
    # to.

    measured = _loudnorm_measurement(source, settings, pipeline)

    cleanup_abandoned = False
    if requested_filters and not math.isfinite(_input_i(measured)):
        # Cleanup emptied the file outright: silenceremove trimmed every
        # sample once the denoiser had already pulled the whole thing below
        # its threshold, which is exactly what a recording that is nothing
        # but noise does. That upload already earned acceptance from the
        # raw silence check above; loudnorm's ``measured_I`` cannot take a
        # literal ``-inf`` (ffmpeg: "Value -inf for parameter 'measured_I'
        # out of range"), and there is nothing left to encode besides. So
        # cleanup is skipped for this one write and the raw signal --
        # resampled/downmixed the same way the off path always is now -- is
        # normalised instead -- an honest fallback, not a silent one:
        # ``cleanup_abandoned`` on the report says so, and
        # ``filters_applied`` still names what was attempted rather than
        # being overwritten with "", which would read identically to
        # cleanup never having been requested at all.
        cleanup_abandoned = True
        pipeline = format_pipeline
        measured = _loudnorm_measurement(source, settings, pipeline)

    # Linear mode with the measured values: one gain for the whole beat and
    # no riding. ffmpeg drops back to dynamic by itself if the requested
    # gain would clip, which is the right trade and not worth refusing.
    applied = (f"loudnorm=I={UPLOAD_LOUDNESS}:TP={UPLOAD_TRUE_PEAK}"
               f":LRA={UPLOAD_LOUDNESS_RANGE}"
               f":measured_I={measured['input_i']}"
               f":measured_TP={measured['input_tp']}"
               f":measured_LRA={measured['input_lra']}"
               f":measured_thresh={measured['input_thresh']}"
               f":offset={measured.get('target_offset', 0)}"
               f":linear=true")
    write_filters = f"{pipeline},{applied}"

    partial = target.with_suffix(target.suffix + ".part")
    try:
        result = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-i", str(source), "-af", write_filters,
             "-ar", str(NARRATION_SAMPLE_RATE),
             "-ac", str(NARRATION_CHANNELS),
             "-c:a", "libmp3lame", "-q:a", "2",
             # Named, not inferred: the output lands on a ``.part`` path so
             # a rejected upload cannot leave a broken beat behind, and
             # ffmpeg cannot guess a muxer from that extension.
             "-f", "mp3",
             # Video is dropped rather than refused: somebody handing over
             # the .mp4 their phone recorded meant the sound in it.
             "-vn", str(partial)],
            capture_output=True, text=True)
        if result.returncode != 0 or not partial.is_file():
            raise UploadRejected(
                f"ffmpeg could not convert that file into narration: "
                f"{result.stderr.strip()[-200:]}")
        # Read the length back off what was actually written rather than
        # off the source: the written file is what the timeline measures.
        written = probe_duration(partial, settings.ffmpeg)
        os.replace(partial, target)
        # Only now, with the cleaned file itself safely in place, does the
        # raw copy replace whatever this beat's raw currently is (final
        # review, Important 1): a write-pass failure never reaches this
        # line, so the beat's previously stored raw survives a refused
        # re-upload byte for byte.
        if raw_partial is not None:
            os.replace(raw_partial, raw_target)
    finally:
        Path(partial).unlink(missing_ok=True)
        if raw_partial is not None:
            raw_partial.unlink(missing_ok=True)

    # Read-only probe off the file that was actually written. Not a third
    # transform pass -- nothing here feeds back into what was encoded --
    # just the number Global Constraint 4 requires for the report.
    final_measured = _loudnorm_measurement(target, settings)

    report = CleanupReport(
        seconds_before=seconds_before,
        seconds_after=written,
        # The raw signal's own measurement, not whichever pipeline ended up
        # driving the write (final review, Minor 3): ``measured`` is the
        # cleaned-and-resampled signal when cleanup ran, so using it here
        # named two different signals "before" depending on state, and the
        # same beat toggled between cleaned and raw reported two numbers
        # that were not comparable. ``raw_measured`` means the same thing
        # in every state: what the upload measured before this call did
        # anything to it.
        loudness_before=_input_i(raw_measured),
        loudness_after=_input_i(final_measured),
        # The fragment requested, not ``pipeline``: "" when cleanup was
        # off, the cleanup chain when it was requested -- whether it ran
        # (``cleanup_abandoned`` False) or was abandoned (True). Never
        # silently collapsed back to "" for the abandoned case, which
        # would be indistinguishable from cleanup never having been asked
        # for.
        filters_applied=requested_filters,
        cleanup_abandoned=cleanup_abandoned,
    )
    return written, report


def apply_beat_audio(beat, path: str | Path, settings, *,
                     engine: str = UPLOAD_ENGINE, aligner=None) -> float:
    """Point a beat at different audio and redo everything derived from it.

    A beat's span is its audio's measured length, and its caption words are
    positions inside that span — so swapping the audio without redoing both
    leaves the captions timed to a recording that no longer exists. Kept
    beside ``synth_plan`` and doing it the same way, because two places
    deriving a beat's timings differently is how they drift.
    """
    path = Path(path)
    beat.audio_path = str(path)
    beat.voice_engine = engine
    beat.measured_seconds = probe_duration(path, settings.ffmpeg)
    if aligner is None:
        beat.words = caption_timings(beat.caption_text,
                                     beat.measured_seconds)
        beat.word_timing_source = INTERPOLATED
    else:
        result = aligner.align(path, beat.caption_text,
                               beat.measured_seconds)
        beat.words = result.words
        beat.word_timing_source = result.source
    # Whatever the previous engine reported about its own speech does not
    # describe this file.
    beat.spoken_words = None
    return beat.measured_seconds


def synth_beat_edge(beat_text: str, target: Path, settings) -> int:
    """edge-tts path. Returns how many boundary spans came back."""
    spans = asyncio.run(synth_beat(
        beat_text, target, voice=settings.voice,
        rate=settings.voice_rate, pitch=settings.voice_pitch))
    return len(spans)


def synth_beat_piper(beat_text: str, target: Path, settings) -> int:
    """Piper path. Piper reports no timings at all, hence 0."""
    from engine.media import piper_voice

    piper_voice.synth(beat_text, target, settings)
    return 0


def _trim_beat_edges(target: Path, settings) -> bool:
    """Cut the silence a just-spoken beat opens and closes on, in place.

    A polish pass, not a stage: the audio is already correct when this
    runs, so a trim that fails leaves the beat exactly as synthesis
    wrote it rather than failing the voice stage. An untrimmed beat is a
    slightly loose reel; no beat is no reel.

    Written beside the target and renamed, because ffmpeg cannot read
    and write the same path, and a half-written beat is one the render
    would concatenate without complaint.
    """
    filters = edge_trim_filter(settings)
    if not filters:
        return False
    trimmed = target.with_suffix(target.suffix + ".trim.mp3")
    try:
        result = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-i", str(target), "-af", filters,
             "-ar", str(NARRATION_SAMPLE_RATE),
             "-ac", str(NARRATION_CHANNELS),
             "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(trimmed)],
            capture_output=True, text=True)
        if result.returncode != 0 or not trimmed.is_file()                 or trimmed.stat().st_size == 0:
            print(f"[voice] edge trim skipped for {target.name}: "
                  f"{(result.stderr or '').strip()[-160:]}",
                  file=sys.stderr, flush=True)
            return False
        os.replace(trimmed, target)
        return True
    except OSError as exc:
        print(f"[voice] edge trim skipped for {target.name}: {exc}",
              file=sys.stderr, flush=True)
        return False
    finally:
        trimmed.unlink(missing_ok=True)


# One ``atempo`` covers 0.5x to 2.0x. Beyond that ffmpeg wants them
# chained, and a narration asked to run at half or double speed is a
# different decision from "this reads a little slow" -- so the pass
# declines instead of guessing.
SPEED_MIN = 0.5
SPEED_MAX = 2.0


# The Hindi voices Piper publishes, and whether each is a woman's voice.
#
# Listed rather than discovered because `models/` is gitignored: a fresh
# clone has no .onnx at all, and offering only what is on disk showed an
# empty control on a machine where Piper works perfectly --
# `piper_voice.ensure_model` downloads the model on first use. Anything
# actually on disk is added to this on top, so a voice someone drops in by
# hand still appears whatever it is called.
#
# The gender is a label, so nobody has to synthesise three voices to find
# out which is which. Nothing depends on it.
PIPER_GENDERS = {"pratham": "Male", "rohan": "Male", "priyamvada": "Female"}
PIPER_FETCHABLE = ("pratham", "priyamvada", "rohan")

# The locales worth offering for a Hindi channel. edge-tts carries several
# hundred voices and a list that long is not a choice, it is a search.
EDGE_LOCALES = ("hi-IN", "en-IN")

_VOICE_ID = re.compile(r"^(piper|edge):([A-Za-z0-9][A-Za-z0-9_.-]*)$")


def split_voice_id(voice_id: str) -> tuple[str, str]:
    """``("edge", "hi-IN-SwaraNeural")`` from ``"edge:hi-IN-SwaraNeural"``.

    A Piper voice becomes a filename, so this is the gate that keeps a
    chosen id from reaching the filesystem as anything but a name.
    """
    match = _VOICE_ID.fullmatch(voice_id or "")
    if not match:
        raise ValueError(f"not a voice id: {voice_id!r}")
    return match.group(1), match.group(2)


def _edge_hindi_voices() -> list[dict]:
    """edge-tts's own catalogue, narrowed to the locales worth offering.

    Split out so the test for "one library being unreachable" can replace
    it without a network stub.
    """
    import asyncio

    import edge_tts

    found = asyncio.run(edge_tts.list_voices())
    return [v for v in found
            if str(v.get("Locale", "")).startswith(EDGE_LOCALES)]


def available_voices(settings, *, include_edge: bool = True) -> list[dict]:
    """Every voice this install can actually speak with.

    Discovered, not listed: a model dropped into the models directory shows
    up without a code change, and a hardcoded list is one that goes stale
    the first time someone adds a voice.

    edge-tts is reported but marked ``local: False``, because it goes to
    Microsoft for every beat -- that is the difference that matters when
    the network is down mid-render. If its catalogue cannot be reached the
    local voices are still returned: losing the network should cost the
    edge voices, not the whole control.
    """
    voices: list[dict] = []
    models = Path(getattr(settings, "piper_models_dir", "") or ".")
    try:
        on_disk = {p.stem for p in models.glob("*.onnx")}
    except OSError:
        on_disk = set()
    # Published first, in their published order, then anything else found
    # on disk. `local` says whether it is here yet; a False one costs a
    # download the first time it speaks, and nothing after that.
    for name in list(PIPER_FETCHABLE) + sorted(on_disk - set(PIPER_FETCHABLE)):
        voices.append({
            "id": f"piper:{name}",
            "engine": "piper",
            "voice": name,
            "label": name,
            "gender": PIPER_GENDERS.get(name, ""),
            "local": name in on_disk,
        })

    if include_edge:
        try:
            for entry in _edge_hindi_voices():
                short = str(entry.get("ShortName", ""))
                if not short:
                    continue
                voices.append({
                    "id": f"edge:{short}",
                    "engine": "edge",
                    "voice": short,
                    # "hi-IN-SwaraNeural" reads as a product code; the name
                    # in the middle is what a person recognises.
                    "label": short.split("-")[-1].replace("Neural", ""),
                    "gender": str(entry.get("Gender", "")),
                    "local": False,
                })
        except Exception as exc:                      # noqa: BLE001
            print(f"[voice] edge-tts voices unavailable: {exc}",
                  file=sys.stderr, flush=True)
    return voices


def spoken_master_path(target: str | Path) -> Path:
    """Where a beat's audio is kept as it was spoken, before any speed.

    By convention rather than by a contract field: it sits beside the beat
    and is derived from its name, so nothing has to be stored, migrated or
    kept in step for a file that only this module and the respeed route
    ever open.
    """
    target = Path(target)
    return target.with_suffix(target.suffix + ".spoken")


def respeed_beat(target: str | Path, settings) -> bool:
    """Redo a beat's speed from the spoken original. True when it did.

    Always from the original, never from the current file. Speeding an
    already-sped beat compounds -- 1.2 twice is 1.44 -- so the factor the
    person asked for would not be the factor they heard, and every step
    back towards the spoken pace would be impossible.

    Returns False when there is no original to work from, which is the
    honest answer for a beat spoken before this existed: its audio is the
    only copy and re-speeding it would compound.
    """
    target = Path(target)
    master = spoken_master_path(target)
    if not master.is_file():
        return False
    try:
        shutil.copyfile(master, target)
    except OSError as exc:
        print(f"[voice] respeed skipped for {target.name}: {exc}",
              file=sys.stderr, flush=True)
        return False
    # At 1.0 the copy above already restored the spoken pace, and
    # `speed_beat` correctly declines to re-encode for nothing.
    speed_beat(target, settings)
    return True


def speed_beat(target: Path, settings) -> bool:
    """Play a just-spoken beat faster, in place. True when it did.

    Placed between synthesis and ``probe_duration`` on purpose. Everything
    downstream is measured off this file -- ``measured_seconds``, the
    caption word timings, the clip count, the times the stickers fire --
    so speeding it here means all of them describe the audio that will
    actually play. Speeding it any later would leave every one of those
    numbers pointing at a beat that no longer runs that long.

    ``atempo`` stretches time and leaves pitch alone. Changing the sample
    rate instead would shorten the beat by the same factor and raise the
    voice with it, which is the chipmunk this is careful not to be.

    A polish pass, like ``_trim_beat_edges`` beside it: the audio is
    already correct when this runs, so a failure leaves the beat exactly
    as synthesis wrote it. A beat that did not speed up is a slightly slow
    reel; a beat that failed to write is no reel.
    """
    try:
        speed = float(getattr(settings, "voice_speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        return False
    if speed == 1.0:
        # Nothing to do, and re-encoding an mp3 for nothing spends a
        # generation of quality on no change at all.
        return False
    if not SPEED_MIN <= speed <= SPEED_MAX:
        print(f"[voice] speed {speed} is outside {SPEED_MIN}-{SPEED_MAX}; "
              f"leaving {target.name} at its spoken pace",
              file=sys.stderr, flush=True)
        return False

    # Written beside the target and renamed, because ffmpeg cannot read and
    # write the same path, and a half-written beat is one the render would
    # concatenate without complaint.
    # Kept before anything changes, so a later speed change starts from
    # what was said rather than from what was last played.
    master = spoken_master_path(target)
    try:
        if not master.is_file():
            shutil.copyfile(target, master)
    except OSError as exc:
        print(f"[voice] could not keep the spoken original for "
              f"{target.name}: {exc}", file=sys.stderr, flush=True)

    faster = target.with_suffix(target.suffix + ".speed.mp3")
    try:
        result = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-i", str(target), "-af", f"atempo={speed:g}",
             "-ar", str(NARRATION_SAMPLE_RATE),
             "-ac", str(NARRATION_CHANNELS),
             "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(faster)],
            capture_output=True, text=True)
        if (result.returncode != 0 or not faster.is_file()
                or faster.stat().st_size == 0):
            print(f"[voice] speed-up skipped for {target.name}: "
                  f"{(result.stderr or '').strip()[-160:]}",
                  file=sys.stderr, flush=True)
            return False
        os.replace(faster, target)
        return True
    except OSError as exc:
        print(f"[voice] speed-up skipped for {target.name}: {exc}",
              file=sys.stderr, flush=True)
        return False
    finally:
        faster.unlink(missing_ok=True)


def speak_beat(text: str, target: str | Path, settings, *,
               engine: str | None = None) -> tuple[str, int, str]:
    """Say one beat. Returns ``(engine_used, spans, note)``.

    The engine choice and the Piper-to-edge fallback live here rather than
    inside ``synth_plan``'s loop because there are now two callers: the
    whole plan, and one beat being re-spoken after a human corrected a
    word at the voice gate. Two copies of "which engine, and what happens
    when it is missing" is exactly the drift this repo keeps paying for.

    ``engine`` overrides ``settings.voice_engine``, which is how the plan
    loop carries a fallback forward: once Piper has failed it passes
    ``"edge"`` for the remaining beats instead of retrying a broken
    install nine more times.
    """
    engine = (engine or settings.voice_engine or "piper").strip().lower()
    target = Path(target)

    if engine != "piper":
        spans = synth_beat_edge(text, target, settings)
        _trim_beat_edges(target, settings)
        speed_beat(target, settings)
        return "edge", spans, "edge"

    from engine.media.piper_voice import PiperUnavailable
    try:
        spans = synth_beat_piper(text, target, settings)
        # After synthesis and before anything measures the file: the beat's
        # duration is read back from disk and drives the clip count, the
        # slot spans and the caption timings, so trimming later would
        # leave all three describing a file that no longer exists.
        _trim_beat_edges(target, settings)
        # Trim first, then speed: trimming keys on level, and the silence
        # it looks for is easier to find at the pace it was spoken.
        speed_beat(target, settings)
        return "piper", spans, "piper"
    except (PiperUnavailable, OSError) as exc:
        # Say so loudly. This was silent, and a run that quietly used
        # edge-tts looked identical to one that used Piper until someone
        # noticed the voice had changed.
        print(f"[voice] Piper unavailable, falling back to edge-tts: {exc}",
              file=sys.stderr, flush=True)
        return ("edge", synth_beat_edge(text, target, settings),
                f"edge (piper unavailable: {str(exc)[:60]})")


def beat_audio_path(plan_id: str, beat_id: str,
                    work_dir: str | Path) -> Path:
    """Where synthesis writes one beat. The single definition of it.

    ``synth_plan`` built this inline, and the re-speak route needs the
    identical path so a corrected beat overwrites the take it replaces
    rather than accumulating a second file nothing reads.
    """
    return Path(work_dir) / plan_id / "audio" / f"{beat_id}.mp3"


def synth_plan(plan: ReelPlan, work_dir: str | Path, settings,
               progress=None) -> dict[str, int]:
    """Fill audio_path, measured_seconds and words for every beat.

    The engine is chosen by ``settings.voice_engine``. Piper is the default;
    edge-tts is the fallback, used when Piper is selected but cannot run at
    all — a missing model, a failed download, a broken install. Falling back
    once and carrying on is better than losing an approved script to a voice
    problem, and the engine that was actually used is reported through
    ``progress``.

    Returns the timing-source counts, the way ``generate_plan_clips`` returns
    its provider counts: ``{"whisper": 7, "whisper-span": 2,
    "interpolated": 1}``. Same reasoning — the fallback here is silent by
    design, so the run has to say what it actually did.
    """
    root = Path(work_dir)
    (root / plan.plan_id / "audio").mkdir(parents=True, exist_ok=True)

    engine = (settings.voice_engine or "piper").strip().lower()
    # One aligner for the whole plan, or None when RAHASYA_ALIGN is off.
    # Built here rather than inside the loop because loading the Whisper
    # model costs ~8s and transcribing a beat costs ~1.5s: per beat it would
    # dominate the stage.
    aligner = build_aligner(settings)
    sources: dict[str, int] = {}

    for index, beat in enumerate(plan.script.beats):
        target = beat_audio_path(plan.plan_id, beat.beat_id, root)

        was = engine
        engine, spoken, used = speak_beat(beat.voice_text, target, settings,
                                          engine=engine)
        if engine != was:
            # Carried forward deliberately: if Piper is broken for one beat
            # it is broken for all of them, and retrying it per beat would
            # only be slow. Said out loud for the same reason the fallback
            # itself is — it used to be silent.
            print(f"[voice] the rest of this plan will use {engine}",
                  file=sys.stderr, flush=True)

        beat.audio_path = str(target)
        beat.voice_engine = engine
        beat.measured_seconds = probe_duration(target, settings.ffmpeg)
        # Burned captions are Roman, the voice is Devanagari; align the
        # on-screen words to this beat's measured span. Neither TTS engine
        # reports usable word timings, so the choice is between measuring
        # them back off the audio and interpolating them.
        if aligner is None:
            beat.words = caption_timings(beat.caption_text,
                                         beat.measured_seconds)
            beat.word_timing_source = INTERPOLATED
        else:
            result = aligner.align(target, beat.caption_text,
                                   beat.measured_seconds)
            beat.words = result.words
            beat.word_timing_source = result.source
            used = f"{used} / {result.source}"
            if result.source != WHISPER:
                print(f"[align] {beat.beat_id}: {result.source} "
                      f"({result.detail})", file=sys.stderr, flush=True)
        sources[beat.word_timing_source] = \
            sources.get(beat.word_timing_source, 0) + 1
        beat.spoken_words = spoken
        if progress:
            progress(index + 1, len(plan.script.beats), beat.beat_id,
                     beat.measured_seconds, used)

    if aligner is not None:
        # One summary line, the way the clip stage prints its provider
        # counts. A run that quietly interpolated every beat is the exact
        # thing this is here to make visible.
        print("[align] " + ", ".join(f"{k}={v}" for k, v in
                                     sorted(sources.items())),
              file=sys.stderr, flush=True)
    return sources


def stitch_narration(plan: ReelPlan, out_path: str | Path,
                     ffmpeg: str) -> str:
    """Concatenate per-beat mp3s into one narration track."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = out_path.with_suffix(".txt")
    lines = [f"file '{Path(b.audio_path).as_posix()}'"
             for b in plan.script.beats if b.audio_path]
    listing.write_text("\n".join(lines), encoding="utf-8")

    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame", "-q:a", "2",
         str(out_path)], check=True, capture_output=True)
    return str(out_path)


def largest_silence_gap(plan: ReelPlan) -> float:
    """Biggest gap between consecutive words, used by the QC scorecard."""
    gap = 0.0
    for beat in plan.script.beats:
        for a, b in zip(beat.words, beat.words[1:]):
            gap = max(gap, b.start - a.end)
    return gap


def word_alignment(plan: ReelPlan) -> float:
    """Fraction of caption words that carry a timing.

    Checks the captions, not the narration: a missing timing here is what
    actually desyncs the burned text from the audio.
    """
    expected = 0
    got = 0
    for beat in plan.script.beats:
        expected += len([w for w in re.split(r"\s+", beat.caption_text) if w])
        got += len(beat.words)
    return (got / expected) if expected else 1.0


def _demo(text: str) -> int:
    from engine.config import settings

    settings.ensure_dirs()
    out = settings.work_dir / "voice_demo.mp3"
    chunks: list[dict] = []

    async def capture() -> list:
        import edge_tts
        out.parent.mkdir(parents=True, exist_ok=True)
        comm = edge_tts.Communicate(text, settings.voice,
                                    rate=settings.voice_rate,
                                    pitch=settings.voice_pitch)
        with out.open("wb") as handle:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                else:
                    chunks.append(chunk)
        return offsets_to_timings(chunks)

    words = asyncio.run(capture())
    seconds = probe_duration(out, settings.ffmpeg)
    kinds = sorted({c.get("type") for c in chunks})
    native = any(c.get("type") == "WordBoundary" for c in chunks)
    print(f"events  : {kinds} -> "
          f"{'native word timing' if native else 'interpolated from sentences'}")
    print(f"voice   : {settings.voice} rate={settings.voice_rate} "
          f"pitch={settings.voice_pitch}")
    print(f"file    : {out}  ({out.stat().st_size} bytes)")
    print(f"duration: {seconds:.2f}s")
    print(f"words   : {len(words)}")
    for w in words[:6]:
        print(f"          {w.start:5.2f}-{w.end:5.2f}  {w.word}")
    return 0 if seconds > 0 and words else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", required=True)
    raise SystemExit(_demo(parser.parse_args().demo))

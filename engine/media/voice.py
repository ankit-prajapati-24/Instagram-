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
import re
import subprocess
import sys
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
    work_dir = Path(work_dir) / plan.plan_id / "audio"
    work_dir.mkdir(parents=True, exist_ok=True)

    engine = (settings.voice_engine or "piper").strip().lower()
    # One aligner for the whole plan, or None when RAHASYA_ALIGN is off.
    # Built here rather than inside the loop because loading the Whisper
    # model costs ~8s and transcribing a beat costs ~1.5s: per beat it would
    # dominate the stage.
    aligner = build_aligner(settings)
    sources: dict[str, int] = {}

    for index, beat in enumerate(plan.script.beats):
        target = work_dir / f"{beat.beat_id}.mp3"

        if engine == "piper":
            from engine.media.piper_voice import PiperUnavailable
            try:
                spoken = synth_beat_piper(beat.voice_text, target, settings)
                used = "piper"
            except (PiperUnavailable, OSError) as exc:
                # Fall back for the rest of the plan too: if Piper is broken
                # for one beat it is broken for all of them, and retrying it
                # per beat would just be slow.
                #
                # Say so loudly. This was silent, and a run that quietly used
                # edge-tts looked identical to one that used Piper until
                # someone noticed the voice had changed.
                print(f"[voice] Piper unavailable, falling back to edge-tts "
                      f"for the rest of this plan: {exc}",
                      file=sys.stderr, flush=True)
                engine = "edge"
                spoken = synth_beat_edge(beat.voice_text, target, settings)
                used = f"edge (piper unavailable: {str(exc)[:60]})"
        else:
            spoken = synth_beat_edge(beat.voice_text, target, settings)
            used = "edge"

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

"""Hindi voice via local edge-tts.

Deliberately not routed through OmniRoute. The gateway's /v1/audio/speech
documents only ``openai/tts-1``, which speaks Hindi with a foreign accent, and
on a clean install it answers "No credentials for provider: openai" anyway.
edge-tts gives genuine ``hi-IN`` neural voices, free and unmetered.

Timing is two-tier, and this is a measured decision rather than a preference.
edge-tts 7.2.8 emits **only** ``SentenceBoundary`` events — verified against
hi-IN-MadhurNeural, hi-IN-SwaraNeural and en-US-GuyNeural, none of which
produced a single ``WordBoundary``. So:

  * sentence start/end come from the service and are exact;
  * word positions inside a sentence are interpolated by character length.

For 3-5 second beats of 5-10 words that interpolation is visually
indistinguishable from true per-word timing, and it stays deterministic.
``WordBoundary`` is still honoured if a future version starts emitting it.

Tone note: both hi-IN voices are tagged "Friendly, Positive", which is the
wrong register for dark mystery. The default rate/pitch offsets in
engine.config pull them darker. That needs an ear test, not a unit test.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
from pathlib import Path

from engine.contract import ReelPlan, WordTiming

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


def synth_plan(plan: ReelPlan, work_dir: str | Path, settings,
               progress=None) -> None:
    """Fill audio_path, measured_seconds and words for every beat."""
    work_dir = Path(work_dir) / plan.plan_id / "audio"
    work_dir.mkdir(parents=True, exist_ok=True)

    async def run() -> None:
        for index, beat in enumerate(plan.script.beats):
            target = work_dir / f"{beat.beat_id}.mp3"
            spoken = await synth_beat(
                beat.voice_text, target, voice=settings.voice,
                rate=settings.voice_rate, pitch=settings.voice_pitch)
            beat.audio_path = str(target)
            beat.measured_seconds = probe_duration(target, settings.ffmpeg)
            # Burned captions are Roman, the voice is Devanagari; align the
            # on-screen words to this beat's measured span, not to `spoken`.
            beat.words = caption_timings(beat.caption_text,
                                         beat.measured_seconds)
            beat.spoken_words = len(spoken)
            if progress:
                progress(index + 1, len(plan.script.beats), beat.beat_id,
                         beat.measured_seconds)

    asyncio.run(run())


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

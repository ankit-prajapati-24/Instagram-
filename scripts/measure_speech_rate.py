"""Measure how fast the configured voice actually speaks Hindi.

The script agent is given a word budget rather than a beat count, because
word count is what decides runtime. That budget is
``target_seconds * words_per_second``, and this is where the second number
comes from. Re-run it whenever the engine, the voice or length_scale change.

    python scripts/measure_speech_rate.py

What it measures, and why it measures that
------------------------------------------
It synthesises ``engine.fake_client.SAMPLE_BEATS`` -- the repo's only worked
example of a script -- one beat at a time, exactly as ``synth_plan`` does.
That is deliberate, and it replaced a separate list of sample lines kept in
this file.

The separate list was wrong twice. First it was five clean conversational
lines with no numerals, dates or acronyms: it read 2.94 w/s while production
ran at 2.29, and 3.03 was configured from it, which handed the script agent a
third more words than the duration window could hold. Then numeral lines were
added to it and it read 2.41 -- inside its own 8% drift check by three
points, on a sample that was by then half numerals, denser than any real
script, and still faster than production. Each round made the number less
wrong without making the sample representative; a third round would have done
the same again.

Measuring the worked example removes the choice. There is one script in this
repo, other tests already hold it to the word budget and the duration window,
and synthesising it beat by beat pays ``--sentence-silence`` once per beat
the way a real run does -- a per-beat cost a flat list of lines never paid,
and part of why this tool always read faster than production.

What it can still not know is how numeral-dense the *next* script will be.
The sample is prose with its numbers spelled out, which is the fast end of
what this voice does. So the reading is printed and checked as a **ceiling**:
configuring at or below it is fine, configuring above it is the failure that
happened. It no longer claims a two-sided "accurate" verdict it has no sample
to support.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.fake_client import SAMPLE_BEATS  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

# Every per-script rate this repo has evidence for. The rate is a property of
# the content -- numerals, dates and acronyms are slow per "word", because
# Piper says "१९६५" as "unnees sau painsath", one word and eleven syllables --
# so a single measurement is a point inside this spread, never the whole of
# it. Cited in engine/config.py and in the pre-render gate's margin.
OBSERVED_SLOWEST = 1.83   # numeral- and acronym-heavy lines
OBSERVED_FASTEST = 2.94   # clean conversational Hindi, no numerals


def sample_lines() -> list[str]:
    """The worked example's narration, beat by beat, in order."""
    return [voice for _role, voice, *_rest in SAMPLE_BEATS]


def verdict(measured: float, configured: float) -> tuple[bool, str]:
    """Is ``configured`` a defensible setting given this measurement?

    One-sided on purpose. Above the measured rate is the failure this tool
    exists to prevent: the budget then asks for more words than the voice can
    say inside the window, and nothing discovers it until the render. Below it
    is the safe direction, and the direction production landed in -- so it is
    only reported when it falls under anything ever measured here, which
    starves the script instead.
    """
    if configured > measured:
        return False, (
            f"the configured {configured:.2f} w/s is faster than the "
            f"{measured:.2f} w/s this voice manages on the sample script, "
            f"and the sample is prose with its numbers spelled out -- the "
            f"fast end of what real scripts contain. Treat {measured:.2f} as "
            f"a ceiling, not a target: set RAHASYA_WORDS_PER_SEC to at most "
            f"{measured:.2f}, and lower if your topics carry dates, figures "
            f"or acronyms. Configuring above the ceiling is how 3.03 got set "
            f"and how a 66.5s video reached QC.")
    if configured < OBSERVED_SLOWEST:
        return False, (
            f"the configured {configured:.2f} w/s is slower than "
            f"{OBSERVED_SLOWEST:.2f}, the slowest rate ever measured here. "
            f"That is not caution, it is a budget too small to fill the "
            f"window: the script agent will be asked for a script that "
            f"renders short.")
    return True, (
        f"the configured {configured:.2f} w/s sits at or below the "
        f"{measured:.2f} w/s ceiling and inside the {OBSERVED_SLOWEST:.2f}-"
        f"{OBSERVED_FASTEST:.2f} w/s spread this repo has measured. Nothing "
        f"to change.")


def synth(text: str, out: Path, settings) -> None:
    if (settings.voice_engine or "piper").lower() == "piper":
        from engine.media import piper_voice
        piper_voice.synth(text, out, settings)
    else:
        import asyncio
        from engine.media.voice import synth_beat
        asyncio.run(synth_beat(text, out, voice=settings.voice,
                               rate=settings.voice_rate,
                               pitch=settings.voice_pitch))


def main() -> int:
    settings = Settings()
    settings.ensure_dirs()
    tmp = Path(tempfile.mkdtemp())

    engine = (settings.voice_engine or "piper").lower()
    detail = (f"{settings.piper_voice} @ length_scale "
              f"{settings.piper_length_scale}" if engine == "piper"
              else f"{settings.voice} @ {settings.voice_rate}")
    lines = sample_lines()
    print(f"engine: {engine} ({detail})")
    print(f"sample: engine.fake_client.SAMPLE_BEATS, "
          f"{len(lines)} beats synthesised one at a time\n")

    total_words = total_seconds = 0.0
    for index, text in enumerate(lines):
        out = tmp / f"b{index + 1}.mp3"
        synth(text, out, settings)
        seconds = probe_duration(out, settings.ffmpeg)
        words = len(text.split())
        total_words += words
        total_seconds += seconds
        print(f"  beat {index + 1:2d}: {words:2d} words -> {seconds:5.2f}s   "
              f"({words / seconds:.2f} w/s)")

    rate = total_words / total_seconds
    budget = int(settings.target_seconds * rate)

    # Named, because it is a real part of the runtime and the old flat sample
    # under-paid it: every beat is its own synthesis and carries its own
    # trailing silence.
    silence = len(lines) * settings.piper_sentence_silence

    print(f"\n  {total_words:.0f} words -> {total_seconds:.2f}s "
          f"across {len(lines)} beats")
    if engine == "piper":
        print(f"  of which ~{silence:.2f}s is the per-beat sentence silence "
              f"({settings.piper_sentence_silence}s x {len(lines)})")
    print(f"\n  measured rate : {rate:.2f} words/sec  "
          f"(a CEILING: this sample spells its numbers out)")
    print(f"  configured    : {settings.words_per_second:.2f} words/sec")
    print(f"  measured spread in this repo: {OBSERVED_SLOWEST:.2f}-"
          f"{OBSERVED_FASTEST:.2f} words/sec")
    print(f"  word budget for {settings.target_seconds:.0f}s at the "
          f"ceiling: {budget} words")

    ok, advice = verdict(rate, settings.words_per_second)
    print(f"\n  {'OK' if ok else 'PROBLEM'}: {advice}")
    if not (OBSERVED_SLOWEST <= rate <= OBSERVED_FASTEST):
        print(f"\n  Note: {rate:.2f} w/s is outside the spread recorded "
              f"above, so the voice or its tuning has changed materially. "
              f"Update OBSERVED_SLOWEST/OBSERVED_FASTEST here and the "
              f"evidence quoted in engine/config.py before trusting either.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

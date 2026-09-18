"""Measure how fast the configured voice actually speaks Hindi.

The script agent is given a word budget rather than a beat count, because
word count is what decides runtime. That budget is
``target_seconds * words_per_second``, and this is where the second number
comes from. Re-run it whenever the engine, the voice or length_scale change.

    python scripts/measure_speech_rate.py

It prints the measured rate and the RAHASYA_WORDS_PER_SEC line to put in
.env if it differs from what is configured.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

# Short, medium and long lines, so the rate is not measured on one shape.
SAMPLES = [
    "तीन साल पहले इसी जगह पर एक आदमी गायब हो गया।",
    "सीसीटीवी में वो अंदर जाता दिखता है लेकिन बाहर आता कभी नहीं।",
    "पुलिस ने केस बंद कर दिया।",
    "और हर रात ठीक दो बजकर चौदह मिनट पर उस नंबर से एक मैसेज आता है "
    "जो कोई नहीं भेजता।",
    "तुम्हें क्या लगता है कौन भेज रहा है और क्यों।",
]


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
    print(f"engine: {engine} ({detail})\n")

    total_words = total_seconds = 0.0
    for index, text in enumerate(SAMPLES):
        out = tmp / f"s{index}.mp3"
        synth(text, out, settings)
        seconds = probe_duration(out, settings.ffmpeg)
        words = len(text.split())
        total_words += words
        total_seconds += seconds
        print(f"  {words:2d} words -> {seconds:5.2f}s   "
              f"({words / seconds:.2f} w/s)")

    rate = total_words / total_seconds
    budget = int(settings.target_seconds * rate)
    print(f"\n  measured rate : {rate:.2f} words/sec")
    print(f"  configured    : {settings.words_per_second:.2f} words/sec")
    print(f"  word budget for {settings.target_seconds:.0f}s: {budget} words")

    drift = abs(rate - settings.words_per_second) / rate
    if drift > 0.08:
        print(f"\n  Configured rate is off by {drift * 100:.0f}%. Put this in "
              f".env:\n    RAHASYA_WORDS_PER_SEC={rate:.2f}")
    else:
        print("\n  Configured rate is accurate; nothing to change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

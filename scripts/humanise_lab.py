"""Make one TTS voice sound less synthetic, one change at a time.

The voice itself is settled: hi-IN-MadhurNeural, untuned. The rate and pitch
offsets that were in the config made it *worse* — they were chosen to sound
"darker" and instead flattened the delivery.

This renders a progression into work/humanlab/ so each change can be judged
on its own:

    h1  baseline      untuned Madhur, exactly as the engine makes it now
    h2  + processing  EQ, de-ess, compression, a small room
    h3  + punctuation same words, written for delivery instead of reading
    h4  + pacing      each line at its own rate, the way a narrator varies
    h5  + music bed   a low drone under everything

Why processing matters: TTS output is bone dry. Real recorded speech always
has a room around it, and the absence of one is a large part of what the ear
reads as "synthetic" — before it ever judges the intonation.

    python scripts/humanise_lab.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

OUT = Path("work/humanlab")
VOICE = "hi-IN-MadhurNeural"

# As the script agent writes it today: clean, even, unpunctuated.
PLAIN = [
    "1911 में एक सौ छह लोग सुरंग में गए, वापस क्यों नहीं आए?",
    "इटली। लोम्बार्डी। उन्नीस सौ ग्यारह की एक सुबह।",
    "ज़ानेत्ती ट्रेन में एक सौ छह यात्री थे।",
    "लेकिन इटली के अपने रिकॉर्ड में इस ट्रेन का कोई ज़िक्र नहीं है।",
    "तो फिर ये कहानी आई कहाँ से?",
]

# Same words. Punctuation and line breaks chosen for how it should be *said*:
# commas force breath, ellipses force hesitation, a one-word sentence lands.
PUNCTUATED = [
    "1911 में... एक सौ छह लोग सुरंग में गए। वापस कोई नहीं आया।",
    "इटली। लोम्बार्डी। उन्नीस सौ ग्यारह की, एक सुबह।",
    "ज़ानेत्ती ट्रेन। एक सौ छह यात्री। एक सुरंग।",
    "लेकिन... इटली के अपने रिकॉर्ड में, इस ट्रेन का कोई ज़िक्र नहीं है।",
    "तो फिर... ये कहानी आई कहाँ से?",
]

# A narrator does not read every line at one speed. Slow on the hook, quicker
# through setup, slowest on the reveal.
RATES = ["-6%", "+4%", "+6%", "-10%", "-14%"]

# Dry TTS in a small room, with the boxiness cut and presence lifted.
PROCESS = (
    "highpass=f=85,"
    "equalizer=f=300:t=q:w=1.2:g=-3.5,"      # cut the boxy low-mid
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"      # lift presence/consonants
    "deesser=i=0.4,"
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2,"
    "aecho=0.86:0.9:32:0.12,"                # a small room, not an effect
    "loudnorm=I=-15:TP=-1.5:LRA=11"
)


async def synth(text: str, path: Path, rate: str = "+0%",
                pitch: str = "+0Hz") -> None:
    import edge_tts

    path.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch)
    with path.open("wb") as handle:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])


def run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def concat(parts: list[Path], out: Path, ffmpeg: str) -> None:
    listing = out.with_suffix(".txt")
    listing.write_text("\n".join(f"file '{p.resolve().as_posix()}'"
                                 for p in parts), encoding="utf-8")
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame", "-q:a", "2",
         str(out)])


def process(src: Path, out: Path, ffmpeg: str) -> None:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", PROCESS, "-c:a", "libmp3lame", "-q:a", "2", str(out)])


def make_drone(out: Path, seconds: float, ffmpeg: str) -> None:
    """A low, slow bed generated from scratch, so nothing is licensed."""
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=55:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=82.5:duration={seconds}",
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=brown:a=0.06",
         "-filter_complex",
         "[0:a]volume=0.32[a];[1:a]volume=0.16[b];[2:a]lowpass=f=420[c];"
         "[a][b][c]amix=inputs=3:duration=first,"
         "tremolo=f=0.12:d=0.35,lowpass=f=900,volume=0.5",
         "-c:a", "libmp3lame", "-q:a", "4", str(out)])


def mix(voice: Path, bed: Path, out: Path, ffmpeg: str) -> None:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(voice), "-i", str(bed),
         "-filter_complex",
         "[1:a]volume=-20dB[bed];"
         "[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0,"
         "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-c:a", "libmp3lame", "-q:a", "2", str(out)])


def main() -> int:
    settings = Settings()
    ffmpeg = settings.ffmpeg
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "_parts"
    raw.mkdir(exist_ok=True)

    def report(label: str, path: Path, note: str) -> None:
        print(f"  {label:<16} {probe_duration(path, ffmpeg):5.2f}s   {note}")

    # h1 — exactly what the engine makes today, untuned.
    parts = []
    for i, line in enumerate(PLAIN):
        p = raw / f"plain{i}.mp3"
        asyncio.run(synth(line, p))
        parts.append(p)
    h1 = OUT / "h1-baseline.mp3"
    concat(parts, h1, ffmpeg)
    report("h1-baseline", h1, "untuned Madhur, raw")

    # h2 — same audio, processed.
    h2 = OUT / "h2-processed.mp3"
    process(h1, h2, ffmpeg)
    report("h2-processed", h2, "+ EQ, de-ess, compression, small room")

    # h3 — punctuation written for delivery.
    parts = []
    for i, line in enumerate(PUNCTUATED):
        p = raw / f"punct{i}.mp3"
        asyncio.run(synth(line, p))
        parts.append(p)
    h3raw = raw / "punct-all.mp3"
    concat(parts, h3raw, ffmpeg)
    h3 = OUT / "h3-punctuation.mp3"
    process(h3raw, h3, ffmpeg)
    report("h3-punctuation", h3, "+ commas, ellipses, shorter sentences")

    # h4 — per-line pacing.
    parts = []
    for i, (line, rate) in enumerate(zip(PUNCTUATED, RATES)):
        p = raw / f"dyn{i}.mp3"
        asyncio.run(synth(line, p, rate=rate))
        parts.append(p)
    h4raw = raw / "dyn-all.mp3"
    concat(parts, h4raw, ffmpeg)
    h4 = OUT / "h4-pacing.mp3"
    process(h4raw, h4, ffmpeg)
    report("h4-pacing", h4, f"+ per-line rate {RATES}")

    # h5 — with a bed under it.
    seconds = probe_duration(h4, ffmpeg)
    bed = raw / "bed.mp3"
    make_drone(bed, seconds + 1.0, ffmpeg)
    h5 = OUT / "h5-with-music.mp3"
    mix(h4, bed, h5, ffmpeg)
    report("h5-with-music", h5, "+ low drone at -20dB")

    print(f"\nAll in {OUT.resolve()}")
    print("Listen h1 -> h5 in order. Each file adds exactly one thing, so "
          "whichever step stops sounding robotic is the one that mattered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

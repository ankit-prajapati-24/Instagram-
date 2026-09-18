"""Tune the Piper hi_IN voice that sounded closest to human.

Piper exposes the knobs that actually cause the mechanical quality, which
edge-tts does not:

    length_scale      how long each phoneme is held. >1 is slower and more
                      deliberate, which suits this niche.
    noise_w_scale     variation in phoneme *duration*. This is the rhythm
                      knob — a fixed value is what makes speech tick like a
                      metronome, and raising it is the single biggest move
                      away from robotic.
    noise_scale       variation in the generated waveform: expression.
    sentence_silence  real pauses between sentences. Narration breathes; TTS
                      by default does not.

    python scripts/piper_tune.py

Everything lands in work/pipertune/ and is processed identically (EQ, de-ess,
compression, a small room) except 00, which is left raw for reference.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

OUT = Path("work/pipertune")
VOICE_DIR = Path("work/piper")
VOICE = "pratham"

TEXT = (
    "तीन साल पहले, इसी जगह पर, एक आदमी गायब हो गया। "
    "सीसीटीवी में वो अंदर जाता दिखता है। "
    "बाहर आता... कभी नहीं। "
    "पुलिस ने केस बंद कर दिया। "
    "लेकिन उसका फ़ोन... आज भी चालू है। "
    "और हर रात, ठीक दो बजकर चौदह मिनट पर, उस नंबर से एक मैसेज आता है। "
    "तुम्हें क्या लगता है — कौन भेज रहा है?"
)

PROCESS = (
    "highpass=f=85,"
    "equalizer=f=300:t=q:w=1.2:g=-3.5,"
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"
    "deesser=i=0.4,"
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2,"
    "aecho=0.86:0.9:32:0.12,"
    "loudnorm=I=-15:TP=-1.5:LRA=11"
)

# name, length_scale, noise_scale, noise_w_scale, sentence_silence, processed
VARIANTS = [
    ("00-default-raw", 1.0, 0.667, 0.8, 0.2, False),
    ("01-default", 1.0, 0.667, 0.8, 0.2, True),
    ("02-rhythm", 1.0, 0.667, 1.1, 0.2, True),
    ("03-rhythm-more", 1.0, 0.667, 1.35, 0.2, True),
    ("04-expression", 1.0, 0.85, 0.8, 0.2, True),
    ("05-slower-breath", 1.12, 0.667, 0.8, 0.45, True),
    ("06-combined", 1.10, 0.80, 1.15, 0.40, True),
    ("07-combined-strong", 1.18, 0.92, 1.35, 0.55, True),
]


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, **kw)


def ensure_voice() -> Path:
    onnx = VOICE_DIR / f"{VOICE}.onnx"
    if onnx.exists():
        return onnx
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    api = ("https://huggingface.co/api/models/rhasspy/piper-voices/tree/"
           f"main/hi/hi_IN/{VOICE}")
    entries = json.load(urllib.request.urlopen(api, timeout=30))
    quality = next(e["path"].split("/")[-1] for e in entries
                   if e.get("type") == "directory")
    stem = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/"
            f"hi_IN/{VOICE}/{quality}/hi_IN-{VOICE}-{quality}.onnx")
    urllib.request.urlretrieve(stem, onnx)
    urllib.request.urlretrieve(stem + ".json", str(onnx) + ".json")
    return onnx


def main() -> int:
    settings = Settings()
    ffmpeg = settings.ffmpeg
    OUT.mkdir(parents=True, exist_ok=True)
    raw_dir = OUT / "_raw"
    raw_dir.mkdir(exist_ok=True)

    onnx = ensure_voice()
    print(f"voice: {onnx}\nwriting to {OUT.resolve()}\n")
    print(f"  {'name':<22} {'len':>5} {'noise':>6} {'noiseW':>7} "
          f"{'pause':>6}   dur")

    for name, length, noise, noise_w, silence, processed in VARIANTS:
        wav = raw_dir / f"{name}.wav"
        try:
            run([sys.executable, "-m", "piper", "-m", str(onnx),
                 "-f", str(wav),
                 "--length-scale", str(length),
                 "--noise-scale", str(noise),
                 "--noise-w-scale", str(noise_w),
                 "--sentence-silence", str(silence)],
                input=TEXT.encode("utf-8"))

            target = OUT / f"{name}.mp3"
            if processed:
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(wav), "-af", PROCESS,
                     "-c:a", "libmp3lame", "-q:a", "2", str(target)])
            else:
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(wav), "-c:a", "libmp3lame", "-q:a", "2",
                     str(target)])

            print(f"  {name:<22} {length:>5.2f} {noise:>6.3f} "
                  f"{noise_w:>7.2f} {silence:>6.2f}   "
                  f"{probe_duration(target, ffmpeg):5.2f}s")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[:70]
            print(f"  {name:<22} FAILED {detail}")

    print("\n00 is raw for reference; everything else is processed the same.")
    print("03 isolates rhythm, 05 isolates pace and pauses, 06 and 07 "
          "combine them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

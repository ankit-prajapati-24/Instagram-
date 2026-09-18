"""Push the Piper hi_IN voice as far as local processing can take it.

The earlier tuning pass changed Piper's own knobs but still synthesised the
whole script in one go and processed it as one block. A narrator does not
work that way, so this assembles the line sentence by sentence and varies
what a person naturally varies:

  pace      each sentence gets its own length_scale, keyed to its job in the
            script — a hook lands slow, setup moves, a reveal slows again.
  pitch     each sentence starts at a slightly different pitch. Beginning
            every sentence on the same note is a strong synthetic tell, and
            Piper has no pitch control, so it is done in ffmpeg.
  pauses    gaps between sentences vary instead of being a constant, and get
            longer at the turns (a "lekin", the closing question).
  room      a small space built from three echo taps rather than one, so the
            reflections are not a single repeat.

    python scripts/piper_humanise.py

Output in work/humanpiper/. v0 is the current single-pass version for
reference; each later file adds one layer.
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

OUT = Path("work/humanpiper")
ONNX = Path("work/piper/pratham.onnx")

# sentence, role, length_scale, semitone offset, pause after (seconds)
LINES = [
    ("तीन साल पहले, इसी जगह पर, एक आदमी गायब हो गया।",
     "hook", 1.14, +0.0, 0.42),
    ("सीसीटीवी में वो अंदर जाता दिखता है।",
     "setup", 1.02, -0.4, 0.22),
    ("बाहर आता... कभी नहीं।",
     "beat", 1.22, -1.1, 0.58),
    ("पुलिस ने केस बंद कर दिया।",
     "setup", 1.00, +0.3, 0.30),
    ("लेकिन उसका फ़ोन... आज भी चालू है।",
     "turn", 1.16, +0.6, 0.50),
    ("और हर रात, ठीक दो बजकर चौदह मिनट पर, उस नंबर से एक मैसेज आता है।",
     "reveal", 1.08, -0.5, 0.46),
    ("तुम्हें क्या लगता है — कौन भेज रहा है?",
     "close", 1.18, +0.8, 0.0),
]

# Piper's own knobs, held constant here so the variation being tested is the
# sentence-level one.
NOISE, NOISE_W, SILENCE = 0.72, 1.15, 0.05

SR = 22050

# Three taps rather than one: a single echo reads as an effect, several short
# ones read as a room.
ROOM = "aecho=0.88:0.9:17|31|53:0.10|0.07|0.05"

PROCESS_BASE = (
    "highpass=f=85,"
    "equalizer=f=300:t=q:w=1.2:g=-3.5,"
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"
    "deesser=i=0.4,"
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2"
)


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, **kw)


def piper(text: str, out: Path, length: float) -> None:
    run([sys.executable, "-m", "piper", "-m", str(ONNX), "-f", str(out),
         "--length-scale", str(length),
         "--noise-scale", str(NOISE),
         "--noise-w-scale", str(NOISE_W),
         "--sentence-silence", str(SILENCE)],
        input=text.encode("utf-8"))


def shift_pitch(src: Path, out: Path, semitones: float, ffmpeg: str) -> None:
    """Shift pitch without changing speed.

    asetrate moves pitch and tempo together; atempo puts the tempo back.
    """
    if abs(semitones) < 0.01:
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i",
             str(src), "-c", "copy", str(out)])
        return
    ratio = 2 ** (semitones / 12)
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", f"asetrate={int(SR * ratio)},aresample={SR},"
                f"atempo={1 / ratio:.6f}",
         str(out)])


def silence(seconds: float, out: Path, ffmpeg: str) -> None:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r={SR}:cl=mono", "-t", f"{seconds:.3f}", str(out)])


def concat(parts: list[Path], out: Path, ffmpeg: str) -> None:
    listing = out.with_suffix(".txt")
    listing.write_text("\n".join(f"file '{p.resolve().as_posix()}'"
                                 for p in parts), encoding="utf-8")
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", str(listing), "-ar", str(SR), "-ac", "1",
         str(out)])


def finish(src: Path, out: Path, ffmpeg: str, room: bool) -> None:
    chain = PROCESS_BASE + ("," + ROOM if room else "")
    chain += ",loudnorm=I=-15:TP=-1.5:LRA=11"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", chain, "-c:a", "libmp3lame", "-q:a", "2", str(out)])


def build(name: str, *, vary_pace: bool, vary_pitch: bool,
          vary_pause: bool, room: bool, ffmpeg: str, work: Path) -> Path:
    rng = random.Random(7)
    parts: list[Path] = []

    for index, (text, _role, length, semis, pause) in enumerate(LINES):
        wav = work / f"{name}-{index}.wav"
        piper(text, wav, length if vary_pace else 1.10)

        if vary_pitch and abs(semis) > 0.01:
            shifted = work / f"{name}-{index}-p.wav"
            shift_pitch(wav, shifted, semis, ffmpeg)
            wav = shifted
        parts.append(wav)

        if pause > 0:
            gap = pause if vary_pause else 0.30
            # A little jitter, so the gaps are not machine-identical either.
            if vary_pause:
                gap += rng.uniform(-0.05, 0.05)
            quiet = work / f"{name}-{index}-gap.wav"
            silence(max(gap, 0.05), quiet, ffmpeg)
            parts.append(quiet)

    joined = work / f"{name}-joined.wav"
    concat(parts, joined, ffmpeg)
    out = OUT / f"{name}.mp3"
    finish(joined, out, ffmpeg, room)
    return out


def main() -> int:
    settings = Settings()
    ffmpeg = settings.ffmpeg
    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / "_work"
    work.mkdir(exist_ok=True)

    if not ONNX.exists():
        print(f"missing {ONNX} — run scripts/piper_tune.py first")
        return 1

    plan = [
        ("v0-single-pass", False, False, False, True,
         "one request, flat processing (what you heard)"),
        ("v1-pauses", False, False, True, True,
         "+ varied pauses between sentences"),
        ("v2-pace", True, False, True, True,
         "+ per-sentence pace"),
        ("v3-pitch", True, True, True, True,
         "+ per-sentence pitch shift"),
        ("v4-no-room", True, True, True, False,
         "same as v3 but with the room removed"),
    ]

    print(f"writing to {OUT.resolve()}\n")
    for name, pace, pitch, pause, room, note in plan:
        if name == "v0-single-pass":
            wav = work / "v0.wav"
            piper(" ".join(t for t, *_ in LINES), wav, 1.10)
            out = OUT / f"{name}.mp3"
            finish(wav, out, ffmpeg, room)
        else:
            out = build(name, vary_pace=pace, vary_pitch=pitch,
                        vary_pause=pause, room=room, ffmpeg=ffmpeg,
                        work=work)
        print(f"  {name:<18} {probe_duration(out, ffmpeg):5.2f}s   {note}")

    print("\nv0 -> v3 each add one layer. v4 exists to check whether the "
          "room is helping or hurting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

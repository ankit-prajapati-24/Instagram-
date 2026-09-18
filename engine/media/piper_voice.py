"""Piper TTS: the local Hindi voice this pipeline uses.

Chosen over edge-tts by ear after a side-by-side of twelve edge voices, three
Piper voices and several tuning passes. It also removes two problems that
edge-tts carried: it runs offline with no network dependency, and it is
MIT-licensed, so the open question about edge-tts's commercial terms goes
away.

Two things it does NOT do, both settled by investigation rather than guess:

  * It cannot be trained closer to a commercial voice on this machine. Piper
    fine-tuning wants ~24 GB of VRAM; there is no CPU path, and this machine
    has integrated graphics.
  * Its delivery cannot be fixed by voice conversion. RVC and similar
    preserve the source's pitch contour and timing by design — they change
    who it sounds like, not how it is spoken.

So the voice is what it is, and everything here is about the packaging around
it: pace, a small room, and the EQ that dry synthesis always needs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices"
VOICE_API = ("https://huggingface.co/api/models/rhasspy/piper-voices/tree/"
             "main/hi/hi_IN")

# Dry TTS in a small room. The absence of any space around synthesised speech
# is a large part of what the ear reads as artificial, before it judges the
# intonation at all. Three short taps rather than one: a single echo sounds
# like an effect, several sound like a room.
PROCESS_CHAIN = (
    "highpass=f=85,"
    "equalizer=f=300:t=q:w=1.2:g=-3.5,"      # cut the boxy low-mid
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"      # lift presence and consonants
    "deesser=i=0.4,"
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2,"
    "aecho=0.88:0.9:17|31|53:0.10|0.07|0.05,"
    "loudnorm=I=-15:TP=-1.5:LRA=11"
)


class PiperUnavailable(RuntimeError):
    """Piper cannot synthesise — the caller should fall back."""


def model_path(voice: str, models_dir: str | Path) -> Path:
    return Path(models_dir) / f"{voice}.onnx"


def ensure_model(voice: str, models_dir: str | Path) -> Path:
    """Download the voice on first use. Returns the .onnx path."""
    target = model_path(voice, models_dir)
    if target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        entries = json.load(urllib.request.urlopen(f"{VOICE_API}/{voice}",
                                                   timeout=60))
        quality = next(e["path"].split("/")[-1] for e in entries
                       if e.get("type") == "directory")
        stem = (f"{VOICE_REPO}/resolve/main/hi/hi_IN/{voice}/{quality}/"
                f"hi_IN-{voice}-{quality}.onnx")
        urllib.request.urlretrieve(stem, target)
        urllib.request.urlretrieve(stem + ".json", str(target) + ".json")
    except Exception as exc:
        raise PiperUnavailable(
            f"could not fetch the Piper voice '{voice}': {exc}") from exc
    return target


def synth(text: str, out_path: str | Path, settings) -> None:
    """Synthesise one beat to ``out_path`` as mp3.

    Piper writes wav, so the result is converted and run through the
    processing chain in the same ffmpeg pass.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    onnx = ensure_model(settings.piper_voice, settings.piper_models_dir)
    wav = out_path.with_suffix(".raw.wav")

    command = [
        sys.executable, "-m", "piper", "-m", str(onnx), "-f", str(wav),
        "--length-scale", str(settings.piper_length_scale),
        "--noise-scale", str(settings.piper_noise_scale),
        "--noise-w-scale", str(settings.piper_noise_w),
        "--sentence-silence", str(settings.piper_sentence_silence),
    ]
    result = subprocess.run(command, input=text.encode("utf-8"),
                            capture_output=True)

    if result.returncode != 0 or not wav.exists():
        # Write the whole thing to a file and name it in the exception.
        # Truncating stderr to its last 300 characters once hid the actual
        # cause and left only a traceback tail from Python's wave module.
        detail = result.stderr.decode("utf-8", "replace")
        report = Path(out_path).parent / "piper-error.log"
        try:
            report.write_text("\n".join([
                "command:",
                "  " + " ".join(str(a) for a in command),
                "",
                f"returncode : {result.returncode}",
                f"wav written: {wav.exists()}",
                f"text ({len(text)} chars):",
                "  " + text,
                "",
                "stdout:",
                result.stdout.decode("utf-8", "replace"),
                "",
                "stderr:",
                detail,
            ]), encoding="utf-8")
        except OSError:
            report = None

        first = next((line for line in detail.splitlines()
                      if line.strip() and not line.startswith(" ")
                      and "Traceback" not in line), "")
        raise PiperUnavailable(
            f"piper exited {result.returncode}"
            + (f": {first.strip()[:200]}" if first else "")
            + (f" (full log: {report})" if report else ""))

    chain = PROCESS_CHAIN if settings.voice_process else "loudnorm=I=-15"
    convert = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(wav), "-af", chain,
         "-c:a", "libmp3lame", "-q:a", "2", str(out_path)],
        capture_output=True)

    wav.unlink(missing_ok=True)
    if convert.returncode != 0 or not out_path.exists():
        raise PiperUnavailable(
            "ffmpeg post-processing failed: "
            + convert.stderr.decode("utf-8", "replace")[-300:])


def available(settings) -> bool:
    """Is Piper importable and is its model present or fetchable?"""
    try:
        subprocess.run([sys.executable, "-c", "import piper"],
                       capture_output=True, check=True)
    except Exception:
        return False
    return model_path(settings.piper_voice,
                      settings.piper_models_dir).exists()

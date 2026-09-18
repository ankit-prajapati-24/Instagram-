"""Same opening, every engine and voice, for a side-by-side listen.

The text is a real channel opening rather than a paragraph of facts: it has a
pause, a specific number, a reveal, a "lekin" turn and a closing question.
Those are exactly the places a synthetic voice gives itself away, so it is a
much fairer test than flat narration.

    python scripts/voice_shootout.py

Everything lands in work/shootout/ named so it sorts in listening order.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

OUT = Path("work/shootout")
PIPER_DIR = Path("work/piper")

DEVANAGARI = (
    "तीन साल पहले, इसी जगह पर, एक आदमी गायब हो गया। "
    "सीसीटीवी में वो अंदर जाता दिखता है। "
    "बाहर आता... कभी नहीं। "
    "पुलिस ने केस बंद कर दिया। "
    "लेकिन उसका फ़ोन... आज भी चालू है। "
    "और हर रात, ठीक दो बजकर चौदह मिनट पर, उस नंबर से एक मैसेज आता है। "
    "तुम्हें क्या लगता है — कौन भेज रहा है?"
)

ROMAN = (
    "Teen saal pehle, isi jagah par, ek aadmi gayab ho gaya. "
    "CCTV mein wo andar jaata dikhta hai. "
    "Bahar aata... kabhi nahi. "
    "Police ne case band kar diya. "
    "Lekin uska phone... aaj bhi chalu hai. "
    "Aur har raat, theek do bajkar chaudah minute par, us number se ek "
    "message aata hai. "
    "Tumhe kya lagta hai — kaun bhej raha hai?"
)

# Dry TTS in a small room, boxiness cut, presence lifted.
PROCESS = (
    "highpass=f=85,"
    "equalizer=f=300:t=q:w=1.2:g=-3.5,"
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"
    "deesser=i=0.4,"
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2,"
    "aecho=0.86:0.9:32:0.12,"
    "loudnorm=I=-15:TP=-1.5:LRA=11"
)

EDGE = [
    ("01-edge-madhur-raw", "hi-IN-MadhurNeural", DEVANAGARI, False),
    ("02-edge-madhur-processed", "hi-IN-MadhurNeural", DEVANAGARI, True),
    ("03-edge-swara-processed", "hi-IN-SwaraNeural", DEVANAGARI, True),
    ("04-edge-manohar-processed", "mr-IN-ManoharNeural", DEVANAGARI, True),
    ("05-edge-prabhat-roman", "en-IN-PrabhatNeural", ROMAN, True),
    ("06-edge-neerja-expressive-roman", "en-IN-NeerjaExpressiveNeural",
     ROMAN, True),
]

PIPER = ["pratham", "priyamvada", "rohan"]
PIPER_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "hi/hi_IN")


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, **kw)


def process(src: Path, out: Path, ffmpeg: str) -> None:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", PROCESS, "-c:a", "libmp3lame", "-q:a", "2", str(out)])


async def edge_synth(text: str, path: Path, voice: str) -> None:
    import edge_tts

    path.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text, voice)   # untuned: tuning made it worse
    with path.open("wb") as handle:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])


def ensure_piper_voice(name: str) -> Path | None:
    onnx = PIPER_DIR / f"{name}.onnx"
    if onnx.exists():
        return onnx
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    api = ("https://huggingface.co/api/models/rhasspy/piper-voices/tree/"
           f"main/hi/hi_IN/{name}")
    try:
        entries = json.load(urllib.request.urlopen(api, timeout=30))
        quality = next(e["path"].split("/")[-1] for e in entries
                       if e.get("type") == "directory")
        stem = f"{PIPER_BASE}/{name}/{quality}/hi_IN-{name}-{quality}.onnx"
        urllib.request.urlretrieve(stem, onnx)
        urllib.request.urlretrieve(stem + ".json", str(onnx) + ".json")
        return onnx
    except Exception:
        return None


def main() -> int:
    settings = Settings()
    ffmpeg = settings.ffmpeg
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "_raw"
    raw.mkdir(exist_ok=True)

    print(f"writing to {OUT.resolve()}\n")

    for name, voice, text, processed in EDGE:
        try:
            src = raw / f"{name}.mp3"
            asyncio.run(edge_synth(text, src, voice))
            target = OUT / f"{name}.mp3"
            if processed:
                process(src, target, ffmpeg)
            else:
                target.write_bytes(src.read_bytes())
            script = "devanagari" if text is DEVANAGARI else "roman"
            print(f"  {name:<34} {script:<10} "
                  f"{probe_duration(target, ffmpeg):5.2f}s")
        except Exception as exc:
            print(f"  {name:<34} FAILED {str(exc)[:50]}")

    for index, voice in enumerate(PIPER, start=7):
        onnx = ensure_piper_voice(voice)
        if onnx is None:
            print(f"  {index:02d}-piper-{voice:<26} unavailable")
            continue
        try:
            wav = raw / f"piper-{voice}.wav"
            run([sys.executable, "-m", "piper", "-m", str(onnx),
                 "-f", str(wav)], input=DEVANAGARI.encode("utf-8"))
            target = OUT / f"{index:02d}-piper-{voice}.mp3"
            process(wav, target, ffmpeg)
            print(f"  {index:02d}-piper-{voice:<26} devanagari "
                  f"{probe_duration(target, ffmpeg):5.2f}s")
        except Exception as exc:
            print(f"  {index:02d}-piper-{voice:<26} FAILED {str(exc)[:50]}")

    print(f"\nAll processed the same way except 01, which is left raw as the "
          f"reference.\nEverything is untuned — the rate and pitch offsets "
          f"that were in the config made it worse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

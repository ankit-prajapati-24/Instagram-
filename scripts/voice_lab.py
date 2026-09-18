"""Generate voice candidates side by side so an ear can pick one.

Voice quality is the one thing in this pipeline no test can judge, so this
renders the same lines many ways into work/voicelab/ for back-to-back
listening.

    python scripts/voice_lab.py            # the shortlist
    python scripts/voice_lab.py --all      # every Indian edge-tts voice

Two ideas here are worth more than the tuning knobs:

* **Roman Hinglish through an en-IN voice.** The Devanagari hi-IN voices are
  the obvious choice and both read flat. Indian-English voices fed the Roman
  caption text are a different model entirely, and `NeerjaExpressive` is a
  different tier again.
* **Marathi voices reading Hindi.** Marathi is written in Devanagari, so
  mr-IN voices pronounce Hindi text correctly while being different voice
  actors.

Every variant is synthesised in ONE request. The pipeline currently does one
request per beat, which restarts intonation at every beat and is itself part
of why the result sounds mechanical.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.media.voice import probe_duration  # noqa: E402

OUT = Path("work/voicelab")

# The same five beats, in both scripts.
DEVANAGARI = (
    "1911 में एक सौ छह लोग सुरंग में गए — वापस क्यों नहीं आए? "
    "इटली। लोम्बार्डी। उन्नीस सौ ग्यारह की एक सुबह। "
    "ज़ानेत्ती ट्रेन में एक सौ छह यात्री थे। "
    "लेकिन इटली के अपने रिकॉर्ड में इस ट्रेन का कोई ज़िक्र नहीं है। "
    "तो फिर ये कहानी आई कहाँ से?"
)
ROMAN = (
    "1911 mein ek sau chhe log surang mein gaye — wapas kyun nahi aaye? "
    "Italy. Lombardy. Unnees sau gyarah ki ek subah. "
    "Zanetti train mein ek sau chhe yaatri the. "
    "Lekin Italy ke apne record mein is train ka koi zikr nahi hai. "
    "To phir ye kahaani aayi kahan se?"
)

# name, voice, script, rate, pitch
SHORTLIST = [
    # The two Devanagari voices, for reference.
    ("01-madhur-hi", "hi-IN-MadhurNeural", DEVANAGARI, "-8%", "-6Hz"),
    ("02-swara-hi", "hi-IN-SwaraNeural", DEVANAGARI, "-8%", "-6Hz"),

    # Marathi reads Devanagari: same text, different voice actors.
    ("03-manohar-mr", "mr-IN-ManoharNeural", DEVANAGARI, "-8%", "-6Hz"),
    ("04-aarohi-mr", "mr-IN-AarohiNeural", DEVANAGARI, "-8%", "-6Hz"),

    # Indian English voices fed the Roman caption text.
    ("05-prabhat-roman", "en-IN-PrabhatNeural", ROMAN, "-6%", "-4Hz"),
    ("06-neerja-roman", "en-IN-NeerjaNeural", ROMAN, "-6%", "-4Hz"),
    ("07-neerja-expressive", "en-IN-NeerjaExpressiveNeural", ROMAN,
     "-6%", "-4Hz"),
    ("08-neerja-expressive-flat", "en-IN-NeerjaExpressiveNeural", ROMAN,
     "+0%", "+0Hz"),

    # Urdu is phonetically close to spoken Hindi.
    ("09-salman-ur-roman", "ur-IN-SalmanNeural", ROMAN, "-6%", "-4Hz"),
    ("10-asad-ur-roman", "ur-PK-AsadNeural", ROMAN, "-6%", "-4Hz"),

    # Untuned references: the offsets may themselves be flattening things.
    ("11-madhur-untuned", "hi-IN-MadhurNeural", DEVANAGARI, "+0%", "+0Hz"),
    ("12-prabhat-untuned", "en-IN-PrabhatNeural", ROMAN, "+0%", "+0Hz"),
]


async def synth(text: str, path: Path, voice: str, rate: str,
                pitch: str) -> int:
    import edge_tts

    path.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    written = 0
    with path.open("wb") as handle:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
                written += len(chunk["data"])
    return written


async def every_indian_voice() -> list:
    import edge_tts
    voices = await edge_tts.list_voices()
    rows = []
    for v in sorted(voices, key=lambda x: x["ShortName"]):
        if not v["Locale"].endswith(("-IN", "-PK")):
            continue
        lang = v["Locale"].split("-")[0]
        # Devanagari for languages written in it, Roman otherwise.
        text = DEVANAGARI if lang in {"hi", "mr"} else ROMAN
        rows.append((f"all-{v['ShortName']}", v["ShortName"], text,
                     "-6%", "-4Hz"))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="every Indian voice, not just the shortlist")
    args = parser.parse_args()

    settings = Settings()
    OUT.mkdir(parents=True, exist_ok=True)

    variants = (asyncio.run(every_indian_voice()) if args.all else SHORTLIST)
    print(f"writing {len(variants)} samples to {OUT.resolve()}\n")

    for name, voice, text, rate, pitch in variants:
        target = OUT / f"{name}.mp3"
        script = "devanagari" if text is DEVANAGARI else "roman"
        try:
            written = asyncio.run(synth(text, target, voice, rate, pitch))
            if not written:
                print(f"  {name:<26} {voice:<30} NO AUDIO")
                continue
            seconds = probe_duration(target, settings.ffmpeg)
            print(f"  {name:<26} {voice:<30} {script:<10} {seconds:5.2f}s")
        except Exception as exc:
            print(f"  {name:<26} {voice:<30} FAILED {str(exc)[:40]}")

    print("\nListen to 05, 06 and 07 first — Indian-English voices reading "
          "the Roman text are the biggest departure from what you have "
          "heard so far.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

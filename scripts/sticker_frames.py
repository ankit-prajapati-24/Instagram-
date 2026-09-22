"""Render a sample video with emoji stickers and pull frames out of it.

Two jobs, both of which the test suite deliberately does not do:

  * produce frames at full 1080x1920 that a human can look at, rather than
    the 360x640 the pixel assertions in tests/test_stickers.py run at;
  * time the same plan with stickers on and off, so the render-time cost of
    the feature is a measured number and not a guess.

    python scripts/sticker_frames.py                # quick: 3 beats, stills
    python scripts/sticker_frames.py --beats 10 --seconds 5 --clips --grade

Everything lands in outputs/_stickers/.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.assembly import stickers as stk            # noqa: E402
from engine.assembly.render import probe_video, render  # noqa: E402
from engine.config import BASE_DIR, Settings           # noqa: E402
from engine.contract import Clip                       # noqa: E402
from engine.media.voice import caption_timings         # noqa: E402
from tests.factories import make_plan                  # noqa: E402

# Roman Hinglish lines carrying one trigger word each, in the register the
# channel actually writes in.
LINES = [
    "Roopkund jheel mein paanch sau kankaal mile",
    "Koi nahi jaanta ye log kaun the",
    "Raat ko wahan koi nahi jaata",
    "Sab ek hi waqt par khatam hue",
    "DNA ne kuch aur hi bataya",
    "Ye raaz aaj tak band pada hai",
    "Pahad par barf ke neeche dabe the",
    "Kisi ne unhe zinda nahi dekha",
    "Report adhoori chhod di gayi thi",
    "Tumhe kya lagta hai sach kya hai",
]


def _ffmpeg(binary: str, args: list[str]) -> None:
    subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y",
                    *args], check=True, capture_output=True)


def _still(binary: str, path: Path, index: int, w: int, h: int) -> None:
    _ffmpeg(binary, ["-f", "lavfi", "-i",
                     f"gradients=size={w}x{h}:nb_colors=3:seed={index}",
                     "-frames:v", "1", str(path)])


def _clip(binary: str, path: Path, index: int, seconds: float,
          w: int, h: int) -> None:
    _ffmpeg(binary, ["-f", "lavfi", "-i",
                     f"testsrc2=size={w}x{h}:rate=30:duration={seconds:.2f}",
                     "-pix_fmt", "yuv420p", str(path)])


def _audio(binary: str, path: Path, seconds: float) -> None:
    _ffmpeg(binary, ["-f", "lavfi", "-i",
                     f"sine=frequency=180:duration={seconds:.2f}",
                     "-ar", "48000", "-ac", "1", str(path)])


def build_plan(work: Path, settings: Settings, beats: int, seconds: float,
               clips: bool):
    plan = make_plan(beats=beats, measured=seconds)
    for index, beat in enumerate(plan.script.beats):
        beat.caption_text = LINES[index % len(LINES)]
        beat.words = caption_timings(beat.caption_text, seconds)
        still = work / f"{beat.beat_id}.png"
        _still(settings.ffmpeg, still, index, settings.width, settings.height)
        beat.image_path = str(still)
        if clips:
            # Two slots per beat, the shape the clip stage actually emits.
            for slot in range(2):
                path = work / f"{beat.beat_id}-{slot}.mp4"
                _clip(settings.ffmpeg, path, index, seconds / 2 + 0.5,
                      settings.width, settings.height)
                beat.clips.append(Clip(path=str(path), query="q",
                                       provider="pexels",
                                       duration=seconds / 2))
        audio = work / f"{beat.beat_id}.wav"
        _audio(settings.ffmpeg, audio, seconds)
        beat.audio_path = str(audio)
    return plan


def grab(binary: str, video: Path, at: float, out: Path) -> None:
    _ffmpeg(binary, ["-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
                     str(out)])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--beats", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--clips", action="store_true",
                        help="use video slots, the production shape")
    parser.add_argument("--grade", action="store_true",
                        help="render with the house grade, as production does")
    parser.add_argument("--repeat", type=int, default=2,
                        help="timed rounds; the order of the two conditions "
                             "alternates between them")
    args = parser.parse_args()

    settings = Settings()
    settings.video_grade = args.grade
    settings.stickers = True
    out_dir = Path(settings.out_dir) / "_stickers"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(settings.work_dir) / "_sticker_sample"
    work.mkdir(parents=True, exist_ok=True)
    settings.work_dir = work

    plan = build_plan(work, settings, args.beats, args.seconds, args.clips)
    cues = stk.prepare(plan, settings)
    total = sum(beat.seconds() for beat in plan.script.beats)
    print(f"{args.beats} beats, {total:.1f}s, "
          f"{settings.width}x{settings.height}, grade={args.grade}, "
          f"clips={args.clips}")
    print(f"{len(cues)} sticker(s): " + ", ".join(
        f"{c.name} on {c.word!r} @ {c.start:.2f}s" for c in cues))

    # Timing, and why the order alternates.
    #
    # The naive A/B -- render plain, then render with stickers, subtract --
    # reports nonsense here, and did: two runs gave -9.1s and -41.8s, i.e.
    # "stickers make it faster". They do not. Whichever render goes FIRST
    # is slower, by tens of seconds on a 250-second render: the twenty
    # source mp4s are cold in the OS file cache, and the CPU has not
    # settled into a sustained clock. That drift is an order of magnitude
    # bigger than what an overlay costs, so it has to be cancelled rather
    # than hoped away. Alternating the order and taking each condition's
    # best time does that.
    timings: dict[str, list[float]] = {"plain": [], "stickers": []}
    for round_index in range(max(args.repeat, 1)):
        order = (("plain", False), ("stickers", True))
        if round_index % 2:
            order = tuple(reversed(order))
        for label, on in order:
            settings.stickers = on
            target = out_dir / f"sample-{label}.mp4"
            started = time.perf_counter()
            render(plan, settings, target)
            elapsed = time.perf_counter() - started
            timings[label].append(elapsed)
            probe = probe_video(target, settings.ffmpeg)
            print(f"  round {round_index} {label:9s} {elapsed:7.1f}s render, "
                  f"{probe.get('duration', 0):.2f}s video "
                  f"(narration {total:.2f}s)")

    # Best-of, not mean: a render can only be made slower by something else
    # on the machine, so the minimum is the closest thing to the real cost.
    best = {k: min(v) for k, v in timings.items()}
    cost = best["stickers"] - best["plain"]
    print(f"  best   plain {best['plain']:7.1f}s | "
          f"stickers {best['stickers']:7.1f}s")
    print(f"  sticker cost: {cost:+.1f}s "
          f"({100 * cost / best['plain']:+.1f}%) for {len(cues)} sticker(s) "
          f"on a {total:.0f}s video")

    video = out_dir / "sample-stickers.mp4"
    for cue in cues:
        stem = f"{cue.beat_index:02d}-{cue.name}"
        grab(settings.ffmpeg, video, max(cue.start - 0.10, 0.0),
             out_dir / f"{stem}-1-before.png")
        grab(settings.ffmpeg, video, cue.start + stk.PEAK_SECONDS,
             out_dir / f"{stem}-2-overshoot.png")
        grab(settings.ffmpeg, video, cue.start + 0.45,
             out_dir / f"{stem}-3-settled.png")
        shutil.copy(cue.png, out_dir / f"{stem}-glyph.png")

    print(f"frames in {out_dir}")
    print("  relative to the repo: "
          f"{out_dir.relative_to(BASE_DIR).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

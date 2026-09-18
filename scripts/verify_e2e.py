"""End-to-end verification with a fake brain and everything else real.

Real: edge-tts synthesis, ffmpeg render, libass caption burn-in, SQLite,
      dedup, QC scorecard, cost accounting.
Fake: the LLM calls, because a clean OmniRoute install has no provider nodes
      configured and cannot serve a completion yet.

    python scripts/verify_e2e.py                 # writes into outputs/
    python scripts/verify_e2e.py --out some/dir   # writes elsewhere

Exit code is 0 only if a playable MP4 exists and QC has no hard failures.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings
from engine.fake_client import FakeOmniRoute
from engine.pipeline import PipelineEvent, plan_stage, produce_stage
from engine.store import Store

TOPIC = "Roopkund jheel ke 500 kankaal"


def make_emitter(verbose: bool):
    def emit(event: PipelineEvent) -> None:
        if event.status == "info" and not verbose:
            return
        mark = {"started": "..", "done": "OK", "failed": "!!",
                "info": "  "}.get(event.status, "  ")
        line = f"  [{mark}] {event.stage:<10} {event.detail}"
        sys.stdout.write(line[:150] + "\n")
        sys.stdout.flush()
    return emit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None,
                        help="directory for the MP4 (default: outputs/)")
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--captions", default="caption_text",
                        choices=["caption_text", "voice_text"])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--keep-db", action="store_true",
                        help="keep the verify database, so the dedup gate "
                             "sees previous runs (used to test the gate)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    out_dir = Path(args.out) if args.out else root / "outputs"
    work_dir = out_dir / "_work"

    settings = Settings()
    settings.out_dir = out_dir
    settings.work_dir = work_dir
    settings.db_path = out_dir / "verify.db"
    settings.captions_source = args.captions
    settings.ensure_dirs()

    # A verification harness must be re-runnable, and the dedup gate is doing
    # its job when it refuses the same topic twice. Start from a clean database
    # so the run under test is always the first sighting of this topic.
    if not args.keep_db:
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(settings.db_path) + suffix)
            if stale.exists():
                stale.unlink()

    store = Store(settings.db_path)
    store.init()
    client = FakeOmniRoute()
    emit = make_emitter(not args.quiet)

    print(f"ffmpeg  : {settings.ffmpeg}")
    engine = (settings.voice_engine or "piper").lower()
    print(f"voice   : {engine} "
          + (f"{settings.piper_voice} @ length_scale "
             f"{settings.piper_length_scale}" if engine == "piper"
             else f"{settings.voice} rate={settings.voice_rate}"))
    print(f"images  : {settings.image_workers} workers")
    print(f"captions: {settings.captions_source}")
    print(f"topic   : {args.topic}\n")

    print("PLAN STAGE (fake LLM)")
    plan = plan_stage(args.topic, client, store, settings, emit=emit)

    # Stand in for the human gate: pick the contradiction hook, which is the
    # variant the retention rules favour for this niche.
    chosen = next((h for h in plan.hooks if h.style == "contradiction"),
                  plan.hooks[0])
    plan.script.chosen_hook = chosen.variant_id
    plan.script.beats[0].voice_text = chosen.voice_text
    plan.script.beats[0].caption_text = chosen.caption_text
    store.save_plan(plan, status="approved")
    print(f"\nHUMAN GATE (simulated): chose {chosen.variant_id} "
          f"[{chosen.style}] {chosen.caption_text}\n")

    print("PRODUCE STAGE (real TTS, real ffmpeg)")
    result = produce_stage(plan, client, store, settings, emit=emit,
                           captions_source=settings.captions_source)

    probe = result["probe"]
    card = result["scorecard"]

    print("\n" + "=" * 62)
    print(f"video      : {result['video']}")
    print(f"size       : {probe.get('bytes', 0) / 1_048_576:.2f} MB")
    print(f"duration   : {probe.get('duration', 0):.2f}s")
    print(f"resolution : {probe.get('width')}x{probe.get('height')}")
    print(f"audio      : {'present' if probe.get('has_audio') else 'MISSING'}")
    print(f"providers  : {result['image_providers']}")
    print(f"cost       : ${result['cost_usd']:.6f}")
    print(f"qc         : {'PASS' if card['passed'] else 'FAIL'}")

    for check in card["checks"]:
        if not check["ok"]:
            print(f"   {check['kind']:<5} {check['name']:<22} "
                  f"{check['detail']}")
    print("=" * 62)

    problems = []
    if not Path(result["video"]).exists():
        problems.append("no MP4 written")
    if probe.get("bytes", 0) < 100_000:
        problems.append(f"MP4 suspiciously small: {probe.get('bytes')} bytes")
    if (probe.get("width"), probe.get("height")) != (settings.width,
                                                     settings.height):
        problems.append(f"wrong resolution: {probe.get('width')}x"
                        f"{probe.get('height')}")
    if not probe.get("has_audio"):
        problems.append("no audio stream")
    if not card["passed"]:
        problems.append(f"QC hard failures: {card['hard_failures']}")

    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

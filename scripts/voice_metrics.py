"""Measure the things that correlate with sounding mechanical.

This does NOT decide which voice sounds human — no measurement does, and the
ear is the only judge. What it can do is show *why* one sample reads as
robotic, and confirm that a knob did what it claimed.

Three numbers matter:

  pitch spread   Standard deviation of F0 across voiced frames, in semitones.
                 Flat pitch is the clearest signature of synthesis. Natural
                 expressive narration sits around 2.5-4 semitones; under
                 ~1.5 tends to read as monotone.

  rhythm spread  Coefficient of variation of voiced-run lengths. A fixed
                 per-syllable duration is what makes speech tick like a
                 metronome. Higher is more human, to a point.

  pause count    Silences over 150ms. Narration breathes; default TTS does
                 not, and a reading with no pauses is exhausting regardless
                 of how good the voice is.

    python scripts/voice_metrics.py work/shootout work/pipertune
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402

SR = 16000
FRAME = 512
F0_MIN, F0_MAX = 70, 320          # a male/female speech range


def decode(path: Path, ffmpeg: str) -> np.ndarray:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True)
    audio = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
    return audio / 32768.0


def frame_f0(frame: np.ndarray) -> float:
    """Autocorrelation pitch estimate for one frame, 0.0 if unvoiced."""
    frame = frame - frame.mean()
    if np.sqrt((frame ** 2).mean()) < 0.01:
        return 0.0
    corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
    lo, hi = SR // F0_MAX, min(SR // F0_MIN, len(corr) - 1)
    if hi <= lo:
        return 0.0
    segment = corr[lo:hi]
    peak = int(np.argmax(segment)) + lo
    # Require the peak to be a real one, not noise.
    if corr[0] <= 0 or corr[peak] / corr[0] < 0.3:
        return 0.0
    return SR / peak


def analyse(path: Path, ffmpeg: str) -> dict:
    audio = decode(path, ffmpeg)
    frames = [audio[i:i + FRAME] for i in range(0, len(audio) - FRAME, FRAME)]
    if not frames:
        return {}

    energy = np.array([np.sqrt((f ** 2).mean()) for f in frames])
    f0 = np.array([frame_f0(f) for f in frames])
    voiced = f0 > 0

    # Pitch spread in semitones, which is how the ear scales pitch.
    if voiced.sum() > 8:
        semitones = 12 * np.log2(f0[voiced] / np.median(f0[voiced]))
        pitch_spread = float(np.std(semitones))
        pitch_range = float(np.percentile(semitones, 95)
                            - np.percentile(semitones, 5))
    else:
        pitch_spread = pitch_range = 0.0

    # Rhythm: how much the lengths of voiced runs vary.
    runs, current = [], 0
    for value in voiced:
        if value:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    runs_arr = np.array(runs, dtype=float)
    rhythm = float(runs_arr.std() / runs_arr.mean()) if len(runs) > 3 else 0.0

    # Pauses: quiet stretches longer than 150ms.
    quiet = energy < max(energy.mean() * 0.12, 1e-4)
    pauses, current = 0, 0
    min_frames = int(0.150 * SR / FRAME)
    for value in quiet:
        if value:
            current += 1
        else:
            if current >= min_frames:
                pauses += 1
            current = 0
    if current >= min_frames:
        pauses += 1

    seconds = len(audio) / SR
    loud = energy[energy > energy.mean() * 0.2]
    dynamics = float(20 * np.log10(loud.max() / max(loud.mean(), 1e-6))) \
        if len(loud) else 0.0

    return {
        "seconds": seconds,
        "pitch_spread": pitch_spread,
        "pitch_range": pitch_range,
        "rhythm": rhythm,
        "pauses": pauses,
        "pauses_per_min": pauses / (seconds / 60) if seconds else 0,
        "dynamics": dynamics,
    }


def verdict(m: dict) -> str:
    notes = []
    if m["pitch_spread"] < 1.5:
        notes.append("monotone")
    elif m["pitch_spread"] > 3.2:
        notes.append("expressive")
    if m["rhythm"] < 0.55:
        notes.append("even rhythm")
    elif m["rhythm"] > 0.95:
        notes.append("loose rhythm")
    if m["pauses_per_min"] < 8:
        notes.append("few pauses")
    return ", ".join(notes) or "mid-range"


def main() -> int:
    settings = Settings()
    roots = [Path(a) for a in sys.argv[1:]] or [Path("work/shootout")]

    files = []
    for root in roots:
        files.extend(sorted(p for p in root.glob("*.mp3")))
    if not files:
        print("no mp3s found in:", ", ".join(str(r) for r in roots))
        return 1

    print(f"{'file':<36} {'dur':>6} {'pitch':>6} {'range':>6} {'rhythm':>7} "
          f"{'pause/min':>10}  notes")
    print("-" * 100)
    for path in files:
        try:
            m = analyse(path, settings.ffmpeg)
            if not m:
                continue
            print(f"{path.parent.name + '/' + path.stem:<36} "
                  f"{m['seconds']:>5.1f}s {m['pitch_spread']:>6.2f} "
                  f"{m['pitch_range']:>6.2f} {m['rhythm']:>7.2f} "
                  f"{m['pauses_per_min']:>10.1f}  {verdict(m)}")
        except Exception as exc:
            print(f"{path.stem:<36} failed: {str(exc)[:40]}")

    print("\npitch  = F0 spread in semitones (higher = less monotone)")
    print("rhythm = variation in syllable lengths (higher = less metronomic)")
    print("\nThese are proxies, not a verdict. They explain what you hear; "
          "they do not replace hearing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

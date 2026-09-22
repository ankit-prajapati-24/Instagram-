"""Generate the placeholder music bed and sound effects with ffmpeg.

Read this before you judge what it produces: **these are placeholders, and
they sound cheap.** They are four synthesised tones, not sound design. They
exist so that a clean clone renders a video with a real music bed, real
ducking and real sound effects on the first run, instead of shipping a
half-built feature that nobody can hear until somebody sources audio.

Replacing them takes no code change. Drop any audio file into
``assets/music/`` and it becomes the bed; drop ``whoosh.wav``, ``pop.wav``
or ``subdrop.wav`` (or ``whoosh-anything.mp3``) into ``assets/sfx/`` and it
becomes that sound. Discovery prefers any file that is not named
``placeholder-``; see ``engine/assembly/audio.py`` and ``SETUP.md``. The
renderer measures whatever it finds and normalises it, so a real track does
not need the levels retuned either.

Why generate instead of committing the files
--------------------------------------------
A 60-second bed is ~5MB of PCM. Committing it puts a binary nobody can diff
into every clone forever, and ``.gitignore`` already refuses ``*.wav`` and
``*.mp3`` for exactly that reason. Thirty lines of ffmpeg reproduce it in
under two seconds.

Run it with::

    python scripts/make_audio_assets.py

It skips files that already exist, so it will never overwrite a real track
you dropped in. ``--force`` regenerates the placeholders anyway.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                  # running it as a script
    sys.path.insert(0, str(ROOT))

# --- the bed ----------------------------------------------------------------
#
# Four low sines a fifth and an octave apart, under a very slow tremolo,
# rolled off hard and given a long echo so it reads as a room rather than as
# an oscillator. Sixty seconds, with every frequency an exact multiple of
# 1/60 Hz (55, 82.5, 110, 165) and the tremolo at 0.05 Hz -- three whole
# cycles -- so that when `aloop` in the render graph wraps it there is no
# discontinuity at the seam. The 2.5s fade is at the head only, for the same
# reason: a fade at the tail would be a dip every time it looped.
DRONE_EXPR = (
    "(0.30*sin(2*PI*55*t)"
    "+0.18*sin(2*PI*82.5*t)"
    "+0.12*sin(2*PI*110*t)"
    "+0.05*sin(2*PI*165*t))"
    "*(0.75+0.25*sin(2*PI*0.05*t))")
DRONE_SECONDS = 60.0

# --- the sounds -------------------------------------------------------------
#
# A whoosh is filtered noise with a swell in the middle. The flanger is
# doing the work a frequency sweep would do in a real one: ffmpeg cannot
# sweep a filter's cutoff over time, and a comb whose delay moves gives the
# same "something passed" impression for a placeholder.
WHOOSH_SECONDS = 0.9
WHOOSH_CHAIN = (
    "flanger=delay=12:depth=8:speed=1.2,"
    "highpass=f=250,lowpass=f=7000,"
    # A squared sine over the whole length: in and out with no corner.
    f"volume='pow(sin(PI*min(t/{WHOOSH_SECONDS},1)),2)':eval=frame,"
    "afade=t=out:st=0.80:d=0.10")

# A pop is two damped sines an octave apart, the upper one decaying faster:
# that ratio is most of what separates a "pop" from a "beep".
POP_EXPR = ("0.80*exp(-30*t)*sin(2*PI*1200*t)"
            "+0.40*exp(-55*t)*sin(2*PI*2400*t)")
POP_SECONDS = 0.28

# A sub-drop is one sine falling from 90Hz to 30Hz. The phase is the
# integral of the frequency ramp -- 90t - 25t^2 -- not `sin(2*PI*f(t)*t)`,
# which sweeps the instantaneous frequency down through zero and back up
# and sounds like a fault rather than a drop.
SUBDROP_EXPR = "0.95*exp(-1.6*t)*sin(2*PI*(90*t-25*t*t))"
SUBDROP_SECONDS = 1.6

MUSIC_NAME = "placeholder-drone.wav"


def _run(ffmpeg: str, args: list[str]) -> None:
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error",
                             "-nostdin", "-y", *args],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + (result.stderr or "")[-2000:])


def _lavfi(ffmpeg: str, source: str, chain: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(ffmpeg, ["-f", "lavfi", "-i", source, "-af", chain,
                  "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return out


def build_music(ffmpeg: str, music_dir: Path, force: bool = False) -> Path:
    """The looping ambient bed."""
    out = Path(music_dir) / MUSIC_NAME
    if out.exists() and not force:
        return out
    return _lavfi(
        ffmpeg,
        # A couple of seconds longer than the trim, so the echo tail is
        # inside the file rather than being what the file ends on.
        f"aevalsrc='{DRONE_EXPR}':s=48000:d={DRONE_SECONDS + 2:g}",
        ("lowpass=f=900,"
         "aecho=0.8:0.85:250|420:0.35|0.22,"
         "afade=t=in:d=2.5,"
         f"atrim=0:{DRONE_SECONDS:g},"
         "volume=0.8"),
        out)


def build_sfx(ffmpeg: str, sfx_dir: Path, force: bool = False) -> list[Path]:
    """The three one-shots, in the order the brief names them."""
    sfx_dir = Path(sfx_dir)
    made: list[Path] = []

    specs = [
        ("placeholder-whoosh.wav",
         f"anoisesrc=c=pink:r=48000:d={WHOOSH_SECONDS:g}:a=0.7:s=42",
         WHOOSH_CHAIN),
        ("placeholder-pop.wav",
         f"aevalsrc='{POP_EXPR}':s=48000:d={POP_SECONDS:g}",
         "afade=t=in:d=0.002,lowpass=f=9000"),
        ("placeholder-subdrop.wav",
         f"aevalsrc='{SUBDROP_EXPR}':s=48000:d={SUBDROP_SECONDS:g}",
         "afade=t=in:d=0.005,afade=t=out:st=1.20:d=0.40,lowpass=f=180"),
    ]
    for name, source, chain in specs:
        out = sfx_dir / name
        if out.exists() and not force:
            made.append(out)
            continue
        made.append(_lavfi(ffmpeg, source, chain, out))
    return made


def build_all(ffmpeg: str, music_dir, sfx_dir, force: bool = False
              ) -> list[Path]:
    """Everything, bed first. Returns the paths that now exist."""
    return [build_music(ffmpeg, Path(music_dir), force),
            *build_sfx(ffmpeg, Path(sfx_dir), force)]


def main(argv: list[str] | None = None) -> int:
    from engine.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music-dir", default=str(settings.music_dir))
    parser.add_argument("--sfx-dir", default=str(settings.sfx_dir))
    parser.add_argument("--ffmpeg", default=settings.ffmpeg)
    parser.add_argument("--force", action="store_true",
                        help="regenerate even if the file already exists")
    args = parser.parse_args(argv)

    made = build_all(args.ffmpeg, args.music_dir, args.sfx_dir, args.force)
    for path in made:
        size = Path(path).stat().st_size
        print(f"  {path}  ({size // 1024} KB)")
    print("\nThese are placeholders and they sound like it. Drop a real "
          "file\nnext to one and it wins -- no code change; see SETUP.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

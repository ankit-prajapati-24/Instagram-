"""The music bed and the sound-effects layer.

Why this lives beside ``stickers.py``
-------------------------------------
Same reason stickers do. Nothing here acquires material from outside the
plan: the bed and the sounds are files that are already on disk, and what
this module produces is a filtergraph fragment plus a list of ffmpeg inputs
that only the renderer consumes. ``engine/media/`` is for the stages that go
and fetch something; this is assembly.

The three tracks
----------------
1. the mastered voiceover -- ``engine/media/piper_voice.py`` already
   loudnorms every beat to I=-15, which is what makes everything below a
   fixed number rather than a guess,
2. this bed, normalised to ``MUSIC_LUFS`` and then ducked under the voice by
   a sidechain compressor keyed on the narration itself,
3. these sounds: a whoosh on a cut that matters, a pop on each sticker, and
   one sub-bass hit under the hook.

Ducking, and why it is not a gain
---------------------------------
The brief asks for music "roughly 18dB down while the voice is present,
rising in the pauses". The second half of that sentence is the whole
feature, and a static ``volume=-18dB`` cannot do it: it is down by the same
amount whether anyone is speaking or not, so the gaps between beats stay as
empty as they were before the bed existed.

``sidechaincompress`` is the filter that can. The narration drives the
compressor and the bed is what gets compressed, so the bed's level is a
function of whether there is speech right now. The settings in
``DUCK_*`` below were measured, not guessed -- see the module test and the
task report -- against a real 49.5s Piper narration and a real bed:

* ``threshold=0.1`` is -20 dBFS, comfortably under the -15 LUFS every beat
  is normalised to and comfortably over the noise floor between them, so
  the compressor tracks speech rather than tracking level.
* ``ratio=4`` with that threshold measured a median 7.1 dB and a 90th
  percentile 9.2 dB of gain reduction while the voice was speaking. With
  the bed already sitting 10 LU under the voice, that puts the music
  17-19 dB down under speech: the "roughly 18dB" the brief asks for,
  arrived at rather than asserted.
* ``release=300`` is the number that decides whether it ducks or pumps.
  Measured over the same narration: in the real gaps between beats the bed
  came back up to within 0.0 dB (median) of its undicked level, while the
  50-150ms gaps *between words* inside a sentence never recover -- so the
  bed stays down through a line and lifts between them. At 200ms it
  recovered inside sentences too, which is audible pumping; at 450ms it
  was still 1.8 dB down in the real gaps, which is the lift the brief
  asked for going missing.
* ``attack=20`` is fast enough that the duck is already there under the
  first syllable, and a drone has no transient for a slower attack to
  protect.

Levels, and why a dropped-in file needs no code change
------------------------------------------------------
``assets/music/`` and ``assets/sfx/`` ship generated placeholders (see
``scripts/make_audio_assets.py``). Dropping a real file next to one
replaces it: discovery prefers any file that is not named ``placeholder-``.

That only works if the level of the new file does not matter, so it is
measured rather than assumed. The bed is normalised to ``MUSIC_LUFS`` from
its own integrated loudness, and each sound to a peak target from its own
measured peak -- one short ffmpeg pass per file, cached. A track mastered
at -8 LUFS and one at -30 therefore both arrive at the mix at the same
place. If the measurement fails for any reason the fixed
``FALLBACK_MUSIC_GAIN_DB`` is used, which is exactly what this renderer did
before ducking existed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from engine.contract import ReelPlan

# --- discovery --------------------------------------------------------------

AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
                  ".flac", ".wma"}
# The one naming convention in this feature. Everything the generator writes
# carries it and nothing else does, so "prefer what is not a placeholder" is
# the entire override rule.
PLACEHOLDER_PREFIX = "placeholder-"

WHOOSH = "whoosh"
POP = "pop"
SUBDROP = "subdrop"
SFX_KINDS = (WHOOSH, POP, SUBDROP)

# --- levels -----------------------------------------------------------------

# Where the bed sits before it is ducked, as an absolute integrated
# loudness. 10 LU under the -15 LUFS that piper_voice normalises every beat
# to: clearly present in a pause, never competing with a word.
MUSIC_LUFS = -25.0
# What `volume=` gets when the bed cannot be measured. The value this
# renderer used before ducking existed, so an unmeasurable file degrades to
# the old behaviour rather than to silence or to a wall of music.
FALLBACK_MUSIC_GAIN_DB = -18.0

# One-shots are too short for integrated loudness to mean anything -- the
# EBU gate throws away most of a 0.2s pop -- so they are matched on peak
# instead, per kind. The sub-drop is allowed to be hotter because almost
# all of its energy is under 90Hz, where it reads far quieter than the
# number suggests.
SFX_PEAK_DB = {WHOOSH: -16.0, POP: -14.0, SUBDROP: -10.0}
FALLBACK_SFX_GAIN_DB = -20.0

# --- ducking ----------------------------------------------------------------

# The bed and the sounds are whatever files somebody dropped into
# assets/, at whatever sample rate they were made at, and `adelay` counts
# in milliseconds of the rate it is handed -- so the rate is declared here
# rather than left to whatever resampler `amix` decides to insert.
#
# Deliberately NOT forcing a channel layout. Measured on this ffmpeg: a
# mono narration upmixed to stereo before the sidechain arrives ~3 dB
# quieter at the detector, which costs 2.2 dB of the duck -- a third of
# it -- for nothing. A stereo bed against a mono key was checked and
# `sidechaincompress` configures and runs fine, so there was nothing to
# fix in the first place.
MIX_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000"

DUCK_THRESHOLD = 0.1      # linear, ~-20 dBFS
DUCK_RATIO = 4
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 300

# --- the SFX policy ---------------------------------------------------------

# The beats the picture already treats as a turn in the story. This is
# `render.PUSH_ROLES` minus "hook": the hook is beat 0 and has no cut coming
# into it, and it gets the sub-bass hit instead. Kept as its own constant
# rather than imported, because render imports this module and not the other
# way round.
TURN_ROLES = frozenset({"reveal", "twist"})
# Three, the same number and the same reason as `stickers.DEFAULT_CAP`: ten
# beats is nine cuts, and a whoosh on all nine stops meaning "something
# changed" and becomes the texture of the video.
DEFAULT_WHOOSH_CAP = 3
# Never two in a row. Longer than the sticker gap, because a whoosh is a
# second and a half of broadband noise and two of them inside four seconds
# is the thing that makes an edit feel frantic.
MIN_WHOOSH_GAP = 4.0
# A whoosh swells into the cut rather than starting on it, the way an editor
# places one by hand.
WHOOSH_LEAD = 0.25


@dataclass(frozen=True)
class SfxCue:
    """One sound, its file, and where it lands on the finished timeline."""

    kind: str
    path: str
    start: float
    gain_db: float


# --- finding the files ------------------------------------------------------

def _audio_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]


def _is_placeholder(path: Path) -> bool:
    return path.stem.lower().startswith(PLACEHOLDER_PREFIX)


def _prefer_real(candidates: list[Path]) -> str | None:
    """A real file beats a placeholder; otherwise alphabetical order."""
    if not candidates:
        return None
    candidates.sort(key=lambda p: (_is_placeholder(p), p.name.lower()))
    return str(candidates[0])


def find_music(settings) -> str | None:
    """The background bed, or None when there is none or music is off."""
    if not getattr(settings, "music", False):
        return None
    directory = Path(getattr(settings, "music_dir", "") or ".")
    return _prefer_real(_audio_files(directory))


def find_sfx(kind: str, settings) -> str | None:
    """The file for one sound kind, by the documented naming rule.

    A file belongs to ``kind`` when its stem -- with any ``placeholder-``
    prefix removed -- is the kind's name, or starts with the kind's name
    and a hyphen. So ``whoosh.wav``, ``whoosh-metal-01.aiff`` and
    ``placeholder-whoosh.wav`` are all whooshes, and the first two win.
    """
    if not getattr(settings, "sfx", False):
        return None
    directory = Path(getattr(settings, "sfx_dir", "") or ".")
    matches = []
    for path in _audio_files(directory):
        stem = path.stem.lower()
        if stem.startswith(PLACEHOLDER_PREFIX):
            stem = stem[len(PLACEHOLDER_PREFIX):]
        if stem == kind or stem.startswith(f"{kind}-"):
            matches.append(path)
    return _prefer_real(matches)


# --- measuring what was dropped in ------------------------------------------

_MEASURED: dict[tuple, float | None] = {}


def _cache_key(path: str, what: str) -> tuple:
    try:
        stat = Path(path).stat()
        return (what, str(Path(path).resolve()), stat.st_size,
                stat.st_mtime_ns)
    except OSError:
        return (what, str(path), -1, -1)


def _probe(ffmpeg: str, path: str, chain: str, pattern: str) -> float | None:
    if not ffmpeg:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
             "-af", chain, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.findall(pattern, result.stderr or "")
    if not found:
        return None
    try:
        return float(found[-1])
    except ValueError:                        # pragma: no cover - ffmpeg odd
        return None


def measure_lufs(path: str, ffmpeg: str) -> float | None:
    """Integrated loudness of a file, or None if it cannot be read."""
    key = _cache_key(path, "lufs")
    if key not in _MEASURED:
        _MEASURED[key] = _probe(ffmpeg, path, "ebur128=framelog=quiet",
                                r"I:\s*(-?\d+\.?\d*) LUFS")
    return _MEASURED[key]


def measure_peak_db(path: str, ffmpeg: str) -> float | None:
    """Sample peak of a file in dBFS, or None if it cannot be read."""
    key = _cache_key(path, "peak")
    if key not in _MEASURED:
        _MEASURED[key] = _probe(ffmpeg, path, "volumedetect",
                                r"max_volume:\s*(-?\d+\.?\d*) dB")
    return _MEASURED[key]


def _clamp(value: float, low: float = -40.0, high: float = 20.0) -> float:
    return max(low, min(high, value))


def music_gain_db(path: str, settings) -> float:
    """The gain that puts ``path`` at ``settings.music_lufs``.

    Measured, so a track mastered at -8 LUFS and one at -30 both arrive at
    the mix in the same place and neither needs a code change or a knob.
    """
    target = float(getattr(settings, "music_lufs", MUSIC_LUFS))
    measured = measure_lufs(path, getattr(settings, "ffmpeg", ""))
    if measured is None:
        return FALLBACK_MUSIC_GAIN_DB
    return round(_clamp(target - measured), 1)


def sfx_gain_db(kind: str, path: str, settings) -> float:
    """The gain that puts one sound at its kind's peak target."""
    trim = float(getattr(settings, "sfx_gain_db", 0.0))
    measured = measure_peak_db(path, getattr(settings, "ffmpeg", ""))
    if measured is None:
        return round(_clamp(FALLBACK_SFX_GAIN_DB + trim), 1)
    target = SFX_PEAK_DB.get(kind, -16.0)
    return round(_clamp(target - measured + trim), 1)


# --- picking the moments ----------------------------------------------------

def beat_starts(plan: ReelPlan) -> list[float]:
    """Absolute start of each beat on the narration timeline.

    The same running sum ``render.segment_lengths`` calls ``cuts``: the
    narration is a plain concat, so beat i starts at the sum of the beats
    before it, and a cut into beat i happens at exactly that time. Derived
    from the plan rather than passed in, so a sound can never be placed
    against a timeline the picture is not on.
    """
    starts: list[float] = []
    running = 0.0
    for beat in plan.script.beats:
        starts.append(running)
        running += beat.seconds()
    return starts


def whoosh_times(plan: ReelPlan, cap: int = DEFAULT_WHOOSH_CAP,
                 min_gap: float = MIN_WHOOSH_GAP) -> list[float]:
    """When a cut whoosh fires, in timeline order.

    Only cuts into a ``TURN_ROLES`` beat, then capped and spaced. Earliest
    first rather than "strongest first" the way stickers choose, because
    every candidate here is the same strength -- the ranking that matters
    already happened when the script gave the beat its role.
    """
    if cap <= 0:
        return []
    starts = beat_starts(plan)
    chosen: list[float] = []
    for index, beat in enumerate(plan.script.beats):
        if index == 0 or beat.role not in TURN_ROLES:
            continue
        at = starts[index] - WHOOSH_LEAD
        if at < 0:
            continue
        if chosen and at - chosen[-1] < min_gap:
            continue
        chosen.append(at)
        if len(chosen) >= cap:
            break
    return chosen


def plan_sfx(plan: ReelPlan, settings, stickers: list | None = None
             ) -> list[SfxCue]:
    """Every sound this plan should get, in timeline order.

    Empty when SFX are off, and empty for any kind whose file is missing:
    a sound is decoration, and decoration must never cost a render.
    """
    if not getattr(settings, "sfx", False):
        return []
    total = sum(beat.seconds() for beat in plan.script.beats)
    if total <= 0:
        return []

    cues: list[SfxCue] = []

    def add(kind: str, start: float) -> None:
        if not (0.0 <= start < total):
            return
        path = find_sfx(kind, settings)
        if not path:
            return
        cues.append(SfxCue(kind=kind, path=path, start=round(start, 3),
                           gain_db=sfx_gain_db(kind, path, settings)))

    # One sub-bass hit, under the hook. It is the first sound in the video
    # and the only one that is not marking a change.
    beats = plan.script.beats
    if beats and beats[0].role == "hook":
        add(SUBDROP, 0.0)

    for at in whoosh_times(plan, int(getattr(settings, "sfx_whoosh_max",
                                             DEFAULT_WHOOSH_CAP))):
        add(WHOOSH, at)

    # A pop per sticker, on the sticker's own time. The brief pairs the two,
    # and taking the times from the stickers rather than recomputing them is
    # what guarantees the sound and the picture cannot drift apart.
    for sticker in (stickers or []):
        add(POP, float(sticker.start))

    return sorted(cues, key=lambda c: (c.start, c.kind))


# --- the graph --------------------------------------------------------------

def sfx_inputs(cues: list[SfxCue]) -> list[str]:
    """ffmpeg input arguments: one plain input per cue.

    One per *cue*, not one per file, even when the same whoosh fires three
    times. Decoding a one-second file three times is free at this scale,
    and the alternative -- one input fanned out with ``asplit`` -- makes
    the index arithmetic depend on how many cues happened to share a file,
    which is precisely the kind of arithmetic this renderer has been bitten
    by before.
    """
    args: list[str] = []
    for cue in cues:
        args += ["-i", str(Path(cue.path).resolve())]
    return args


def duck_filter() -> str:
    """The sidechain compressor, with the measured settings spelled out."""
    return (f"sidechaincompress=threshold={DUCK_THRESHOLD}:"
            f"ratio={DUCK_RATIO}:attack={DUCK_ATTACK_MS}:"
            f"release={DUCK_RELEASE_MS}:makeup=1:level_sc=1")


def sfx_chain(cues: list[SfxCue], first_index: int) -> tuple[list[str],
                                                             list[str]]:
    """The filtergraph fragments that place each sound, and their labels.

    ``adelay`` moves the sound onto its cue. Deliberately not
    ``-itsoffset`` on the input, for the same reason the sticker chain does
    not use it: the offset has to be visible in the graph, next to the time
    it came from, or nobody can check it against the plan.

    ``aformat`` is here so the mix is not at the mercy of whatever sample
    rate a dropped-in file happens to carry -- the alternative is ffmpeg
    inserting a resampler wherever it likes and the delays landing a few
    milliseconds off.
    """
    parts: list[str] = []
    labels: list[str] = []
    for offset, cue in enumerate(cues):
        label = f"sfx{offset}"
        delay_ms = int(round(cue.start * 1000))
        parts.append(
            f"[{first_index + offset}:a]{MIX_FORMAT},"
            f"volume={cue.gain_db}dB,"
            f"adelay=delays={delay_ms}:all=1[{label}]")
        labels.append(label)
    return parts, labels

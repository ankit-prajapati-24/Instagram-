"""The three audio tracks: voice, a ducked music bed, and the SFX layer.

Most of this file is ordinary unit work on discovery and cue placement. The
part that matters is at the bottom, and it runs ffmpeg.

A filtergraph is a string until ffmpeg accepts it, and for *audio* a string
assertion is weaker still: a graph can be syntactically perfect, wire the
sidechain to the wrong stream, and come out silent or undicked while every
`assert "sidechaincompress" in graph` still passes. So the ducking claim is
asserted on measured decibels in a rendered MP4 -- the music's own band,
sampled while the narration speaks and again in a gap between beats -- and
the loudness claim on what `ebur128` reads back off the finished file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from engine.assembly import audio
from engine.assembly.render import build_filter_graph, plan_inputs
from engine.media.voice import caption_timings
from tests.factories import make_plan


# --- helpers ----------------------------------------------------------------

def _timed_plan(beats=3, measured=4.0):
    plan = make_plan(beats=beats, measured=measured)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, measured)
    return plan


class _Stub:
    """A settings stand-in: only the attributes the audio layer reads."""

    def __init__(self, **kwargs):
        self.music = True
        self.music_duck = True
        self.music_lufs = audio.MUSIC_LUFS
        self.sfx = True
        self.sfx_whoosh_max = audio.DEFAULT_WHOOSH_CAP
        self.sfx_gain_db = 0.0
        self.music_dir = Path(".")
        self.sfx_dir = Path(".")
        self.ffmpeg = ""
        self.__dict__.update(kwargs)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    return path


# --- discovery: a real file must beat the placeholder with no code change ---

def test_a_real_track_wins_over_the_shipped_placeholder(tmp_path):
    """The whole point of the placeholder: dropping a file replaces it.

    The generated bed is named with a `placeholder-` prefix and nothing
    else is, so "prefer anything that is not a placeholder" is the entire
    override rule -- no filename to match, no config to edit.
    """
    _touch(tmp_path / "placeholder-drone.wav")
    _touch(tmp_path / "my-real-bed.mp3")
    assert Path(audio.find_music(_Stub(music_dir=tmp_path))).name \
        == "my-real-bed.mp3"


def test_the_placeholder_is_used_when_nothing_else_is_there(tmp_path):
    _touch(tmp_path / "placeholder-drone.wav")
    assert Path(audio.find_music(_Stub(music_dir=tmp_path))).name \
        == "placeholder-drone.wav"


def test_no_music_directory_is_not_an_error(tmp_path):
    assert audio.find_music(_Stub(music_dir=tmp_path / "nope")) is None


def test_non_audio_files_are_ignored(tmp_path):
    _touch(tmp_path / "README.md")
    _touch(tmp_path / "cover.png")
    assert audio.find_music(_Stub(music_dir=tmp_path)) is None


def test_the_music_switch_turns_discovery_off(tmp_path):
    _touch(tmp_path / "placeholder-drone.wav")
    assert audio.find_music(_Stub(music_dir=tmp_path, music=False)) is None


def test_sfx_are_found_by_kind_and_a_real_file_still_wins(tmp_path):
    _touch(tmp_path / "placeholder-whoosh.wav")
    _touch(tmp_path / "placeholder-pop.wav")
    _touch(tmp_path / "whoosh-metal-01.wav")
    stub = _Stub(sfx_dir=tmp_path)
    assert Path(audio.find_sfx(audio.WHOOSH, stub)).name == "whoosh-metal-01.wav"
    assert Path(audio.find_sfx(audio.POP, stub)).name == "placeholder-pop.wav"
    assert audio.find_sfx(audio.SUBDROP, stub) is None


def test_the_sfx_switch_turns_discovery_off(tmp_path):
    _touch(tmp_path / "placeholder-pop.wav")
    assert audio.find_sfx(audio.POP, _Stub(sfx_dir=tmp_path, sfx=False)) is None


# --- the SFX policy ---------------------------------------------------------

def test_a_sub_bass_hit_lands_on_the_hook(tmp_path):
    _touch(tmp_path / "placeholder-subdrop.wav")
    cues = audio.plan_sfx(_timed_plan(beats=4), _Stub(sfx_dir=tmp_path))
    drops = [c for c in cues if c.kind == audio.SUBDROP]
    assert len(drops) == 1
    assert drops[0].start == pytest.approx(0.0)


def test_a_whoosh_marks_a_turn_not_every_cut(tmp_path):
    """Sparse on purpose, the same reason stickers are capped at three.

    Ten beats is nine cuts. A whoosh on all nine is the audio equivalent of
    a sticker on every word: it stops signifying "something changed" and
    becomes the texture of the video. Only the beats the picture already
    treats as a turn -- the roles `render.PUSH_ROLES` pushes on -- get one.
    """
    _touch(tmp_path / "placeholder-whoosh.wav")
    plan = _timed_plan(beats=10, measured=4.0)
    roles = [b.role for b in plan.script.beats]
    turns = [i for i, r in enumerate(roles) if r in audio.TURN_ROLES and i]
    assert len(turns) > audio.DEFAULT_WHOOSH_CAP, \
        "fixture must offer more turns than the cap, or this proves nothing"

    cues = audio.plan_sfx(plan, _Stub(sfx_dir=tmp_path))
    whooshes = [c for c in cues if c.kind == audio.WHOOSH]
    assert len(whooshes) == audio.DEFAULT_WHOOSH_CAP
    # Every one of them sits on a cut into a turn beat, lead-in allowed for.
    cuts = {}
    running = 0.0
    for index, beat in enumerate(plan.script.beats):
        cuts[index] = running
        running += beat.seconds()
    for cue in whooshes:
        landing = cue.start + audio.WHOOSH_LEAD
        assert any(abs(landing - cuts[i]) < 1e-6 for i in turns), \
            f"whoosh at {cue.start} is not on a turn"


def test_the_whoosh_cap_is_configurable(tmp_path):
    _touch(tmp_path / "placeholder-whoosh.wav")
    plan = _timed_plan(beats=10, measured=4.0)
    cues = audio.plan_sfx(plan, _Stub(sfx_dir=tmp_path, sfx_whoosh_max=1))
    assert len([c for c in cues if c.kind == audio.WHOOSH]) == 1
    cues = audio.plan_sfx(plan, _Stub(sfx_dir=tmp_path, sfx_whoosh_max=0))
    assert not [c for c in cues if c.kind == audio.WHOOSH]


def test_a_pop_lands_on_every_sticker(tmp_path):
    """The spec pairs a pop with each sticker, so the times come from the
    stickers themselves rather than being computed a second time."""
    from engine.assembly.stickers import Sticker
    _touch(tmp_path / "placeholder-pop.wav")
    made = [Sticker(name="warning", emoji="!", word="w", beat_index=1,
                    start=5.5, slot=0, size=10, canvas=12, frames=7,
                    pattern="p-%03d.png", png="p-006.png"),
            Sticker(name="eye", emoji="!", word="w", beat_index=3,
                    start=13.25, slot=1, size=10, canvas=12, frames=7,
                    pattern="q-%03d.png", png="q-006.png")]
    cues = audio.plan_sfx(_timed_plan(beats=6), _Stub(sfx_dir=tmp_path),
                          stickers=made)
    pops = [c for c in cues if c.kind == audio.POP]
    assert [c.start for c in pops] == [5.5, 13.25]


def test_cues_are_in_timeline_order_and_never_past_the_end(tmp_path):
    from engine.assembly.stickers import Sticker
    for kind in (audio.WHOOSH, audio.POP, audio.SUBDROP):
        _touch(tmp_path / f"placeholder-{kind}.wav")
    plan = _timed_plan(beats=6, measured=2.0)          # 12.0s total
    late = Sticker(name="x", emoji="!", word="w", beat_index=5, start=99.0,
                   slot=0, size=10, canvas=12, frames=7, pattern="p-%03d.png",
                   png="p-006.png")
    cues = audio.plan_sfx(plan, _Stub(sfx_dir=tmp_path), stickers=[late])
    assert [c.start for c in cues] == sorted(c.start for c in cues)
    assert all(0.0 <= c.start < 12.0 for c in cues)


def test_missing_sfx_files_simply_drop_their_cues(tmp_path):
    """A sound is decoration. A missing file must never cost a render."""
    _touch(tmp_path / "placeholder-whoosh.wav")
    cues = audio.plan_sfx(_timed_plan(beats=10), _Stub(sfx_dir=tmp_path))
    assert cues and all(c.kind == audio.WHOOSH for c in cues)


# --- the graph --------------------------------------------------------------

def test_ducking_is_a_sidechain_keyed_on_the_narration():
    """Not a static gain. A flat -18dB cannot rise in the pauses."""
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2,
                                     music_index=4)
    assert "sidechaincompress=" in graph
    # The narration has to reach both the mix and the compressor's key input.
    assert "asplit" in graph
    chain = [p for p in graph.split(";") if "sidechaincompress" in p][0]
    assert chain.startswith("[bed0][duckkey]"), chain
    assert f"threshold={audio.DUCK_THRESHOLD}" in chain
    assert f"ratio={audio.DUCK_RATIO}" in chain
    assert f"release={audio.DUCK_RELEASE_MS}" in chain


def test_ducking_can_be_switched_off_without_losing_the_bed():
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2,
                                     music_index=4, duck=False)
    assert "sidechaincompress" not in graph
    assert "volume=-18.0dB" in graph
    assert "amix=inputs=2" in graph


def test_the_mix_never_renormalises_when_a_sound_ends():
    """amix's default `normalize=1` divides by the live input count, so a
    half-second whoosh ending would step every other track up. Off."""
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2,
                                     music_index=4)
    assert "normalize=0" in graph


def test_sfx_are_delayed_onto_their_cue_and_mixed_in():
    cues = [audio.SfxCue(kind=audio.WHOOSH, path="w.wav", start=3.25,
                         gain_db=-6.0),
            audio.SfxCue(kind=audio.SUBDROP, path="s.wav", start=0.0,
                         gain_db=-3.0)]
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2,
                                     music_index=4, sfx=cues, sfx_offset=7)
    assert "[7:a]" in graph and "[8:a]" in graph
    assert "adelay=delays=3250:all=1" in graph
    # narration + bed + two sounds
    assert "amix=inputs=4" in graph


def test_no_sfx_and_no_music_leaves_the_old_graph_untouched():
    """Off is a complete off: the byte-identical graph this had before."""
    graph, _, _ = build_filter_graph(_timed_plan(beats=2), audio_offset=2)
    assert graph.endswith("[narr]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    assert "amix" not in graph and "sidechaincompress" not in graph


# --- input arithmetic -------------------------------------------------------

def test_sfx_inputs_land_after_the_stickers_so_nothing_renumbers(tmp_path):
    """Visuals, narration, music, stickers, THEN sounds.

    `audio_offset`, `music_index` and `sticker_offset` are all positions in
    this argv. Every one of them is computed from the counts before it, so
    a new input class is only safe at the end -- put it anywhere else and
    every beat's narration maps to the wrong stream.
    """
    from engine.assembly.render import build_command
    from engine.config import Settings

    music = _touch(tmp_path / "placeholder-drone.wav")
    for kind in (audio.WHOOSH, audio.POP, audio.SUBDROP):
        _touch(tmp_path / f"placeholder-{kind}.wav")

    plan = _timed_plan(beats=3, measured=4.0)
    for i, beat in enumerate(plan.script.beats):
        beat.image_path = f"C:/tmp/img{i}.png"
        beat.audio_path = f"C:/tmp/a{i}.mp3"
    settings = Settings()
    settings.stickers = False          # its own inputs are pinned elsewhere
    settings.sfx = True
    settings.sfx_dir = tmp_path

    command, _, _ = build_command(plan, settings, Path("C:/tmp/out.mp4"),
                                  music_path=str(music))
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    # 3 stills + 3 narration + 1 music, and the sounds after all of them.
    assert len(inputs) > 7
    assert [Path(p).name for p in inputs[3:6]] == ["a0.mp3", "a1.mp3",
                                                  "a2.mp3"]
    assert Path(inputs[6]).name == "placeholder-drone.wav"
    assert all("placeholder-" in Path(p).name for p in inputs[7:])

    graph = command[command.index("-filter_complex") + 1]
    assert "[3:a][4:a][5:a]concat=n=3" in graph
    assert "[6:a]volume=" in graph
    assert "[7:a]" in graph


# --- the generated placeholders ---------------------------------------------

def _ffmpeg_or_skip():
    from engine.config import Settings
    settings = Settings()
    if not Path(settings.ffmpeg).exists():   # pragma: no cover - env guard
        pytest.skip("ffmpeg is not available in this environment")
    return settings


def test_the_generator_writes_a_bed_and_three_sounds(tmp_path):
    """`scripts/make_audio_assets.py` is how a clean clone gets audio.

    Committing a binary drone into git is how a repo grows a 10MB blob that
    nobody can diff; generating it is how the feature still works out of
    the box. What it produces is cheap, and it is supposed to be: it is a
    stand-in for a sound designer, not one.
    """
    import sys
    settings = _ffmpeg_or_skip()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.make_audio_assets import build_all

    made = build_all(settings.ffmpeg, tmp_path / "music", tmp_path / "sfx")
    assert len(made) == 4
    names = sorted(Path(p).name for p in made)
    assert names == ["placeholder-drone.wav", "placeholder-pop.wav",
                     "placeholder-subdrop.wav", "placeholder-whoosh.wav"]
    for path in made:
        assert Path(path).stat().st_size > 2000, path

    # And they are discoverable by the rule the renderer uses.
    stub = _Stub(music_dir=tmp_path / "music", sfx_dir=tmp_path / "sfx")
    assert audio.find_music(stub)
    for kind in (audio.WHOOSH, audio.POP, audio.SUBDROP):
        assert audio.find_sfx(kind, stub), kind


# --- a real render, measured in decibels ------------------------------------

def _run(ffmpeg, args) -> str:
    result = subprocess.run([ffmpeg, "-hide_banner", "-y", *args],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stderr


def _mean_db(ffmpeg, path, start, duration, band=None):
    """Mean RMS of one window of a file, optionally in one band only."""
    chain = list(band or [])
    chain.append("volumedetect")
    err = _run(ffmpeg, ["-ss", f"{start:.3f}", "-i", str(path),
                        "-t", f"{duration:.3f}", "-af", ",".join(chain),
                        "-f", "null", "-"])
    found = re.search(r"mean_volume:\s*(-?\d+\.?\d*) dB", err)
    assert found, err[-1500:]
    return float(found.group(1))


def _integrated_lufs(ffmpeg, path):
    err = _run(ffmpeg, ["-i", str(path), "-af", "ebur128=framelog=quiet",
                        "-f", "null", "-"])
    found = re.findall(r"I:\s*(-?\d+\.?\d*) LUFS", err)
    assert found, err[-1500:]
    return float(found[-1])


def _max_db(ffmpeg, path):
    err = _run(ffmpeg, ["-i", str(path), "-af", "volumedetect",
                        "-f", "null", "-"])
    found = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", err)
    assert found, err[-1500:]
    return float(found.group(1))


# Everything above 3kHz is the music in this fixture and nothing else: the
# narration is a 220Hz tone and the sub-drop is under 90Hz.
MUSIC_BAND = ["highpass=f=3000:poles=2", "highpass=f=3000:poles=2"]
VOICE_BAND = ["lowpass=f=600:poles=2", "highpass=f=90:poles=2"]


def _audio_render_plan(tmp_path, settings, beats=3, beat_seconds=2.0,
                       speech=1.2):
    """Narration with REAL gaps in it, and a music bed of pure 6kHz.

    The gaps are the whole fixture: a duck that cannot be told from a flat
    gain is exactly what this file exists to catch, and it can only be seen
    where the voice stops. The bed is a single high tone so it can be
    isolated out of the finished mix by frequency, which is the only way to
    measure the music's own level after it has been mixed with speech and
    run through `loudnorm`.
    """
    plan = _timed_plan(beats=beats, measured=beat_seconds)
    for index, beat in enumerate(plan.script.beats):
        wav = tmp_path / f"narr{index}.wav"
        _run(settings.ffmpeg, ["-loglevel", "error", "-f", "lavfi", "-i",
                               f"sine=frequency=220:duration={speech}",
                               # lavfi's `sine` comes out at 0.125 (-18 dBFS
                               # peak); +6dB puts its RMS on -15 dBFS,
                               # which is where piper_voice's
                               # `loudnorm=I=-15` puts a real narration
                               # beat. The duck threshold is measured
                               # against that level, so a fixture that is
                               # quieter tests nothing.
                               "-af", f"apad=whole_dur={beat_seconds},"
                                      f"volume=6dB",
                               "-ar", "48000", "-ac", "1", str(wav)])
        beat.audio_path = str(wav)
        still = tmp_path / f"still{index}.png"
        _run(settings.ffmpeg, ["-loglevel", "error", "-f", "lavfi", "-i",
                               "color=c=navy:size=160x120", "-frames:v", "1",
                               str(still)])
        beat.image_path = str(still)
    music = tmp_path / "music" / "placeholder-tone.wav"
    music.parent.mkdir(parents=True, exist_ok=True)
    _run(settings.ffmpeg, ["-loglevel", "error", "-f", "lavfi", "-i",
                           "sine=frequency=6000:duration=30",
                           "-ar", "48000", "-ac", "1", str(music)])
    return plan, str(music)


def _render_settings(tmp_path):
    settings = _ffmpeg_or_skip()
    settings.width, settings.height, settings.fps = 160, 120, 30
    settings.transition_duration = 0.4
    settings.work_dir = tmp_path
    settings.out_dir = tmp_path
    settings.stickers = False
    return settings


def test_the_music_is_audible_ducked_under_speech_and_lifts_in_the_gap(
        tmp_path):
    """The assertion a filtergraph string cannot fake.

    Three measurements off one rendered MP4:

    * the music's band is loud enough to hear at all (against the same
      render with no music, which is the only honest floor),
    * it is measurably quieter while the narration speaks than in the gap
      between two beats -- which a static ``volume=-18dB`` cannot produce,
      and which is the entire difference between mixing and ducking,
    * and the narration's own band is still there, which is what proves
      the extra SFX inputs did not renumber the beat streams.
    """
    from engine.assembly.render import render
    from scripts.make_audio_assets import build_all
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    settings = _render_settings(tmp_path)
    settings.sfx_dir = tmp_path / "sfx"
    build_all(settings.ffmpeg, tmp_path / "gen", settings.sfx_dir)
    settings.sfx = True

    plan, music = _audio_render_plan(tmp_path, settings)
    total = sum(b.seconds() for b in plan.script.beats)

    with_music = tmp_path / "bed.mp4"
    render(plan, settings, with_music, music_path=music)
    silent = tmp_path / "silent.mp4"
    render(plan, settings, silent, music_path=None)

    from engine.assembly.render import probe_video
    probe = probe_video(with_music, settings.ffmpeg)
    assert probe["duration"] == pytest.approx(total, abs=0.05), \
        "the audio layer moved the narration timeline"

    # Beat 1 runs 2.0-4.0s: it speaks until 3.2 and is silent after.
    speech = _mean_db(settings.ffmpeg, with_music, 2.4, 0.6, MUSIC_BAND)
    gap = _mean_db(settings.ffmpeg, with_music, 3.5, 0.45, MUSIC_BAND)
    floor = _mean_db(settings.ffmpeg, silent, 3.5, 0.45, MUSIC_BAND)

    assert gap - floor > 15.0, (
        f"the bed is not audibly there: {gap:.1f} dB in the gap against a "
        f"{floor:.1f} dB floor with no music at all")
    assert gap - speech > 4.0, (
        f"no ducking: music sits at {speech:.1f} dB under the voice and "
        f"{gap:.1f} dB in the gap, a {gap - speech:.1f} dB lift")

    voice = _mean_db(settings.ffmpeg, with_music, 2.4, 0.6, VOICE_BAND)
    # The brief's own number: "roughly 18dB down while the voice is
    # present". Measured on this fixture at ~19 dB. The band is wide
    # because the exact figure depends on the bed, but a bed 8 dB under
    # the voice is fighting it and one 30 dB under is not there.
    assert 12.0 < voice - speech < 26.0, (
        f"the bed sits {voice - speech:.1f} dB under the voice while it "
        f"speaks; the brief asks for roughly 18")

    voice_gap = _mean_db(settings.ffmpeg, with_music, 3.5, 0.45,
                         VOICE_BAND)
    assert voice - voice_gap > 15.0, (
        f"the narration is not where it should be ({voice:.1f} dB while "
        f"speaking, {voice_gap:.1f} dB in its own gap) -- the most likely "
        f"cause is an input index off by one")


def test_the_finished_mix_still_lands_on_the_loudness_target(tmp_path):
    """-14 LUFS with true peak under -1.5, music and sounds and all.

    This is the assertion that cannot be written as a string test at all.
    Adding tracks to an `amix` is exactly how a mix ends up hot, and
    `loudnorm` is the last filter in the chain for exactly this reason.
    """
    from engine.assembly.render import render
    from scripts.make_audio_assets import build_all
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    settings = _render_settings(tmp_path)
    settings.sfx_dir = tmp_path / "sfx"
    build_all(settings.ffmpeg, tmp_path / "gen", settings.sfx_dir)
    settings.sfx = True

    plan, music = _audio_render_plan(tmp_path, settings, beats=5)
    out = tmp_path / "loud.mp4"
    render(plan, settings, out, music_path=music)

    lufs = _integrated_lufs(settings.ffmpeg, out)
    assert lufs == pytest.approx(-14.0, abs=1.5), f"{lufs} LUFS"
    assert _max_db(settings.ffmpeg, out) < 0.0, "the mix clips"

"""Speeding the finished narration up, after it is spoken.

Piper reads well under conversational Hindi, and the fix people reach for
first is `piper_length_scale` -- which asks Piper to speak faster, works
only for Piper, and means re-synthesising everything to change.

This is the other knob: the audio is sped up after it is written, so it
works on edge-tts too and can be changed without saying the line again.

The placement is the whole point. `speak_beat` writes the file, then
`probe_duration` measures it, then `caption_timings` divides that span
into words. Speeding the file before it is measured means measured_seconds,
the caption timings, the clip count and the sticker cue times are all taken
from the sped-up audio and none of them need adjusting. Speeding it
anywhere later would leave every one of those pointing at a beat that no
longer runs that long.
"""

from __future__ import annotations

import struct
import subprocess

import pytest

from engine.config import Settings
from engine.media.voice import probe_duration, speed_beat

TONE_HZ = 220


@pytest.fixture()
def settings():
    return Settings()


def _tone(settings, path, seconds=2.0):
    """A pure tone, so both duration and pitch are exactly measurable."""
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i",
         f"sine=frequency={TONE_HZ}:sample_rate=24000:duration={seconds}",
         "-c:a", "libmp3lame", "-q:a", "2", str(path)],
        check=True, capture_output=True)
    return path


def _dominant_hz(settings, path):
    """Frequency by zero crossings. Exact enough for a pure tone, and it
    needs no numpy."""
    raw = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
        check=True, capture_output=True).stdout
    samples = struct.unpack(f"<{len(raw) // 2}h", raw[:len(raw) // 2 * 2])
    # Skip the codec's lead-in, which is not the tone.
    samples = samples[2400:-2400]
    crossings = sum(1 for a, b in zip(samples, samples[1:])
                    if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / 24000)


def test_the_beat_gets_shorter_by_the_factor_asked_for(settings, tmp_path):
    beat = _tone(settings, tmp_path / "b.mp3", seconds=2.0)
    before = probe_duration(beat, settings.ffmpeg)

    settings.voice_speed = 1.2
    assert speed_beat(beat, settings) is True

    after = probe_duration(beat, settings.ffmpeg)
    assert after == pytest.approx(before / 1.2, abs=0.05), \
        f"{before:.3f}s at 1.2x should be {before / 1.2:.3f}s, got {after:.3f}s"


def test_the_voice_does_not_go_up_in_pitch(settings, tmp_path):
    """atempo stretches time; resampling would raise the pitch by the same
    factor and turn the narrator into a chipmunk. 220Hz sped 1.2x by
    resampling would come back at 264Hz."""
    beat = _tone(settings, tmp_path / "b.mp3", seconds=2.0)
    settings.voice_speed = 1.2
    speed_beat(beat, settings)

    hz = _dominant_hz(settings, beat)
    assert hz == pytest.approx(TONE_HZ, rel=0.05), \
        f"pitch moved to {hz:.0f}Hz; 1.2x by resampling would give 264Hz"


def test_speed_one_leaves_the_file_exactly_alone(settings, tmp_path):
    """Not merely 'the same length'. At 1.0 there is nothing to do, and
    re-encoding an mp3 for nothing costs a generation of quality."""
    beat = _tone(settings, tmp_path / "b.mp3")
    original = beat.read_bytes()

    settings.voice_speed = 1.0
    assert speed_beat(beat, settings) is False
    assert beat.read_bytes() == original


def test_a_speed_that_ffmpeg_would_refuse_is_ignored(settings, tmp_path):
    """One atempo handles 0.5-2.0. Outside that the pass declines rather
    than emitting a filter ffmpeg rejects, because a beat that failed to
    speed up is a slightly slow reel and a beat that failed to write is no
    reel at all."""
    beat = _tone(settings, tmp_path / "b.mp3")
    original = beat.read_bytes()

    for bad in (0.0, -1.0, 2.5):
        settings.voice_speed = bad
        assert speed_beat(beat, settings) is False, bad
        assert beat.read_bytes() == original


def test_a_failed_pass_leaves_the_beat_as_synthesis_wrote_it(settings,
                                                             tmp_path,
                                                             monkeypatch):
    """Same contract as the edge trim beside it: this is polish, so a
    failure costs the polish and never the beat."""
    beat = _tone(settings, tmp_path / "b.mp3")
    original = beat.read_bytes()
    settings.voice_speed = 1.2
    monkeypatch.setattr(settings, "ffmpeg", "definitely-not-ffmpeg")

    assert speed_beat(beat, settings) is False
    assert beat.read_bytes() == original


def test_speak_beat_hands_back_an_already_sped_file(settings, tmp_path,
                                                    monkeypatch):
    """The claim the placement rests on, at the seam that matters.

    `voice_stage` calls `speak_beat`, then measures the file, then divides
    that span into caption words. If the speed-up happens inside
    `speak_beat`, every one of those numbers is taken from the audio that
    will actually play and nothing downstream needs to know this feature
    exists. This checks the file is already short by the time it is handed
    back -- not that some later step compensates.
    """
    from engine.media import voice as voice_mod

    spoken = _tone(settings, tmp_path / "spoken.mp3", seconds=2.0)
    plain = probe_duration(spoken, settings.ffmpeg)

    def fake_synth(text, target, s):
        __import__("shutil").copyfile(spoken, target)
        return 0

    monkeypatch.setattr(voice_mod, "synth_beat_piper", fake_synth)
    monkeypatch.setattr(voice_mod, "_trim_beat_edges",
                        lambda target, s: False)
    settings.voice_speed = 1.2

    out = tmp_path / "beat.mp3"
    engine, _spans, _note = voice_mod.speak_beat("कुछ", out, settings,
                                                 engine="piper")
    assert engine == "piper"
    assert probe_duration(out, settings.ffmpeg) == pytest.approx(
        plain / 1.2, abs=0.05), "speak_beat handed back the unsped file"


def test_the_caption_words_follow_the_sped_audio(settings, tmp_path):
    """The drift this placement exists to prevent.

    `caption_timings` divides a measured span into words. Measured off the
    sped file the last word ends with the audio; measured off the original
    it would run past the end of a beat that is now a fifth shorter, and
    every sticker cue hanging off those words with it.
    """
    from engine.media.voice import caption_timings

    beat = _tone(settings, tmp_path / "b.mp3", seconds=2.0)
    settings.voice_speed = 1.2
    speed_beat(beat, settings)

    measured = probe_duration(beat, settings.ffmpeg)
    words = caption_timings("ek do teen chaar paanch", measured)
    assert words, "no timings to check"
    assert words[-1].end == pytest.approx(measured, abs=0.02)


# --- changing the speed after the fact ------------------------------------

def test_the_spoken_original_is_kept_beside_the_sped_beat(settings, tmp_path,
                                                          monkeypatch):
    """Changing the speed later has to start from what Piper said.

    Re-speeding an already-sped file compounds: 1.2 applied twice is 1.44,
    and the factor the person asked for is not the factor they get. Same
    reason `raw_audio_path` keeps a human upload beside the cleaned copy.
    """
    from engine.media import voice as voice_mod

    spoken = _tone(settings, tmp_path / "spoken.mp3", seconds=2.0)
    plain = probe_duration(spoken, settings.ffmpeg)

    monkeypatch.setattr(voice_mod, "synth_beat_piper",
                        lambda text, target, s: (
                            __import__("shutil").copyfile(spoken, target), 0)[1])
    monkeypatch.setattr(voice_mod, "_trim_beat_edges", lambda t, s: False)
    settings.voice_speed = 1.2

    out = tmp_path / "beat.mp3"
    voice_mod.speak_beat("कुछ", out, settings, engine="piper")

    master = voice_mod.spoken_master_path(out)
    assert master.is_file(), "the spoken original was not kept"
    assert probe_duration(master, settings.ffmpeg) == pytest.approx(
        plain, abs=0.05), "the kept original is not the unsped audio"


def test_respeeding_starts_from_the_original_and_does_not_compound(
        settings, tmp_path, monkeypatch):
    """1.2 then 1.3 must give 1.3, not 1.56."""
    from engine.media import voice as voice_mod

    spoken = _tone(settings, tmp_path / "spoken.mp3", seconds=3.0)
    plain = probe_duration(spoken, settings.ffmpeg)

    monkeypatch.setattr(voice_mod, "synth_beat_piper",
                        lambda text, target, s: (
                            __import__("shutil").copyfile(spoken, target), 0)[1])
    monkeypatch.setattr(voice_mod, "_trim_beat_edges", lambda t, s: False)

    out = tmp_path / "beat.mp3"
    settings.voice_speed = 1.2
    voice_mod.speak_beat("कुछ", out, settings, engine="piper")

    settings.voice_speed = 1.3
    assert voice_mod.respeed_beat(out, settings) is True
    assert probe_duration(out, settings.ffmpeg) == pytest.approx(
        plain / 1.3, abs=0.08), "re-speeding compounded on the sped file"

    # And back down again, from the same original.
    settings.voice_speed = 1.0
    assert voice_mod.respeed_beat(out, settings) is True
    assert probe_duration(out, settings.ffmpeg) == pytest.approx(
        plain, abs=0.08), "1.0 did not restore the spoken pace"

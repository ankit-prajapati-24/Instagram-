"""Trimming the silence each synthesised beat ends on.

Every beat Piper wrote came back padded. Measured across a real
ten-beat reel:

    b1  tail 0.25s     b6  tail 0.31s
    b2  tail 0.27s     b7  tail 0.20s
    b3  tail 0.22s     b8  tail 0.24s
    b4  tail 0.20s     b9  tail 0.29s
    b5  tail 0.24s     b10 tail 0.24s

Six of them also opened on 0.05-0.09s of nothing. The beats are
concatenated, so at every seam the previous tail and the next head add
up to about a third of a second of silence -- nine times in one reel.
That is the "dead air between sentences" a reviewer heard, and it is
systematic rather than occasional.

**Only the edges.** The pauses *inside* a beat are speech rhythm, and
``voice_pause_cap`` exists to protect exactly them -- its comment says a
natural inter-word gap of 0.1-0.3s must survive untouched. The measured
mid-beat gaps are 0.05-0.37s, which is that range, so a cap low enough
to shorten them would be cutting rhythm rather than dead air. This trims
the head and the tail and leaves everything between them alone.

**Not the recorded-audio chain.** ``cleanup_filters`` denoises,
de-clicks and highpasses because a phone recording has room noise and
mouth clicks. Synthesised speech has neither. Measured on one Piper
beat, running the full chain over it cost 0.5 LU (-15.8 -> -16.3) and
trimmed no more than the trim alone did: whatever the denoiser removed
from clean TTS was signal, not noise.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from engine.config import Settings
from engine.media.voice import (EDGE_SILENCE_KEPT, edge_trim_filter,
                                probe_duration)


@pytest.fixture()
def settings():
    return Settings()


def _speech(settings, path, *, head=0.0, tail=0.0, middle=0.0):
    """A tone with silence bolted on where a beat's would be.

    A tone rather than real speech because the filter keys on level, not
    on content, and a generated file makes the expected durations exact.
    """
    parts, inputs, n = [], [], 0

    def add(filter_):
        nonlocal n
        inputs.extend(["-f", "lavfi", "-i", filter_])
        parts.append(f"[{n}:a]")
        n += 1

    if head:
        add(f"anullsrc=r=24000:cl=mono:d={head}")
    add("sine=frequency=220:sample_rate=24000:duration=0.6")
    if middle:
        add(f"anullsrc=r=24000:cl=mono:d={middle}")
        add("sine=frequency=220:sample_rate=24000:duration=0.6")
    if tail:
        add(f"anullsrc=r=24000:cl=mono:d={tail}")

    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", *inputs,
         "-filter_complex", f"{''.join(parts)}concat=n={n}:v=0:a=1[a]",
         "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", str(path)],
        check=True, capture_output=True)
    return path


def _apply(settings, src, dst, filters):
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src),
         "-af", filters, "-c:a", "libmp3lame", "-q:a", "2", str(dst)],
        check=True, capture_output=True)
    return dst


def _silences(settings, path):
    out = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=noise=-45dB:d=0.02", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.-]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    if len(ends) < len(starts):
        ends.append(probe_duration(path, settings.ffmpeg))
    return list(zip(starts, ends))


# --- the trim ---------------------------------------------------------------


def test_the_tail_a_beat_ends_on_is_cut(settings, tmp_path):
    """The defect: every beat in the measured reel ended on 0.20-0.31s."""
    src = _speech(settings, tmp_path / "in.mp3", tail=0.30)
    before = probe_duration(src, settings.ffmpeg)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    assert probe_duration(out, settings.ffmpeg) < before - 0.15


def test_the_head_is_cut_too(settings, tmp_path):
    src = _speech(settings, tmp_path / "in.mp3", head=0.30)
    before = probe_duration(src, settings.ffmpeg)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    assert probe_duration(out, settings.ffmpeg) < before - 0.15


def test_a_breath_is_left_rather_than_cutting_to_the_word(settings,
                                                          tmp_path):
    """Trimmed to nothing, one sentence starts on the syllable the last
    one ended on. The seam needs to be tight, not absent.

    Measured on the file rather than derived from the constant: mp3
    padding adds a consistent ~0.06s, so EDGE_SILENCE_KEPT is what the
    filter is asked for and this is what comes out. An earlier version
    of this test subtracted an assumed tone length instead and reported
    the padding as a failure.
    """
    src = _speech(settings, tmp_path / "in.mp3", tail=0.40)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    regions = _silences(settings, out)
    assert regions, "the tail was cut away entirely"
    start, end = regions[-1]
    assert end - start == pytest.approx(EDGE_SILENCE_KEPT, abs=0.02)


def test_a_pause_inside_the_beat_is_left_alone(settings, tmp_path):
    """Speech rhythm, not dead air. voice_pause_cap's own comment
    protects 0.1-0.3s inter-word gaps, and the measured mid-beat gaps
    are exactly that."""
    src = _speech(settings, tmp_path / "in.mp3", middle=0.28)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    inner = [end - start for start, end in _silences(settings, out)
             if start > 0.1]
    assert inner, "the pause between the two tones vanished"
    assert max(inner) == pytest.approx(0.28, abs=0.06)


def test_a_beat_with_no_padding_is_left_as_it_is(settings, tmp_path):
    src = _speech(settings, tmp_path / "in.mp3")
    before = probe_duration(src, settings.ffmpeg)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    assert probe_duration(out, settings.ffmpeg) == pytest.approx(before,
                                                                 abs=0.08)


def test_both_ends_go_in_one_pass(settings, tmp_path):
    src = _speech(settings, tmp_path / "in.mp3", head=0.35, tail=0.35)
    before = probe_duration(src, settings.ffmpeg)

    out = _apply(settings, src, tmp_path / "out.mp3",
                 edge_trim_filter(settings))

    assert probe_duration(out, settings.ffmpeg) < before - 0.4


# --- the switch -------------------------------------------------------------


def test_the_trim_can_be_turned_off(settings):
    settings.voice_trim_edges = False

    assert edge_trim_filter(settings) == ""


def test_it_is_on_by_default():
    assert Settings().voice_trim_edges is True


def test_it_is_not_the_recorded_audio_chain(settings):
    """Synthesised speech has no room noise to denoise and no mouth
    clicks to de-click; measured, running those over it cost 0.5 LU and
    trimmed nothing extra."""
    trim = edge_trim_filter(settings)

    assert "silenceremove" in trim
    assert "afftdn" not in trim
    assert "adeclick" not in trim
    assert "highpass" not in trim


# --- applied where the beat is spoken --------------------------------------


def test_speaking_a_beat_trims_what_it_wrote(settings, tmp_path,
                                             monkeypatch):
    """Inside speak_beat, so both callers get it: the plan loop and the
    re-speak route at the voice gate."""
    import engine.media.voice as voice

    padded = _speech(settings, tmp_path / "src.mp3", head=0.30, tail=0.30)
    before = probe_duration(padded, settings.ffmpeg)

    def fake_synth(text, target, s):
        import shutil
        shutil.copyfile(padded, target)
        return 0

    monkeypatch.setattr(voice, "synth_beat_piper", fake_synth)
    out = tmp_path / "beat.mp3"

    voice.speak_beat("kuch bhi", out, settings)

    assert probe_duration(out, settings.ffmpeg) < before - 0.4


def test_the_beat_is_still_playable_after_the_trim(settings, tmp_path,
                                                   monkeypatch):
    """The trim rewrites the file the render will concatenate; a
    half-written one would be composited without complaint."""
    import engine.media.voice as voice

    padded = _speech(settings, tmp_path / "src.mp3", tail=0.30)

    def fake_synth(text, target, s):
        import shutil
        shutil.copyfile(padded, target)
        return 0

    monkeypatch.setattr(voice, "synth_beat_piper", fake_synth)
    out = tmp_path / "beat.mp3"

    voice.speak_beat("kuch bhi", out, settings)

    probe = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-i", str(out),
         "-f", "null", "-"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr[-300:]


def test_a_failed_trim_leaves_the_spoken_beat_alone(settings, tmp_path,
                                                    monkeypatch):
    """Better an untrimmed beat than no beat. The audio is already
    correct when the trim runs; it is a polish pass, not a stage."""
    import engine.media.voice as voice

    padded = _speech(settings, tmp_path / "src.mp3", tail=0.30)
    before = probe_duration(padded, settings.ffmpeg)

    def fake_synth(text, target, s):
        import shutil
        shutil.copyfile(padded, target)
        return 0

    monkeypatch.setattr(voice, "synth_beat_piper", fake_synth)
    monkeypatch.setattr(voice, "edge_trim_filter",
                        lambda s: "definitely_not_a_filter")
    out = tmp_path / "beat.mp3"

    voice.speak_beat("kuch bhi", out, settings)

    assert probe_duration(out, settings.ffmpeg) == pytest.approx(before,
                                                                 abs=0.05)


def test_the_trim_switch_reaches_the_spoken_beat(settings, tmp_path,
                                                 monkeypatch):
    import engine.media.voice as voice

    settings.voice_trim_edges = False
    padded = _speech(settings, tmp_path / "src.mp3", tail=0.30)
    before = probe_duration(padded, settings.ffmpeg)

    def fake_synth(text, target, s):
        import shutil
        shutil.copyfile(padded, target)
        return 0

    monkeypatch.setattr(voice, "synth_beat_piper", fake_synth)
    out = tmp_path / "beat.mp3"

    voice.speak_beat("kuch bhi", out, settings)

    assert probe_duration(out, settings.ffmpeg) == pytest.approx(before,
                                                                 abs=0.05)

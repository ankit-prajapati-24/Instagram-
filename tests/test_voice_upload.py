"""Narration a human supplied, instead of narration Piper synthesised.

Every test here runs ffmpeg. The thing being tested *is* an ffmpeg call and
a number read back off its output, so a test that asserted on the command
string would prove nothing about the file the render is going to open — and
this repo has already shipped five defects past a green suite built exactly
that way.

Two facts these tests pin, both measured off the real pipeline rather than
chosen:

  * Piper writes **24000 Hz mono mp3**. ``stitch_narration`` concatenates
    the beats with ffmpeg's concat demuxer, which needs every input to
    agree, so an upload that stays at 44.1 kHz stereo is not a cosmetic
    mismatch.
  * Piper's own beats measure -18.2, -15.9 and -16.3 LUFS. ``-16`` is the
    middle of the band it already occupies, which is why an upload
    normalised to it sits next to a synthesised beat without a step in
    level.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.config import Settings
from engine.media.voice import (NARRATION_CHANNELS, NARRATION_SAMPLE_RATE,
                                UPLOAD_LOUDNESS, UploadRejected,
                                apply_beat_audio, ingest_narration)
from tests.factories import make_plan


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    return s


def _tone(settings, path: Path, *, seconds: float, hz: int = 220,
          rate: int = 44100, channels: int = 2,
          volume: str = "0dB") -> Path:
    """A real audio file, deliberately in the wrong format for the pipeline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
         "-af", f"volume={volume}", "-ar", str(rate), "-ac", str(channels),
         str(path)], check=True, capture_output=True)
    return path


def _probe(settings, path: Path) -> dict:
    """Sample rate, channel count and codec, read back off the file."""
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True)
    line = next(ln for ln in result.stderr.splitlines() if "Stream #" in ln)
    return {
        "rate": int(next(p for p in line.split(", ") if p.endswith("Hz"))
                    .split()[0]),
        "mono": "mono" in line,
        "mp3": "mp3" in line,
    }


def _loudness(settings, path: Path) -> float:
    """Integrated LUFS, the same way the render's loudnorm would see it."""
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
        capture_output=True, text=True)
    line = next(ln for ln in result.stderr.splitlines()
                if "Input Integrated" in ln)
    return float(line.split(":")[1].strip().split()[0])


# --- the ingest ------------------------------------------------------------


def test_an_upload_is_rewritten_into_the_format_the_concat_demuxer_needs(
        settings, tmp_path):
    """44.1 kHz stereo in, 24 kHz mono mp3 out — Piper's exact format.

    Not cosmetic: ``stitch_narration`` uses the concat demuxer, and a beat
    that disagrees about sample rate or channel count either fails the
    concat or plays at the wrong speed.
    """
    source = _tone(settings, tmp_path / "in.wav", seconds=2.0,
                   rate=44100, channels=2)
    target = tmp_path / "out.mp3"

    ingest_narration(source, target, settings)

    got = _probe(settings, target)
    assert got["rate"] == NARRATION_SAMPLE_RATE == 24000
    assert got["mono"] is (NARRATION_CHANNELS == 1)
    assert got["mp3"]


def test_a_quiet_upload_is_lifted_into_the_band_piper_already_occupies(
        settings, tmp_path):
    """A phone recording at -36 dB must not sit under the synthesised beats.

    The final ``loudnorm`` in the render runs on the *concatenated*
    narration, so it cannot fix one beat being quieter than its neighbours
    — by then they are one stream. Levelling has to happen here, per beat.
    """
    source = _tone(settings, tmp_path / "quiet.wav", seconds=3.0,
                   volume="-30dB")
    assert _loudness(settings, source) < UPLOAD_LOUDNESS - 10, \
        "the fixture is supposed to start far too quiet"

    ingest_narration(source, tmp_path / "out.mp3", settings)

    after = _loudness(settings, tmp_path / "out.mp3")
    assert abs(after - UPLOAD_LOUDNESS) <= 1.5, \
        f"normalised to {after} LUFS, wanted about {UPLOAD_LOUDNESS}"


def test_a_loud_upload_is_pulled_down_to_the_same_band(settings, tmp_path):
    source = _tone(settings, tmp_path / "loud.wav", seconds=3.0,
                   volume="0dB")
    ingest_narration(source, tmp_path / "out.mp3", settings)
    after = _loudness(settings, tmp_path / "out.mp3")
    assert abs(after - UPLOAD_LOUDNESS) <= 1.5


def test_the_ingest_keeps_the_length_it_was_given(settings, tmp_path):
    """Duration is the one thing normalising must not touch: it is the
    beat's span, and every clip slot and caption word is cut from it."""
    source = _tone(settings, tmp_path / "in.wav", seconds=4.25)
    seconds = ingest_narration(source, tmp_path / "out.mp3", settings)
    assert abs(seconds - 4.25) < 0.1


def test_bytes_that_do_not_decode_are_refused_here_not_at_the_render(
        settings, tmp_path):
    """A signature is eight bytes; this is the check that costs something."""
    source = tmp_path / "liar.mp3"
    source.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 4096)
    with pytest.raises(UploadRejected):
        ingest_narration(source, tmp_path / "out.mp3", settings)


def test_a_failed_ingest_leaves_no_half_written_file(settings, tmp_path):
    source = tmp_path / "liar.mp3"
    source.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)
    target = tmp_path / "out.mp3"
    with pytest.raises(UploadRejected):
        ingest_narration(source, target, settings)
    assert not target.exists()


def test_silence_is_refused_because_a_beat_has_to_say_something(
        settings, tmp_path):
    """A muted file decodes perfectly and would render as a silent gap the
    length of the beat, with captions scrolling over nothing."""
    source = tmp_path / "mute.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "3",
         str(source)], check=True, capture_output=True)
    with pytest.raises(UploadRejected, match="silen"):
        ingest_narration(source, tmp_path / "out.mp3", settings)


def test_a_file_too_short_to_be_a_beat_is_refused(settings, tmp_path):
    source = _tone(settings, tmp_path / "blip.wav", seconds=0.2)
    with pytest.raises(UploadRejected, match="short"):
        ingest_narration(source, tmp_path / "out.mp3", settings)


# --- what it does to the beat ----------------------------------------------


def test_replacing_a_beats_audio_retimes_it_and_its_caption_words(
        settings, tmp_path):
    """The beat's span and every caption word inside it are derived from
    the audio, so swapping the audio has to redo both."""
    plan = make_plan(plan_id="p1", beats=2)
    beat = plan.script.beats[0]
    beat.measured_seconds = 99.0
    beat.words = []
    audio = tmp_path / "b1.mp3"
    ingest_narration(_tone(settings, tmp_path / "in.wav", seconds=3.0),
                     audio, settings)

    apply_beat_audio(beat, audio, settings, engine="upload")

    assert beat.audio_path == str(audio)
    assert beat.voice_engine == "upload"
    assert abs(beat.measured_seconds - 3.0) < 0.1
    assert beat.words, "caption words were not rebuilt"
    assert abs(beat.words[-1].end - beat.measured_seconds) < 0.01
    assert beat.words[0].start == pytest.approx(0.0)


def test_retiming_one_beat_moves_the_plans_total(settings, tmp_path):
    plan = make_plan(plan_id="p1", beats=2)
    for b in plan.script.beats:
        b.measured_seconds = 5.0
    before = plan.duration()
    audio = tmp_path / "b1.mp3"
    ingest_narration(_tone(settings, tmp_path / "in.wav", seconds=8.0),
                     audio, settings)

    apply_beat_audio(plan.script.beats[0], audio, settings, engine="upload")

    assert plan.duration() > before + 2.5

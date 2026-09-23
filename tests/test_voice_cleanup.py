"""The cleanup filter chain a human-recorded upload runs through.

Same discipline as ``tests/test_voice_upload.py``: every claim here is
measured off a real ffmpeg run, not asserted against the filter string,
because a test that only checks the string proves nothing about the audio
it would produce.

This task builds the filter-string builder and the report dataclass in
isolation -- ``ingest_narration`` is not wired to either yet (that is Task
2) -- so these tests run ``cleanup_filters(settings)`` output through
ffmpeg by hand and measure the result back off the file, the same way
Task 2's real pipeline will.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from engine.media.voice import cleanup_filters, probe_duration
from tests.factories import shipped_settings


@pytest.fixture()
def settings(tmp_path):
    s = shipped_settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    return s


def _run_filter(settings, source: Path, target: Path, filters: str) -> None:
    """Apply an ``-af`` chain with real ffmpeg and write the result."""
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-i", str(source), "-af", filters, str(target)],
        check=True, capture_output=True)


def _concat(settings, path: Path, parts: list[str], *,
           rate: int = 44100) -> Path:
    """Build one file out of lavfi source specs, concatenated in order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = len(parts)
    inputs: list[str] = []
    for part in parts:
        inputs += ["-f", "lavfi", "-i", part]
    labels = "".join(f"[{i}:a]" for i in range(n))
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         *inputs,
         "-filter_complex", f"{labels}concat=n={n}:v=0:a=1[out]",
         "-map", "[out]", "-ar", str(rate), "-ac", "1", str(path)],
        check=True, capture_output=True)
    return path


def _silencedetect_gaps(settings, path: Path, *,
                        noise_db: float, min_duration: float) -> list[float]:
    """Silence-gap durations ``silencedetect`` reports inside the file."""
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    return [float(m) for m in
            re.findall(r"silence_duration:\s*([\d.]+)", result.stderr)]


def _mean_volume(settings, path: Path, *, isolate_above_hz: float) -> float:
    """Mean volume (dB) of everything above ``isolate_above_hz``.

    A tone's fundamental and low harmonics sit well under this cutoff, so a
    highpass here isolates the broadband noise floor mixed in alongside it
    -- which is what afftdn is supposed to have pulled down.
    """
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"highpass=f={isolate_above_hz},volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True)
    line = next(ln for ln in result.stderr.splitlines()
                if "mean_volume" in ln)
    return float(line.split(":")[1].strip().split()[0])


# --- the noise floor ---------------------------------------------------


def test_the_chain_pulls_down_the_noise_floor_under_a_tone(settings, tmp_path):
    """A tone with white noise mixed in, denoised without touching the tone.

    Measured by isolating everything above 4000 Hz -- well clear of a
    220 Hz tone's fundamental and low harmonics -- so what's left is (almost
    entirely) the mixed-in broadband noise, before and after.
    """
    noisy = tmp_path / "noisy.wav"
    noisy.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
         "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.05:duration=3",
         "-filter_complex",
         "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[out]",
         "-map", "[out]", "-ar", "44100", "-ac", "1", str(noisy)],
        check=True, capture_output=True)

    before = _mean_volume(settings, noisy, isolate_above_hz=4000)

    cleaned = tmp_path / "cleaned.wav"
    _run_filter(settings, noisy, cleaned, cleanup_filters(settings))

    after = _mean_volume(settings, cleaned, isolate_above_hz=4000)

    assert after < before - 8.0, (
        f"noise floor only moved {before:.1f} -> {after:.1f} dB, "
        f"wanted at least 8 dB of reduction")


# --- silenceremove: the internal gap ------------------------------------


def test_an_internal_silence_longer_than_the_cap_is_shortened_to_it(
        settings, tmp_path):
    """tone / 2s silence / tone: the chain caps the gap at ``pause_cap``.

    Verified against real ffmpeg, not assumed (see ``cleanup_filters``'s
    docstring): ``silenceremove``'s ``start_silence``/``stop_silence`` are
    the maximum silence *kept* after trimming, and ``stop_periods=-1``
    reapplies that trimming to every internal gap, not just the ends.
    """
    source = _concat(settings, tmp_path / "gap.wav", [
        "sine=frequency=220:duration=1",
        "anullsrc=r=44100:cl=mono:d=2",
        "sine=frequency=220:duration=1",
    ])
    before = probe_duration(source, settings.ffmpeg)
    assert abs(before - 4.0) < 0.05

    target = tmp_path / "capped.wav"
    _run_filter(settings, source, target, cleanup_filters(settings))
    after = probe_duration(target, settings.ffmpeg)

    pause_cap = settings.voice_pause_cap
    expected = before - (2.0 - pause_cap)
    assert abs(after - expected) < 0.15, (
        f"got {after:.2f}s, wanted about {expected:.2f}s "
        f"({before:.2f}s minus the excess over pause_cap)")

    gaps = _silencedetect_gaps(
        settings, target,
        noise_db=settings.voice_silence_threshold, min_duration=0.05)
    assert gaps, "expected the capped gap to still register as silence"
    assert max(gaps) <= pause_cap + 0.1, (
        f"a gap of {max(gaps):.2f}s survived, longer than "
        f"pause_cap ({pause_cap}) + 0.1")


def test_natural_inter_word_gaps_are_left_alone(settings, tmp_path):
    """Gaps already under ``pause_cap`` (0.2s, a real inter-word pause)
    must not be squeezed -- only excess silence is trimmed."""
    source = _concat(settings, tmp_path / "natural.wav", [
        "sine=frequency=220:duration=1",
        "anullsrc=r=44100:cl=mono:d=0.2",
        "sine=frequency=220:duration=1",
        "anullsrc=r=44100:cl=mono:d=0.2",
        "sine=frequency=220:duration=1",
    ])
    before = probe_duration(source, settings.ffmpeg)

    target = tmp_path / "natural_out.wav"
    _run_filter(settings, source, target, cleanup_filters(settings))
    after = probe_duration(target, settings.ffmpeg)

    assert abs(after - before) < 0.15, (
        f"a natural 0.2s gap was squeezed: {before:.2f}s -> {after:.2f}s")


# --- the switch ----------------------------------------------------------


def test_cleanup_off_returns_an_empty_fragment(settings):
    settings.voice_clean = False
    assert cleanup_filters(settings) == ""


def test_cleanup_off_means_no_filter_is_applied_at_all(settings, tmp_path):
    """The off switch is a real off: run through ffmpeg with the empty
    fragment as a no-op filter, the file is unchanged."""
    settings.voice_clean = False
    source = _concat(settings, tmp_path / "gap.wav", [
        "sine=frequency=220:duration=1",
        "anullsrc=r=44100:cl=mono:d=2",
        "sine=frequency=220:duration=1",
    ])
    before = probe_duration(source, settings.ffmpeg)
    fragment = cleanup_filters(settings)
    assert fragment == ""

    target = tmp_path / "untouched.wav"
    # anull is the identity filter; this is what a caller does with an
    # empty fragment when it must still pass *something* to -af.
    _run_filter(settings, source, target, fragment or "anull")
    after = probe_duration(target, settings.ffmpeg)
    assert abs(after - before) < 0.05


# --- each Setting reaches the string --------------------------------------


def test_highpass_setting_reaches_the_filter_string(settings):
    settings.voice_clean_highpass = 123.0
    assert "highpass=f=123.0" in cleanup_filters(settings)


def test_denoise_setting_reaches_the_filter_string(settings):
    settings.voice_clean_denoise = -30.0
    assert "afftdn=nf=-30.0" in cleanup_filters(settings)


def test_pause_cap_setting_reaches_the_filter_string(settings):
    settings.voice_pause_cap = 0.5
    fragment = cleanup_filters(settings)
    assert "start_silence=0.5" in fragment
    assert "stop_silence=0.5" in fragment


def test_silence_threshold_setting_reaches_the_filter_string(settings):
    settings.voice_silence_threshold = -50.0
    fragment = cleanup_filters(settings)
    assert "start_threshold=-50.0dB" in fragment
    assert "stop_threshold=-50.0dB" in fragment


def test_the_fragment_has_no_leading_or_trailing_comma(settings):
    fragment = cleanup_filters(settings)
    assert fragment
    assert not fragment.startswith(",")
    assert not fragment.endswith(",")

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
                                SILENCE_LUFS, UPLOAD_LOUDNESS,
                                UploadRejected, apply_beat_audio,
                                cleanup_filters, ingest_narration,
                                probe_duration)
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


def _noisy_tone(settings, path: Path, *, seconds: float = 3.0) -> Path:
    """A tone with white noise mixed in -- a noisy room behind real
    narration, not silence and not pure noise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
         "-f", "lavfi",
         "-i", f"anoisesrc=color=white:amplitude=0.05:duration={seconds}",
         "-filter_complex",
         "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[out]",
         "-map", "[out]", "-ar", "44100", "-ac", "1", str(path)],
        check=True, capture_output=True)
    return path


def _gap_tone(settings, path: Path) -> Path:
    """tone / 2s silence / tone -- an internal gap cleanup's
    ``silenceremove`` caps at ``voice_pause_cap``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
         "-map", "[out]", "-ar", "44100", "-ac", "1", str(path)],
        check=True, capture_output=True)
    return path


def _mean_volume_above(settings, path: Path, hz: float) -> float:
    """Mean volume (dB) of everything above ``hz`` -- isolates broadband
    noise mixed in beside a low tone, same measurement
    tests/test_voice_cleanup.py uses to show a noise floor moved."""
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"highpass=f={hz},volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    line = next(ln for ln in result.stderr.splitlines()
                if "mean_volume" in ln)
    return float(line.split(":")[1].strip().split()[0])


def _cleaned_loudness_preview(settings, source: Path, tmp_path: Path) -> float:
    """What ``source`` would measure at if cleanup ran on it standalone.

    Used only to prove a refusal-ordering scenario is real -- the fixture
    really would look silent if the silence gate ran on the cleaned signal
    -- never to decide what ``ingest_narration`` itself should do.
    """
    preview = tmp_path / f"{source.stem}-cleaned-preview.wav"
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-i", str(source), "-af", cleanup_filters(settings), str(preview)],
        check=True, capture_output=True)
    return _loudness(settings, preview)


def _decorrelated_stereo(settings, path: Path, *,
                         seconds: float = 5.0) -> Path:
    """Genuinely two-channel audio: a different tone in L and R, joined --
    not one mono source duplicated to both, which is all ``_tone`` and
    ``_noisy_tone`` ever produce. A real phone recording with two live
    channels looks like this, not like a mono signal wearing a stereo
    header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
         "-filter_complex",
         "[0:a][1:a]join=inputs=2:channel_layout=stereo[out]",
         "-map", "[out]", "-ar", "44100", str(path)],
        check=True, capture_output=True)
    return path


# --- the ingest ------------------------------------------------------------


@pytest.mark.parametrize("voice_clean", [True, False])
def test_an_upload_is_rewritten_into_the_format_the_concat_demuxer_needs(
        settings, tmp_path, voice_clean):
    """44.1 kHz stereo in, 24 kHz mono mp3 out — Piper's exact format.

    Not cosmetic: ``stitch_narration`` uses the concat demuxer, and a beat
    that disagrees about sample rate or channel count either fails the
    concat or plays at the wrong speed.

    Parametrised over ``voice_clean`` (final review, Minor 7): the cleaned
    path reaches this format through ``aresample``/``aformat`` inside the
    filter graph; the off path used to reach it through the output-side
    ``-ar``/``-ac`` flags instead -- two different mechanisms, previously
    pinned by one assertion running under whichever ``Settings()`` default
    the developer's environment happened to read. Both are pinned here
    regardless of that ambient default.
    """
    settings.voice_clean = voice_clean
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
    seconds, _report = ingest_narration(source, tmp_path / "out.mp3", settings)
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


# --- cleanup folded into the ingest (Task 2) --------------------------------


def test_the_raw_upload_lands_beside_the_cleaned_one_byte_identical(
        settings, tmp_path):
    """``raw_target`` gets the exact bytes handed over -- not transcoded,
    not cleaned -- because that copy is what a later revert re-ingests."""
    settings.voice_clean = True
    source = _tone(settings, tmp_path / "in.wav", seconds=3.0)
    raw_copy = tmp_path / "raw" / "kept.wav"

    seconds, report = ingest_narration(
        source, tmp_path / "out.mp3", settings, raw_target=raw_copy)

    assert raw_copy.read_bytes() == source.read_bytes()
    assert seconds > 0
    assert report.filters_applied


def test_the_clean_parameter_overrides_the_setting_for_one_call(
        settings, tmp_path):
    """``clean=False`` turns cleanup off for a call even though the setting
    is on, and ``clean=True`` turns it on even though the setting is off --
    what the revert route (Task 3) needs, measured off real output here."""
    settings.voice_clean = True
    on_by_default = _noisy_tone(settings, tmp_path / "a.wav")
    _, forced_off = ingest_narration(
        on_by_default, tmp_path / "a.mp3", settings, clean=False)
    assert forced_off.filters_applied == ""

    settings.voice_clean = False
    off_by_default = _noisy_tone(settings, tmp_path / "b.wav")
    _, forced_on = ingest_narration(
        off_by_default, tmp_path / "b.mp3", settings, clean=True)
    assert forced_on.filters_applied


def test_cleanup_on_a_decorrelated_stereo_upload_still_lands_in_the_band(
        settings, tmp_path):
    """Review round 1, Critical 1: measuring the cleaned signal before it
    is downmixed to mono, then writing the downmix afterwards, computes
    loudnorm's gain for a different signal than the one it is applied to.
    Real two-channel content (different audio per channel, not one mono
    source duplicated to both -- which is all ``_tone``'s stereo fixture
    ever was) is exactly where that shows up. The fix has to put the same
    resample/downmix ahead of ``loudnorm`` in *both* the measure pass and
    the write pass, so this has to land in the same 1.5 dB band
    tests/test_voice_upload.py already holds every other upload to.
    """
    settings.voice_clean = True
    source = _decorrelated_stereo(settings, tmp_path / "panned.wav")
    probed = _probe(settings, source)
    assert not probed["mono"], "fixture must be genuinely two-channel"

    target = tmp_path / "out.mp3"
    seconds, report = ingest_narration(source, target, settings)

    after = _loudness(settings, target)
    assert abs(after - UPLOAD_LOUDNESS) <= 1.5, (
        f"normalised to {after} LUFS, wanted within 1.5 dB of "
        f"{UPLOAD_LOUDNESS}")
    assert report.filters_applied
    assert report.cleanup_abandoned is False


def test_cleanup_on_makes_a_noisy_upload_measurably_cleaner_than_the_raw(
        settings, tmp_path):
    """The written file's noise floor moved, and the report says cleanup
    ran -- not asserted on the filter string, measured off the file."""
    settings.voice_clean = True
    source = _noisy_tone(settings, tmp_path / "noisy.wav")
    raw_noise = _mean_volume_above(settings, source, 4000)

    target = tmp_path / "out.mp3"
    seconds, report = ingest_narration(source, target, settings)

    clean_noise = _mean_volume_above(settings, target, 4000)
    # A smaller margin than tests/test_voice_cleanup.py uses: that file
    # measures the cleanup filter's own WAV output directly, this one
    # measures after loudnorm's gain and a lossy mp3 encode have also run,
    # both of which raise the noise floor back up somewhat.
    assert clean_noise < raw_noise - 2.5, (
        f"noise floor only moved {raw_noise:.1f} -> {clean_noise:.1f} dB, "
        f"wanted at least 2.5 dB of reduction")
    assert report.filters_applied
    assert abs(seconds - report.seconds_after) < 0.01


def test_cleanup_off_gives_the_same_output_as_before_this_feature(
        settings, tmp_path):
    """``voice_clean = False`` behaves exactly as ``ingest_narration`` did
    before cleanup existed: same loudness band, no trimming, and the
    report's empty ``filters_applied`` is the honest record of that."""
    settings.voice_clean = False
    source = _tone(settings, tmp_path / "quiet.wav", seconds=3.0,
                   volume="-30dB")
    target = tmp_path / "out.mp3"

    seconds, report = ingest_narration(source, target, settings)

    after = _loudness(settings, target)
    assert abs(after - UPLOAD_LOUDNESS) <= 1.5
    assert report.filters_applied == ""
    assert report.cleanup_abandoned is False
    assert abs(report.seconds_before - report.seconds_after) < 0.1
    assert abs(seconds - 3.0) < 0.1


def test_a_silent_upload_is_still_refused_with_cleanup_on(settings, tmp_path):
    settings.voice_clean = True
    source = tmp_path / "mute.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "3",
         str(source)], check=True, capture_output=True)
    with pytest.raises(UploadRejected, match="silen"):
        ingest_narration(source, tmp_path / "out.mp3", settings)


def test_loud_noise_with_no_speech_is_accepted_though_cleanup_would_silence_it(
        settings, tmp_path):
    """The subtle part of the refusal ordering, direction one: a recording
    that is nothing but noise is loud enough raw to pass the silence gate,
    even though the denoiser and ``silenceremove`` together quiet it to
    nothing once cleanup actually runs. Judging the *cleaned* signal
    instead would refuse it as silent -- the gate has to run on the raw
    upload for this not to happen.
    """
    settings.voice_clean = True
    source = tmp_path / "noise.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi",
         "-i", "anoisesrc=color=white:amplitude=0.01:duration=3",
         "-ar", "44100", "-ac", "1", str(source)],
        check=True, capture_output=True)

    raw_loudness = _loudness(settings, source)
    assert raw_loudness > SILENCE_LUFS, "fixture must not be silent raw"
    cleaned_loudness = _cleaned_loudness_preview(settings, source, tmp_path)
    assert cleaned_loudness <= SILENCE_LUFS, (
        "fixture is supposed to be silenced by cleanup, or this test "
        "proves nothing about the ordering")

    # No UploadRejected: judged on the raw upload, this passes. Cleanup
    # having nothing left to normalise falls back to the raw signal for
    # the write, which the report says honestly: cleanup was requested
    # (filters_applied names it) but abandoned (cleanup_abandoned), not
    # simply off.
    seconds, report = ingest_narration(source, tmp_path / "out.mp3", settings)
    assert seconds > 0
    assert report.filters_applied
    assert report.cleanup_abandoned is True


def test_a_quiet_real_recording_is_not_refused_after_cleanup_erases_it(
        settings, tmp_path):
    """The subtle part of the refusal ordering, direction two: a soft but
    real recording (a single tone standing in for quietly-spoken
    narration) is loud enough raw to pass the silence gate, even though
    cleanup's ``silenceremove`` trims the whole thing away once it runs --
    exactly the loss the ordering exists to protect a real recording from.
    """
    settings.voice_clean = True
    source = _tone(settings, tmp_path / "soft.wav", seconds=3.0,
                   volume="-30dB")

    raw_loudness = _loudness(settings, source)
    assert raw_loudness > SILENCE_LUFS, "fixture must not be silent raw"
    cleaned_loudness = _cleaned_loudness_preview(settings, source, tmp_path)
    assert cleaned_loudness <= SILENCE_LUFS, (
        "fixture is supposed to be erased by cleanup, or this test proves "
        "nothing about the ordering")

    seconds, report = ingest_narration(source, tmp_path / "out.mp3", settings)
    assert seconds > 0
    assert report.filters_applied
    assert report.cleanup_abandoned is True


def test_cleanup_off_and_cleanup_abandoned_are_distinguishable_reports(
        settings, tmp_path):
    """Both leave the written file at the raw signal's normalised loudness
    and both could otherwise look identical on the report -- but a caller
    (Task 3's review board) has to be able to tell "cleanup was off" from
    "cleanup was attempted and abandoned because it emptied the file"."""
    settings.voice_clean = False
    off_source = _tone(settings, tmp_path / "off.wav", seconds=3.0)
    _, off_report = ingest_narration(off_source, tmp_path / "off.mp3",
                                     settings)

    settings.voice_clean = True
    abandoned_source = tmp_path / "abandoned.wav"
    abandoned_source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi",
         "-i", "anoisesrc=color=white:amplitude=0.01:duration=3",
         "-ar", "44100", "-ac", "1", str(abandoned_source)],
        check=True, capture_output=True)
    _, abandoned_report = ingest_narration(
        abandoned_source, tmp_path / "abandoned.mp3", settings)

    assert off_report.filters_applied == ""
    assert off_report.cleanup_abandoned is False
    assert abandoned_report.filters_applied
    assert abandoned_report.cleanup_abandoned is True
    # the pair together is the actual requirement: same falsy-vs-not shape
    # on filters_applied would read as "cleanup was off" either way unless
    # cleanup_abandoned also differs.
    assert ((off_report.filters_applied, off_report.cleanup_abandoned)
           != (abandoned_report.filters_applied,
               abandoned_report.cleanup_abandoned))


def test_report_seconds_bracket_a_real_trim(settings, tmp_path):
    """``seconds_before``/``seconds_after`` are not just echoed inputs --
    they bracket the internal-silence trim cleanup actually performs, and
    match what the files themselves measure."""
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gap.wav")
    target = tmp_path / "out.mp3"
    real_before = probe_duration(source, settings.ffmpeg)

    seconds, report = ingest_narration(source, target, settings)

    real_after = probe_duration(target, settings.ffmpeg)
    assert abs(report.seconds_before - real_before) < 0.01
    assert abs(report.seconds_after - real_after) < 0.01
    assert abs(seconds - real_after) < 0.01
    # the internal 2s gap, capped to well under a second, is a real trim
    assert report.seconds_before - report.seconds_after > 1.0
    assert report.filters_applied


def test_report_loudness_matches_what_the_written_file_measures(
        settings, tmp_path):
    settings.voice_clean = True
    source = _tone(settings, tmp_path / "in.wav", seconds=3.0,
                   volume="-20dB")
    target = tmp_path / "out.mp3"

    seconds, report = ingest_narration(source, target, settings)

    real_after = _loudness(settings, target)
    assert abs(report.loudness_after - real_after) < 0.5


# --- final whole-feature review ---------------------------------------------


def test_a_failed_reupload_does_not_destroy_the_raw_the_beat_still_points_at(
        settings, tmp_path, monkeypatch):
    """Important 1: a good upload's raw copy must survive a second upload
    that clears every refusal (it decodes, it is not silent) but then
    fails during its own measure/write pass -- the documented
    ``silenceremove``+resample ffmpeg assertion, or a full disk, are the
    real-world versions of this. ``shutil.copyfile`` onto the deterministic
    per-beat ``raw_target`` used to run before that pass, so a failure
    there left the beat's stored raw already overwritten with the bytes of
    the take that was about to be refused.
    """
    settings.voice_clean = True
    raw_target = tmp_path / "raw" / "b0-upload.raw.wav"
    target = tmp_path / "out.mp3"

    first_source = _tone(settings, tmp_path / "first.wav", seconds=3.0,
                        hz=220)
    ingest_narration(first_source, target, settings, raw_target=raw_target)
    good_raw_bytes = raw_target.read_bytes()
    good_target_bytes = target.read_bytes()
    assert good_raw_bytes == first_source.read_bytes()

    second_source = _tone(settings, tmp_path / "second.wav", seconds=3.0,
                         hz=440)
    assert second_source.read_bytes() != first_source.read_bytes(), \
        "the fixture must actually be different bytes from the first take"

    import engine.media.voice as voice_mod
    real_run = voice_mod.subprocess.run

    def _write_pass_blows_up(args, **kwargs):
        # Only the write pass's ffmpeg invocation drops video with -vn;
        # the measure passes and probe_duration never do. Simulates a
        # write-pass failure (a full disk, the documented assertion)
        # without faking away any audio measurement.
        if "-vn" in args:
            raise OSError("simulated: no space left on device")
        return real_run(args, **kwargs)

    monkeypatch.setattr(voice_mod.subprocess, "run", _write_pass_blows_up)

    with pytest.raises(OSError):
        ingest_narration(second_source, target, settings,
                        raw_target=raw_target)

    assert raw_target.read_bytes() == good_raw_bytes, (
        "the second upload's bytes clobbered the beat's stored raw before "
        "its own write pass was known to have succeeded")
    assert target.read_bytes() == good_target_bytes, (
        "the beat's cleaned audio was touched by a write that failed")
    # No orphan scratch file left behind under work_dir either.
    assert not raw_target.with_suffix(raw_target.suffix + ".part").exists()


def test_a_failed_first_upload_leaves_no_orphan_raw_behind(
        settings, tmp_path, monkeypatch):
    """The other half of Important 1: when there was no previous raw to
    protect, a failed write must not leave a stray ``.part`` copy sitting
    under ``work_dir`` either."""
    settings.voice_clean = True
    raw_target = tmp_path / "raw" / "b0-upload.raw.wav"
    target = tmp_path / "out.mp3"
    source = _tone(settings, tmp_path / "in.wav", seconds=3.0)

    import engine.media.voice as voice_mod
    real_run = voice_mod.subprocess.run

    def _write_pass_blows_up(args, **kwargs):
        if "-vn" in args:
            raise OSError("simulated: no space left on device")
        return real_run(args, **kwargs)

    monkeypatch.setattr(voice_mod.subprocess, "run", _write_pass_blows_up)

    with pytest.raises(OSError):
        ingest_narration(source, target, settings, raw_target=raw_target)

    assert not raw_target.exists()
    assert not raw_target.with_suffix(raw_target.suffix + ".part").exists()
    assert not target.exists()


def test_cleanup_off_a_decorrelated_stereo_upload_still_lands_in_the_band(
        settings, tmp_path):
    """Important 2: with cleanup off, the write pass used to pass only
    ``loudnorm`` through ``-af`` and do the downmix with the output-side
    ``-ar``/``-ac`` flags *after* the filter graph, so loudnorm's linear
    gain was computed for the stereo signal and applied to a mono downmix
    of it -- the same defect fix round 1 corrected for the cleaned path.
    ``_tone``'s stereo fixture duplicates one channel to both, which is
    why this never showed up here before; genuinely decorrelated stereo
    (``_decorrelated_stereo``) measured 3.1 dB off target on the pre-fix
    code. The controller ruling: put the resample/downmix ahead of
    ``loudnorm`` unconditionally, in both passes, so the off path gets the
    same treatment as the on path -- this is the model test for it, the
    same 1.5 dB band ``test_cleanup_on_a_decorrelated_stereo_upload_
    still_lands_in_the_band`` already holds the on path to.
    """
    settings.voice_clean = False
    source = _decorrelated_stereo(settings, tmp_path / "panned.wav")
    probed = _probe(settings, source)
    assert not probed["mono"], "fixture must be genuinely two-channel"

    target = tmp_path / "out.mp3"
    seconds, report = ingest_narration(source, target, settings)

    after = _loudness(settings, target)
    assert abs(after - UPLOAD_LOUDNESS) <= 1.5, (
        f"normalised to {after} LUFS, wanted within 1.5 dB of "
        f"{UPLOAD_LOUDNESS}")
    assert report.filters_applied == ""
    assert report.cleanup_abandoned is False


def test_loudness_before_means_the_raw_signal_in_every_cleanup_state(
        settings, tmp_path):
    """Minor 3: ``loudness_before`` used to be whichever pipeline's own
    ``input_i`` ended up driving the write -- the cleaned-and-resampled
    signal's loudness when cleanup ran, the raw signal's when it did not
    -- so the same beat toggled between cleaned and raw reported two
    numbers that were not comparable, even though the panel presents the
    delta as "what cleanup did to the loudness". It has to mean the same
    thing -- the upload's own loudness before this call did anything to it
    -- in every state.
    """
    source = _noisy_tone(settings, tmp_path / "in.wav")
    raw_loudness = _loudness(settings, source)

    _, cleaned_report = ingest_narration(
        source, tmp_path / "on.mp3", settings, clean=True)
    _, off_report = ingest_narration(
        source, tmp_path / "off.mp3", settings, clean=False)

    assert cleaned_report.loudness_before == off_report.loudness_before
    assert abs(cleaned_report.loudness_before - raw_loudness) < 0.5

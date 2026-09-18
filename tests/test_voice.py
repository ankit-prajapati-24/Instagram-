from pathlib import Path

import pytest

from engine.media.voice import (caption_timings, distribute_words,
                                largest_silence_gap, offsets_to_timings,
                                word_alignment)
from tests.factories import make_plan


def test_real_wordboundary_events_are_used_when_present():
    raw = [{"type": "WordBoundary", "offset": 0, "duration": 5_000_000,
            "text": "Roopkund"},
           {"type": "WordBoundary", "offset": 5_000_000,
            "duration": 3_000_000, "text": "jheel"}]
    timings = offsets_to_timings(raw)
    assert [t.word for t in timings] == ["Roopkund", "jheel"]
    assert timings[0].start == pytest.approx(0.0)
    assert timings[0].end == pytest.approx(0.5)
    assert timings[1].end == pytest.approx(0.8)


def test_sentenceboundary_is_expanded_into_words():
    """edge-tts 7.2.8 only ever emits SentenceBoundary; verified live."""
    raw = [{"type": "SentenceBoundary", "offset": 1_000_000,
            "duration": 32_000_000,
            "text": "Roopkund jheel mein kankaal mile"}]
    timings = offsets_to_timings(raw)
    assert [t.word for t in timings] == ["Roopkund", "jheel", "mein",
                                         "kankaal", "mile"]
    assert timings[0].start == pytest.approx(0.1)
    assert timings[-1].end == pytest.approx(3.3)


def test_two_sentences_stay_in_their_own_spans():
    raw = [{"type": "SentenceBoundary", "offset": 0, "duration": 10_000_000,
            "text": "ek do"},
           {"type": "SentenceBoundary", "offset": 10_000_000,
            "duration": 10_000_000, "text": "teen chaar"}]
    timings = offsets_to_timings(raw)
    assert timings[1].end == pytest.approx(1.0)
    assert timings[2].start == pytest.approx(1.0)


def test_audio_only_stream_yields_no_timings():
    assert offsets_to_timings([{"type": "audio"}]) == []


def test_distribute_words_is_contiguous_and_fills_the_span():
    timings = distribute_words("Bhumadhya Saagar se the", 2.0, 6.0)
    assert timings[0].start == pytest.approx(2.0)
    assert timings[-1].end == pytest.approx(6.0)
    for a, b in zip(timings, timings[1:]):
        assert b.start == pytest.approx(a.end)


def test_longer_words_get_more_time():
    timings = distribute_words("ye Bhumadhya", 0.0, 4.0)
    short, long = timings[0], timings[1]
    assert (long.end - long.start) > (short.end - short.start)


def test_distribute_handles_degenerate_input():
    assert distribute_words("", 0.0, 3.0) == []
    assert distribute_words("kuch", 3.0, 3.0) == []
    assert distribute_words("kuch", 5.0, 1.0) == []


def test_caption_timings_start_at_zero_for_the_beat():
    timings = caption_timings("Sab ek hi samay par mare the", 4.4)
    assert timings[0].start == pytest.approx(0.0)
    assert timings[-1].end == pytest.approx(4.4)


def test_word_alignment_measures_caption_coverage():
    plan = make_plan(beats=2, measured=4.0)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, 4.0)
    assert word_alignment(plan) == pytest.approx(1.0)

    plan.script.beats[0].words = []
    assert word_alignment(plan) < 1.0


def test_interpolated_timings_report_no_silence_gap():
    plan = make_plan(beats=3, measured=4.0)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, 4.0)
    assert largest_silence_gap(plan) == pytest.approx(0.0)


# --- engine dispatch --------------------------------------------------------
# Piper is the default engine; edge-tts is the fallback. A voice problem must
# never cost an approved script, so a Piper failure switches engines and
# carries on rather than raising.

class FakeSettings:
    def __init__(self, engine="piper", tmp=None):
        from engine.config import Settings
        base = Settings()
        self.voice_engine = engine
        self.ffmpeg = base.ffmpeg
        self.voice = base.voice
        self.voice_rate = base.voice_rate
        self.voice_pitch = base.voice_pitch
        self.piper_voice = "pratham"
        self.piper_models_dir = tmp
        self.piper_length_scale = 1.12
        self.piper_noise_scale = 0.667
        self.piper_noise_w = 0.9
        self.piper_sentence_silence = 0.25
        self.voice_process = True


def test_piper_is_the_default_engine():
    from engine.config import Settings
    assert Settings().voice_engine == "piper"
    assert Settings().piper_voice == "pratham"


def test_piper_length_scale_defaults_above_one():
    """Piper reads ~40% faster than edge; at 1.0 a 12-beat script lands
    near 33s and fails the 38-52s duration check."""
    from engine.config import Settings
    assert Settings().piper_length_scale > 1.0


def test_piper_failure_falls_back_to_edge_for_the_whole_plan(monkeypatch,
                                                             tmp_path):
    from engine.media import voice as mod
    from engine.media.piper_voice import PiperUnavailable
    from tests.factories import make_plan

    calls = {"piper": 0, "edge": 0}

    def bad_piper(text, target, settings):
        calls["piper"] += 1
        raise PiperUnavailable("no model")

    def fake_edge(text, target, settings):
        calls["edge"] += 1
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"x")
        return 3

    monkeypatch.setattr(mod, "synth_beat_piper", bad_piper)
    monkeypatch.setattr(mod, "synth_beat_edge", fake_edge)
    monkeypatch.setattr(mod, "probe_duration", lambda *a, **k: 4.0)

    plan = make_plan(beats=5, measured=None)
    seen = []
    mod.synth_plan(plan, tmp_path, FakeSettings(tmp=tmp_path),
                   progress=lambda *a: seen.append(a[-1]))

    # Piper is tried once, then abandoned for the rest of the plan.
    assert calls["piper"] == 1
    assert calls["edge"] == 5
    assert all(b.measured_seconds == 4.0 for b in plan.script.beats)
    assert "piper unavailable" in seen[0]
    assert seen[-1] == "edge"


def test_engine_edge_never_calls_piper(monkeypatch, tmp_path):
    from engine.media import voice as mod
    from tests.factories import make_plan

    def boom(*a, **k):
        raise AssertionError("piper must not be called when engine=edge")

    monkeypatch.setattr(mod, "synth_beat_piper", boom)
    monkeypatch.setattr(mod, "synth_beat_edge",
                        lambda t, target, s: (Path(target).parent.mkdir(
                            parents=True, exist_ok=True),
                            Path(target).write_bytes(b"x"), 2)[-1])
    monkeypatch.setattr(mod, "probe_duration", lambda *a, **k: 3.0)

    plan = make_plan(beats=3, measured=None)
    mod.synth_plan(plan, tmp_path, FakeSettings(engine="edge", tmp=tmp_path))
    assert all(b.spoken_words == 2 for b in plan.script.beats)


def test_caption_timings_are_filled_even_though_piper_reports_none(
        monkeypatch, tmp_path):
    """Piper returns no spans, so captions must still come from the
    measured beat span or the burned text would have no timing at all."""
    from engine.media import voice as mod
    from tests.factories import make_plan

    monkeypatch.setattr(mod, "synth_beat_piper",
                        lambda t, target, s: (Path(target).parent.mkdir(
                            parents=True, exist_ok=True),
                            Path(target).write_bytes(b"x"), 0)[-1])
    monkeypatch.setattr(mod, "probe_duration", lambda *a, **k: 4.4)

    plan = make_plan(beats=3, measured=None)
    mod.synth_plan(plan, tmp_path, FakeSettings(tmp=tmp_path))

    for beat in plan.script.beats:
        assert beat.spoken_words == 0
        assert beat.words, "captions must still be timed"
        assert beat.words[-1].end == pytest.approx(4.4)


def test_a_piper_failure_writes_the_whole_error_to_a_log(monkeypatch,
                                                         tmp_path):
    """The first diagnosis of a real failure was impossible: stderr was
    truncated to its last 300 characters, leaving only a traceback tail
    from Python's wave module and no command, text or exit code."""
    import subprocess
    from engine.media import piper_voice

    class Result:
        returncode = 1
        stdout = b""
        stderr = ("Traceback (most recent call last):\n"
                  "  File \"x\", line 1\n"
                  "ValueError: something specific went wrong\n"
                  + "padding\n" * 200).encode()

    monkeypatch.setattr(piper_voice, "ensure_model",
                        lambda v, d: tmp_path / "v.onnx")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())

    settings = FakeSettings(tmp=tmp_path)
    with pytest.raises(piper_voice.PiperUnavailable) as exc:
        piper_voice.synth("कुछ", tmp_path / "out" / "b1.mp3", settings)

    log = tmp_path / "out" / "piper-error.log"
    assert log.exists(), "the full error must be written somewhere"
    body = log.read_text(encoding="utf-8")
    assert "command:" in body
    assert "returncode : 1" in body
    assert "कुछ" in body, "the input text must be recorded"
    assert "something specific went wrong" in body

    # and the exception itself must name the exit code and the log
    assert "exited 1" in str(exc.value)
    assert "piper-error.log" in str(exc.value)


def test_piper_is_given_an_explicit_utf8_stdin_encoding(monkeypatch,
                                                        tmp_path):
    """The root cause of Piper failing from the panel but not a terminal.

    Piper's Python decodes stdin with the process locale. On cp1252 the
    third byte of the Devanagari candrabindu became a lone surrogate, espeak
    raised UnicodeEncodeError, and all that escaped was a wave.Error about
    channels. Manual tests passed only because the shell happened to export
    PYTHONIOENCODING, which the child inherited.
    """
    import subprocess
    from engine.media import piper_voice

    seen = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **kwargs):
        # synth() shells out twice: piper, then ffmpeg. Only the first is
        # under test here.
        if "piper" in cmd:
            seen.update(kwargs)
            out = Path(cmd[cmd.index("-f") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"RIFF")
        else:
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"ID3")
        return Result()

    monkeypatch.setattr(piper_voice, "ensure_model",
                        lambda v, d: tmp_path / "v.onnx")
    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        piper_voice.synth("कहाँ", tmp_path / "b1.mp3",
                          FakeSettings(tmp=tmp_path))
    except piper_voice.PiperUnavailable:
        pass  # the ffmpeg step is stubbed away; the env is what matters

    assert seen.get("env"), "the child must get an explicit environment"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen["env"]["PYTHONUTF8"] == "1"

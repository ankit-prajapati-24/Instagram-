"""Real word alignment, measured from the synthesised audio.

Everything in here exists because interpolation gets one thing systematically
wrong: it spreads the caption across the *file*, and a Piper beat's file is
longer than its speech. ``--sentence-silence 0.25`` plus the loudnorm tail put
0.35-0.40s of silence after the last phoneme, so every word after the first
drifts late, and the last one lands ~0.4s behind the voice.

Measured on a real ten-beat script, against faster-whisper's own per-word
timings as ground truth on the five beats whose counts agreed: interpolating
across the file errs by mean 0.135s, interpolating inside the measured speech
span by mean 0.116s, and Whisper's timings applied by index are the ground
truth. See ``engine/media/align.py`` for the per-beat table, including the one
beat where the speech span is worse.

That is the whole shape of the feature, and the reason the count-mismatch
fallback is "interpolate inside the measured speech span" rather than "give
up and interpolate across the file". It is polish: the per-word difference
between interpolated and aligned over those ten beats is mean 0.151s, worst
0.375s.
"""

import pytest

from engine.contract import WordTiming
from engine.media.align import (SPEECH_FLOOR, Alignment, WhisperAligner,
                                build_aligner)
from tests.factories import make_plan


# --- a stand-in for faster-whisper ------------------------------------------
# Shaped exactly like the real thing: model_factory() -> model, and
# model.transcribe(path, word_timestamps=True, language=...) -> (segments,
# info), each segment carrying .words with .start/.end/.word. The policy tests
# below drive the real WhisperAligner through this, so the mapping code under
# test is the code that runs in production.

class FakeWord:
    def __init__(self, start, end, word="x"):
        self.start, self.end, self.word = start, end, word


class FakeSegment:
    def __init__(self, words):
        self.words = words


class FakeInfo:
    language = "ur"
    language_probability = 1.0


class FakeModel:
    def __init__(self, spans, calls=None):
        self.spans = spans
        self.calls = calls if calls is not None else []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        words = [FakeWord(s, e) for s, e in self.spans]
        return ([FakeSegment(words)] if words else []), FakeInfo()


def aligner_for(spans, **kwargs):
    loads = []

    def factory():
        loads.append(1)
        return FakeModel(spans)

    a = WhisperAligner(model_factory=factory, **kwargs)
    a.loads = loads
    return a


CAPTION = "Koi nahi jaanta ye log kaun the"      # 7 words
SEVEN = [(0.0, 0.28), (0.28, 0.46), (0.46, 0.96), (0.96, 1.10),
         (1.10, 1.42), (1.42, 1.72), (1.72, 2.00)]
DURATION = 2.40


# --- the switch -------------------------------------------------------------

def test_alignment_is_off_by_default():
    """~40MB of model on first use. A clean clone must not hit that by
    surprise mid-render, and the win is polish, not correctness."""
    from tests.factories import shipped_settings

    assert shipped_settings().align_words is False


def test_the_switch_turns_it_on_the_way_video_grade_turns_off(monkeypatch):
    from engine.config import Settings

    monkeypatch.setenv("RAHASYA_ALIGN", "1")
    assert Settings().align_words is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("RAHASYA_ALIGN", off)
        assert Settings().align_words is False


def test_hindi_is_forced_rather_than_detected():
    """Auto-detect costs a whole extra pass over the audio for nothing:
    measured, forcing hi halved transcription time (8.4s -> 5.7s over four
    beats) and changed not one timing. It does NOT change the script --
    Whisper writes this voice in Urdu/Arabic either way."""
    from tests.factories import shipped_settings

    assert shipped_settings().align_language == "hi"


def test_the_model_download_is_documented_where_someone_will_see_it():
    """~40MB appearing on a fresh clone's first render is the surprise this
    switch is off to avoid; the docs are the other half of that."""
    from pathlib import Path

    env = Path(".env.example").read_text(encoding="utf-8")
    for key in ("RAHASYA_ALIGN", "RAHASYA_ALIGN_MODEL",
                "RAHASYA_ALIGN_COMPUTE", "RAHASYA_ALIGN_DEVICE",
                "RAHASYA_ALIGN_LANG"):
        assert key in env, f"{key} is undocumented"

    assert "faster-whisper" in Path("requirements.txt").read_text(
        encoding="utf-8")

    setup = Path("SETUP.md").read_text(encoding="utf-8")
    assert "RAHASYA_ALIGN" in setup
    assert "40 MB" in setup, "the first-use download must be documented"


def test_build_aligner_returns_nothing_when_the_switch_is_off():
    class S:
        align_words = False

    assert build_aligner(S()) is None


def test_build_aligner_carries_the_configured_model():
    class S:
        align_words = True
        align_model = "tiny"
        align_compute = "int8"
        align_language = "hi"
        align_device = "cpu"

    aligner = build_aligner(S())
    assert aligner.model_size == "tiny"
    assert aligner.language == "hi"


# --- counts agree -----------------------------------------------------------

def test_matching_counts_take_whispers_timings_by_index():
    """Whisper writes this voice in Urdu/Arabic script, so its tokens can
    never be matched to Roman caption words by text. Position is the only
    usable correspondence."""
    aligner = aligner_for(SEVEN)
    result = aligner.align("b.mp3", CAPTION, DURATION)

    assert result.source == "whisper"
    assert [w.word for w in result.words] == CAPTION.split()
    # Word 3 ("ye") starts where Whisper heard it, not where character
    # weighting guessed (1.25s).
    assert result.words[3].start == pytest.approx(0.96)


def test_timings_are_contiguous_and_start_at_zero():
    """captions.py emits ``\\k<centis>`` durations, which libass accumulates
    from the start of the dialogue line -- it never reads word.start. A gap
    between two words would therefore pull every later word early. So the
    aligner absorbs gaps into the preceding word and pins the first start to
    0.0, exactly the contract distribute_words already satisfies."""
    aligner = aligner_for([(0.30, 0.50), (0.90, 1.20), (1.60, 2.00),
                           (2.00, 2.10), (2.10, 2.20), (2.20, 2.30),
                           (2.30, 2.35)])
    words = aligner.align("b.mp3", CAPTION, DURATION).words

    assert words[0].start == pytest.approx(0.0)
    for a, b in zip(words, words[1:]):
        assert b.start == pytest.approx(a.end)
        assert b.start > a.start


@pytest.mark.parametrize("spans,why", [
    (SEVEN, "counts agree"),
    ([(0.0, 0.3), (0.3, 0.8), (0.8, 2.0)], "counts disagree"),
    ([], "nothing transcribed"),
])
def test_every_path_gives_one_timing_per_caption_word_inside_the_span(
        spans, why):
    """The render and the captions both assume this, whatever happened."""
    words = aligner_for(spans).align("b.mp3", CAPTION, DURATION).words

    assert len(words) == len(CAPTION.split()), why
    assert words[0].start >= 0.0
    assert words[-1].end <= DURATION + 1e-9
    for a, b in zip(words, words[1:]):
        assert b.start >= a.end - 1e-9


# --- counts disagree --------------------------------------------------------

def test_mismatched_counts_interpolate_inside_the_measured_speech_span():
    """Two of four real beats came back with the wrong token count: Whisper
    split "Roopkund" into two and merged "ek hi" into one. Index mapping is
    then known to be wrong, but the span it heard speech in is still a
    measurement -- and that span is where most of the error was."""
    from engine.media.voice import distribute_words

    aligner = aligner_for([(0.0, 0.5), (0.5, 1.2), (1.2, 2.00)])
    result = aligner.align("b.mp3", CAPTION, DURATION)

    assert result.source == "whisper-span"
    assert "3" in result.detail and "7" in result.detail
    assert result.words == distribute_words(CAPTION, 0.0, 2.00)
    # and that is measurably better than spreading over the whole 2.40s file
    assert result.words[-1].end == pytest.approx(2.00)


def test_a_transcription_that_missed_most_of_the_beat_is_refused():
    """A span is only trustworthy if Whisper heard the whole beat. Real ones
    land at 0.83-0.87 of the file; anything under SPEECH_FLOOR would squeeze
    the caption into the first half of the audio."""
    short = SPEECH_FLOOR * DURATION - 0.1
    aligner = aligner_for([(0.0, short)])
    result = aligner.align("b.mp3", CAPTION, DURATION)

    assert result.source == "interpolated"
    assert result.words[-1].end == pytest.approx(DURATION)


def test_out_of_order_or_zero_width_whisper_output_drops_to_the_span():
    aligner = aligner_for([(0.0, 0.5), (0.9, 1.0), (0.4, 0.6), (1.2, 1.4),
                           (1.4, 1.6), (1.6, 1.8), (1.8, 2.0)])
    assert aligner.align("b.mp3", CAPTION, DURATION).source == "whisper-span"

    flat = aligner_for([(0.0, 0.5), (0.5, 0.5), (0.5, 1.0), (1.0, 1.2),
                        (1.2, 1.4), (1.4, 1.6), (1.6, 2.0)])
    assert flat.align("b.mp3", CAPTION, DURATION).source == "whisper-span"


def test_timings_that_run_past_the_beat_are_refused():
    over = [(a, b) for a, b in SEVEN[:-1]] + [(1.72, DURATION + 0.9)]
    result = aligner_for(over).align("b.mp3", CAPTION, DURATION)
    assert result.words[-1].end <= DURATION + 1e-9


# --- failure is never fatal -------------------------------------------------

def test_a_missing_faster_whisper_falls_back_instead_of_raising():
    def explode():
        raise ImportError("No module named 'faster_whisper'")

    aligner = WhisperAligner(model_factory=explode)
    result = aligner.align("b.mp3", CAPTION, DURATION)

    assert result.source == "interpolated"
    assert "faster_whisper" in result.detail
    assert len(result.words) == len(CAPTION.split())


def test_a_broken_model_is_only_tried_once():
    """A model that cannot load cannot load for beat two either, and paying
    an eight-second import per beat to rediscover that is the cost this
    feature was explicitly not allowed to add."""
    tries = []

    def explode():
        tries.append(1)
        raise RuntimeError("no such file")

    aligner = WhisperAligner(model_factory=explode)
    for _ in range(5):
        assert aligner.align("b.mp3", CAPTION, DURATION).source \
            == "interpolated"
    assert len(tries) == 1


def test_a_transcription_that_throws_falls_back():
    class Boom:
        def transcribe(self, *a, **k):
            raise RuntimeError("ctranslate2 said no")

    aligner = WhisperAligner(model_factory=Boom)
    result = aligner.align("b.mp3", CAPTION, DURATION)
    assert result.source == "interpolated"
    assert "ctranslate2 said no" in result.detail


def test_a_caption_with_no_words_is_not_an_error():
    assert aligner_for(SEVEN).align("b.mp3", "   ", DURATION).words == []


# --- once per run, not once per beat ----------------------------------------

class AlignSettings:
    """FakeSettings + the alignment switch, for synth_plan."""

    def __init__(self, tmp, on=True):
        from engine.config import Settings
        base = Settings()
        self.voice_engine = "piper"
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
        # Read by edge_trim_filter, which speak_beat runs over every
        # synthesised beat.
        self.voice_trim_edges = base.voice_trim_edges
        self.voice_silence_threshold = base.voice_silence_threshold
        self.align_words = on
        self.align_model = "base"
        self.align_compute = "int8"
        self.align_language = "hi"
        self.align_device = "cpu"


def _stub_synthesis(monkeypatch, mod, seconds=2.40):
    from pathlib import Path

    def fake_piper(text, target, settings):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"x")
        return 0

    monkeypatch.setattr(mod, "synth_beat_piper", fake_piper)
    monkeypatch.setattr(mod, "probe_duration", lambda *a, **k: seconds)


def test_the_model_is_loaded_once_per_run_not_once_per_beat(monkeypatch,
                                                            tmp_path):
    """Eight seconds times ten beats is the whole reason this is a field on
    one aligner object rather than a call in the beat loop."""
    from engine.media import align as align_mod
    from engine.media import voice as mod

    _stub_synthesis(monkeypatch, mod)
    loads = []

    def factory(*a, **k):
        loads.append(1)
        return FakeModel(SEVEN)

    monkeypatch.setattr(align_mod, "load_whisper", factory)

    plan = make_plan(beats=10, measured=None)
    mod.synth_plan(plan, tmp_path, AlignSettings(tmp_path))

    assert len(loads) == 1, f"loaded {len(loads)} times"
    assert all(b.words for b in plan.script.beats)


def test_the_aligner_is_never_built_when_the_switch_is_off(monkeypatch,
                                                           tmp_path):
    from engine.media import align as align_mod
    from engine.media import voice as mod

    _stub_synthesis(monkeypatch, mod)

    def boom(*a, **k):
        raise AssertionError("faster-whisper must not load when off")

    monkeypatch.setattr(align_mod, "load_whisper", boom)

    plan = make_plan(beats=3, measured=None)
    mod.synth_plan(plan, tmp_path, AlignSettings(tmp_path, on=False))
    assert all(b.word_timing_source == "interpolated"
               for b in plan.script.beats)


# --- the fallback is visible ------------------------------------------------

def test_the_beat_records_which_timing_source_it_got(monkeypatch, tmp_path):
    """Mirrors Beat.voice_engine and Clip.provider: a run that silently
    interpolated must not look like a run that aligned."""
    from engine.media import align as align_mod
    from engine.media import voice as mod

    _stub_synthesis(monkeypatch, mod)
    monkeypatch.setattr(align_mod, "load_whisper",
                        lambda *a, **k: FakeModel(SEVEN))

    plan = make_plan(beats=4, measured=None)
    counts = mod.synth_plan(plan, tmp_path, AlignSettings(tmp_path))

    # factories.HINGLISH beat 1 is the seven-word line; the others are not.
    sources = {b.word_timing_source for b in plan.script.beats}
    assert sources <= {"whisper", "whisper-span", "interpolated"}
    assert "whisper" in sources
    assert sum(counts.values()) == 4
    assert counts["whisper"] >= 1


def test_the_progress_line_names_the_timing_source(monkeypatch, tmp_path):
    from engine.media import align as align_mod
    from engine.media import voice as mod

    _stub_synthesis(monkeypatch, mod)
    monkeypatch.setattr(align_mod, "load_whisper",
                        lambda *a, **k: FakeModel([]))

    plan = make_plan(beats=2, measured=None)
    seen = []
    mod.synth_plan(plan, tmp_path, AlignSettings(tmp_path),
                   progress=lambda *a: seen.append(a[-1]))

    assert all("interpolated" in line for line in seen), seen
    assert all("piper" in line for line in seen)


def test_an_interpolated_run_is_reported_even_though_it_succeeded(
        monkeypatch, tmp_path, capsys):
    """The clip stage prints its provider counts; so does this."""
    from engine.media import align as align_mod
    from engine.media import voice as mod

    _stub_synthesis(monkeypatch, mod)
    monkeypatch.setattr(align_mod, "load_whisper",
                        lambda *a, **k: FakeModel([]))

    mod.synth_plan(make_plan(beats=3, measured=None), tmp_path,
                   AlignSettings(tmp_path))
    err = capsys.readouterr().err
    assert "[align]" in err and "interpolated" in err


# --- the real thing ---------------------------------------------------------
# Three bugs on this branch shipped past a green suite because the test
# mocked the broken thing. Everything above mocks faster-whisper. This one
# does not: real Piper synthesis, real faster-whisper, real ffmpeg.

def _real_alignment_available():
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    from engine.config import settings
    from engine.media import piper_voice
    return piper_voice.available(settings)


real_only = pytest.mark.skipif(
    not _real_alignment_available(),
    reason="needs piper + its voice model and faster-whisper installed")


@real_only
def test_real_faster_whisper_aligns_real_piper_audio(tmp_path):
    """No mocks. ~12s: one Piper beat, one model load, one transcription."""
    from engine.config import settings
    from engine.media.piper_voice import synth
    from engine.media.voice import probe_duration
    from tests.factories import HINGLISH

    voice_text, caption = HINGLISH[1]
    audio = tmp_path / "real.mp3"
    synth(voice_text, audio, settings)
    duration = probe_duration(audio, settings.ffmpeg)
    assert duration > 0.5

    aligner = WhisperAligner(model_size="base", compute_type="int8",
                             language="hi")
    result = aligner.align(audio, caption, duration)

    assert isinstance(result, Alignment)
    assert result.source in {"whisper", "whisper-span"}, (
        f"real alignment fell back: {result.detail}")

    words = result.words
    assert [w.word for w in words] == caption.split(), "one per caption word"
    assert all(isinstance(w, WordTiming) for w in words)

    # monotonic, contiguous, and inside the beat
    assert words[0].start == pytest.approx(0.0)
    assert words[-1].end <= duration + 1e-6
    for a, b in zip(words, words[1:]):
        assert b.start > a.start, "timings must be monotonic"
        assert b.start == pytest.approx(a.end)
        assert a.end > a.start, "no zero-width word"

    # and it must actually differ from the interpolation it replaces:
    # the file's tail silence is what interpolation spends on words.
    from engine.media.voice import caption_timings
    interp = caption_timings(caption, duration)
    assert words[-1].end < interp[-1].end - 0.15, (
        "alignment should end the last word before the file does")


@real_only
def test_the_real_model_survives_a_second_beat_without_reloading(tmp_path):
    """The once-per-run claim, against the real model rather than a fake."""
    from engine.config import settings
    from engine.media.piper_voice import synth
    from engine.media.voice import probe_duration
    from tests.factories import HINGLISH

    aligner = WhisperAligner(model_size="base", compute_type="int8",
                             language="hi")
    for index in (1, 2):
        voice_text, caption = HINGLISH[index]
        audio = tmp_path / f"real{index}.mp3"
        synth(voice_text, audio, settings)
        duration = probe_duration(audio, settings.ffmpeg)
        result = aligner.align(audio, caption, duration)
        assert len(result.words) == len(caption.split())

    assert aligner.model_loads == 1

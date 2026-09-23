"""Real caption word timings, measured from the audio we just synthesised.

Its own module, not more of ``voice.py``, for three reasons: it owns an
optional heavyweight dependency that must stay lazily imported, it owns a
model that lives for the whole run rather than for one call, and it owns a
fallback policy that is the most argued-over part of this feature. ``voice.py``
is about producing audio; this is about measuring it.

What this is and is not
-----------------------

``faster-whisper`` is used as a **forced aligner by position**, never as a
transcriber. Whisper writes this Piper voice in Urdu/Arabic script -- ``اسم``
for ``असम``, ``جیل`` for ``jheel`` -- and the burned caption is Roman Hinglish.
Its *text* is therefore unusable and its *timings* are the whole point. The
only correspondence available is index order.

``language="hi"`` is forced. Measured: it does not change the script (still
Urdu/Arabic) and does not move a single timing, but it skips the detection
pass and roughly halves transcription time -- 8.4s to 5.7s over four real
beats, and language_probability goes 0.83-0.96 to 1.00.

When the counts disagree
------------------------

They disagree often: two of four real beats. Whisper split "Roopkund" into two
tokens and merged "ek hi" into one. Index mapping is then *known* to be wrong,
so it is not used.

But the timings are not the only measurement in the transcription. The span it
heard speech in -- first word start to last word end -- is true no matter how
Whisper segmented it, and interpolating inside that span fixes the one thing
interpolation gets systematically wrong. Piper's ``--sentence-silence 0.25``
plus the loudnorm tail leave 0.25-0.57s of silence at the end of every beat
file (measured across ten real beats: speech ends at 0.73-0.96 of the file),
and interpolating across the file spends caption time on it.

Measured against Whisper's own per-word timings as ground truth, on the five
beats of a real ten-beat script whose counts agreed -- mean and worst error
of each word's start:

    beat   words  over the whole file    inside the speech span
    b0     12     mean 0.149  max 0.337  mean 0.343  max 0.618
    b3     10     mean 0.165  max 0.342  mean 0.078  max 0.210
    b4      6     mean 0.085  max 0.161  mean 0.077  max 0.155
    b7      4     mean 0.162  max 0.366  mean 0.021  max 0.041
    b8      6     mean 0.116  max 0.265  mean 0.061  max 0.126
    all            mean 0.135             mean 0.116

So the span wins on four beats out of five, by two to eight times, and loses
badly on one. b0 is the only line in the script with two commas in it: Piper
pauses at both, character weighting does not know that, and the file's 0.57s
of tail silence was accidentally absorbing those pauses. Removing the tail
removes the accident too. That is luck rather than a principle, and the
average is what it is, so a mismatched beat falls back to character-weighted
interpolation *inside the measured speech span*.

It is not close to free of error -- it is interpolation, and it stays
interpolation. It simply cannot be wrong in the specific way an index mapping
over mismatched counts is wrong, and it is right about where the speech stops.
Interpolation across the whole file remains the last resort, for when there is
no trustworthy span at all.

A time-warp variant was tried and rejected: mapping each caption word's
cumulative character position onto Whisper's own token-time curve, so the
guess follows the measured pacing instead of a straight line. Over thirty
simulated mismatches (splitting or merging one token in each count-matching
beat) it scored mean 0.114s against the plain span's 0.116s, with a worse
worst case. It is more code for nothing.

Shape of the output
-------------------

Whatever path is taken, a beat gets exactly one timing per caption word, the
first starts at 0.0, they are contiguous, and the last ends at or before the
beat's measured duration.

Contiguity is not tidiness, it is a requirement. ``engine/assembly/captions.py``
emits ``{\\k<centiseconds>}`` per word and libass accumulates those from the
start of the dialogue line -- it never reads ``word.start``. A gap left between
two words would pull every later word early by the size of the gap. So silence
is absorbed into the word before it, which is also what a karaoke highlight
should do: hold the last-spoken word until the next one begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engine.contract import WordTiming

# How much of the beat Whisper must have heard before its speech span is
# believed. Real beats land at 0.83-0.87 of the file. Below this the
# transcription probably stopped early, and trusting it would squeeze the
# whole caption into the front of the audio -- worse than interpolating.
SPEECH_FLOOR = 0.5

# Timing sources, in the order they are preferred. These strings are what
# lands on ``Beat.word_timing_source`` and in the progress line, so a run that
# silently interpolated is distinguishable from one that aligned.
WHISPER = "whisper"            # Whisper's timings, applied by index
SPAN = "whisper-span"          # interpolated inside Whisper's speech span
INTERPOLATED = "interpolated"  # the old behaviour: spread over the file


@dataclass(frozen=True)
class Alignment:
    """One beat's caption timings, and where they came from."""

    words: list[WordTiming]
    source: str
    detail: str = ""


def load_whisper(model_size: str, device: str, compute_type: str):
    """Import faster-whisper and build a model. Patched in tests.

    Separate from ``WhisperAligner`` so the import stays lazy: nothing about
    faster-whisper is touched on a run with the switch off, and a clean clone
    that never enables it never downloads the ~40MB model.
    """
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device=device, compute_type=compute_type)


class WhisperAligner:
    """Holds the model for the whole run and aligns one beat at a time.

    The model is an instance field, not a per-call load, because that is the
    entire cost question here: ~8s of load against ~1.5s of transcription per
    beat. Ten beats reloading the model would cost more than the render.

    A load that fails is remembered as a failure and never retried, for the
    same reason ``synth_plan`` abandons Piper for the whole plan after one
    failure: a model that cannot load for beat one cannot load for beat two,
    and rediscovering that ten times is the cost this must not add.
    """

    def __init__(self, *, model_size: str = "base", device: str = "cpu",
                 compute_type: str = "int8", language: str = "hi",
                 model_factory=None) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._factory = model_factory
        self._model = None
        self._dead = ""
        self.model_loads = 0

    # --- the model ---------------------------------------------------------

    def model(self):
        """The loaded model, or None if it could not be loaded.

        Loads at most once per aligner, successfully or not.
        """
        if self._model is not None or self._dead:
            return self._model
        self.model_loads += 1
        try:
            if self._factory is not None:
                self._model = self._factory()
            else:
                self._model = load_whisper(self.model_size, self.device,
                                           self.compute_type)
        except Exception as exc:
            self._dead = f"{type(exc).__name__}: {exc}"
            self._model = None
        return self._model

    # --- one beat ----------------------------------------------------------

    def spans(self, audio_path: str | Path) -> list[tuple[float, float]]:
        """Whisper's per-word (start, end) pairs. Text is deliberately
        dropped: it is Urdu script and matching it to Roman captions is the
        thing that cannot be done."""
        model = self.model()
        if model is None:
            raise RuntimeError(self._dead or "no model")
        segments, _info = model.transcribe(
            str(audio_path), word_timestamps=True, language=self.language)
        return [(float(w.start), float(w.end))
                for segment in segments
                for w in (getattr(segment, "words", None) or [])]

    def align(self, audio_path: str | Path, caption_text: str,
              duration: float) -> Alignment:
        """Timings for one beat's caption words. Never raises.

        Every failure -- no model, no transcription, a count mismatch, a
        transcription that does not survive its sanity checks -- comes back as
        an ``Alignment`` carrying a lesser ``source`` and a reason, because a
        timing problem must never cost an approved script its render.
        """
        # Imported here rather than at module scope: voice.py imports this
        # module, and the character weighting is voice.py's own policy --
        # there is one implementation of it, not two.
        from engine.media.voice import distribute_words

        fallback = distribute_words(caption_text, 0.0, duration)
        if not fallback:
            return Alignment([], INTERPOLATED, "no caption words")

        try:
            spans = self.spans(audio_path)
        except Exception as exc:
            return Alignment(fallback, INTERPOLATED,
                             self._dead or f"{type(exc).__name__}: {exc}")

        spans = [(s, e) for s, e in spans if e > s]
        if not spans:
            return Alignment(fallback, INTERPOLATED, "nothing transcribed")

        speech_end = min(max(e for _s, e in spans), duration)
        if speech_end < SPEECH_FLOOR * duration:
            return Alignment(
                fallback, INTERPOLATED,
                f"heard only {speech_end:.2f}s of {duration:.2f}s")

        span_words = distribute_words(caption_text, 0.0, speech_end)
        wanted = len(fallback)
        if len(spans) != wanted:
            return Alignment(
                span_words, SPAN,
                f"{len(spans)} spoken tokens vs {wanted} caption words")

        exact = _by_index(caption_text, spans, speech_end)
        if exact is None:
            return Alignment(span_words, SPAN,
                             "transcription was not monotonic")
        return Alignment(exact, WHISPER, f"{wanted} words")


def _by_index(caption_text: str, spans: list[tuple[float, float]],
              speech_end: float) -> list[WordTiming] | None:
    """Apply Whisper's timings to the caption words, position for position.

    Returns None if the result would not be a usable caption track: starts
    must strictly increase, or the ``\\k`` durations captions.py derives from
    them would be zero or negative.
    """
    words = caption_text.split()
    starts = [s for s, _e in spans]
    # The first word is pinned to 0.0 -- see the module docstring on \\k.
    starts[0] = 0.0
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return None
    if starts[-1] >= speech_end:
        return None

    bounds = starts[1:] + [speech_end]
    return [WordTiming(word=word, start=start, end=end)
            for word, start, end in zip(words, starts, bounds)]


def build_aligner(settings) -> WhisperAligner | None:
    """One aligner for the run, or None when the switch is off.

    ``getattr`` rather than attribute access because several test doubles and
    ``scripts/`` stand-ins implement only the settings they use, and a new
    optional feature must not break them.
    """
    if not getattr(settings, "align_words", False):
        return None
    return WhisperAligner(
        model_size=getattr(settings, "align_model", "base"),
        device=getattr(settings, "align_device", "cpu"),
        compute_type=getattr(settings, "align_compute", "int8"),
        language=getattr(settings, "align_language", "hi"))

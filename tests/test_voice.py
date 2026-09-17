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

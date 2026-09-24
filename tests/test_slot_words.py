"""Which words are on screen while each clip plays.

A beat is one line and several clips. "kamare me ek kursi, aur kursi par
mera naam" is two pictures -- a chair, then a name on it -- and the beat
gets two or three slots to carry them. Choosing footage for a slot is
impossible without knowing which half of the line that slot covers: you
cannot decide the chair shot goes first if nothing tells you slot 0 is
the words "kamare me ek kursi".

Everything needed is already stored. ``Beat.words`` carries a timing per
caption word and ``slot_durations`` carries the span of each slot; this
is the join nobody had written.

A word that straddles a cut is put in the slot holding most of it, so
every word appears exactly once. Listing it twice is more literally true
-- it really is on screen across the cut -- but a reader scanning for
"where does 'kursi' go" wants one answer, and "mostly here" is that
answer.
"""

from __future__ import annotations

import pytest

from engine.contract import WordTiming
from engine.media.clips import SECONDS_PER_CLIP, clip_count, slot_words
from tests.factories import make_plan


def _beat(line: str, measured: float):
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = line
    beat.measured_seconds = measured
    beat.words = caption_timings(line, measured)
    return beat


LINE = "kamare me ek kursi aur kursi par mera naam likha tha"


def test_every_slot_gets_the_words_that_play_over_it():
    beat = _beat(LINE, 5.4)

    rows = slot_words(beat)

    assert len(rows) == clip_count(5.4) == 3
    assert rows[0].words == "kamare me ek kursi"
    assert rows[2].words.endswith("likha tha")


def test_each_word_lands_in_exactly_one_slot():
    """A word across a cut goes where most of it is, so scanning for one
    word gives one answer."""
    beat = _beat(LINE, 5.4)

    joined = " ".join(row.words for row in slot_words(beat)).split()

    assert joined == LINE.split()


def test_the_spans_are_the_slots_own_and_they_tile_the_beat():
    beat = _beat(LINE, 5.4)

    rows = slot_words(beat)

    assert rows[0].start == pytest.approx(0.0)
    assert rows[-1].end == pytest.approx(5.4)
    for earlier, later in zip(rows, rows[1:]):
        assert earlier.end == pytest.approx(later.start)


def test_a_one_clip_beat_holds_the_whole_line():
    beat = _beat("ek chhoti line", 2.0)

    rows = slot_words(beat)

    assert len(rows) == 1
    assert rows[0].words == "ek chhoti line"


def test_a_beat_that_has_not_been_spoken_yet_reports_empty_slots():
    """Before VOICE there are no timings, and guessing them would put
    words on slots that may not survive the measurement."""
    beat = _beat(LINE, 5.4)
    beat.words = []

    rows = slot_words(beat)

    assert len(rows) == 3
    assert all(row.words == "" for row in rows)


def test_a_slot_no_word_falls_in_is_empty_rather_than_missing():
    """A pause long enough to own a whole slot is real: the row still
    exists, and its emptiness is the useful fact."""
    beat = _beat("shuru", 7.5)
    beat.words = [WordTiming(word="shuru", start=0.0, end=0.4)]

    rows = slot_words(beat)

    assert len(rows) == 3
    assert rows[0].words == "shuru"
    assert rows[1].words == ""
    assert rows[2].words == ""


def test_the_count_follows_the_measured_length_not_the_guess():
    """Clip count is ceil(measured / 2.5), so a beat that ran long gets
    more slots -- which is why this can only be known after VOICE."""
    short = _beat(LINE, 2.4)
    long = _beat(LINE, 9.9)

    assert len(slot_words(short)) == 1
    assert len(slot_words(long)) == 4
    assert SECONDS_PER_CLIP == 2.5


# --- the review board carries it -------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from engine.app import create_app
    from engine.config import Settings

    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _seeded(client, line=LINE, measured=5.4):
    from engine.contract import Clip
    from engine.media.clips import clip_count, slot_durations
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = line
    beat.measured_seconds = measured
    beat.words = caption_timings(line, measured)
    count = clip_count(measured)
    beat.clips = [Clip(path="", query=f"q{i}", provider="pexels",
                       duration=span)
                  for i, span in enumerate(slot_durations(measured, count))]
    client.app.state.store.save_plan(plan, status="awaiting_clip_review")
    return plan


def test_each_board_row_says_which_words_its_clip_covers(client):
    """The reason the gate exists: pick footage per slot, which needs the
    words per slot."""
    _seeded(client)

    rows = client.get("/api/plan/p1/clips").json()["clips"]

    assert [row["slot_text"] for row in rows] == [
        "kamare me ek kursi", "aur kursi par mera", "naam likha tha"]


def test_a_row_carries_the_span_so_the_words_can_be_placed_in_time(client):
    _seeded(client)

    rows = client.get("/api/plan/p1/clips").json()["clips"]

    assert rows[0]["slot_start"] == 0.0
    assert rows[-1]["slot_end"] == pytest.approx(5.4)


def test_the_words_follow_the_slot_and_not_the_beat(client):
    """Every row used to show beat-wide text, which is exactly why the
    slots looked identical."""
    _seeded(client)

    rows = client.get("/api/plan/p1/clips").json()["clips"]

    assert len({row["slot_text"] for row in rows}) == len(rows)
    assert len({row["caption_text"] for row in rows}) == 1

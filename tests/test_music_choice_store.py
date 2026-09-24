"""The bed a reel was given, remembered per reel.

``audio.find_music`` reads one shared folder, so before this every reel
rendered with the same track. A bed is a mood choice and the mood
belongs to the reel, so a chosen track is stored against its plan --
the same shape ``sticker_choices`` already uses, for the same reason.

One row per plan, replaced rather than appended: there is one bed per
reel, and a history of rejected picks would only have to be filtered
back out.

The credit is stored with the path. Re-deriving it at publish time would
mean asking Openverse again about a track that may since have been
withdrawn or relicensed -- and the credit that is owed is the one for
the file actually on disk.
"""

from __future__ import annotations

import pytest

from engine.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def _set(store, plan_id="p1", **over):
    row = {"openverse_id": "a1", "path": "/work/p1/music/a1.mp3",
           "title": "Creepy vinyl", "creator": "someone",
           "licence": "by", "attribution": '"Creepy vinyl" by someone...',
           "source_url": "https://freesound.org/s/a1"}
    row.update(over)
    store.set_music_choice(plan_id, **row)
    return row


def test_a_plan_with_no_choice_reports_none(store):
    assert store.music_choice("p1") is None


def test_a_chosen_bed_comes_back_whole(store):
    written = _set(store)

    got = store.music_choice("p1")

    assert got["path"] == written["path"]
    assert got["attribution"] == written["attribution"]
    assert got["licence"] == "by"


def test_choosing_again_replaces_rather_than_stacks(store):
    _set(store, openverse_id="a1")
    _set(store, openverse_id="a2", path="/work/p1/music/a2.mp3")

    got = store.music_choice("p1")

    assert got["openverse_id"] == "a2"


def test_each_plan_keeps_its_own_bed(store):
    _set(store, plan_id="p1", openverse_id="a1")
    _set(store, plan_id="p2", openverse_id="a2")

    assert store.music_choice("p1")["openverse_id"] == "a1"
    assert store.music_choice("p2")["openverse_id"] == "a2"


def test_a_bed_can_be_taken_back_off(store):
    """Clearing has to mean 'fall back to the shared folder', not
    'render in silence'."""
    _set(store)

    store.clear_music_choice("p1")

    assert store.music_choice("p1") is None


def test_clearing_a_plan_that_never_chose_is_not_an_error(store):
    store.clear_music_choice("p1")

    assert store.music_choice("p1") is None


def test_an_old_database_gains_the_table_without_being_rebuilt(tmp_path):
    """Plans already waiting at a gate must survive the upgrade."""
    path = tmp_path / "t.db"
    Store(path).init()  # a database created before this feature existed

    second = Store(path)
    second.init()
    second.set_music_choice("p1", openverse_id="a1", path="/x.mp3",
                            title="t", creator="c", licence="cc0",
                            attribution="", source_url="")

    assert second.music_choice("p1")["openverse_id"] == "a1"

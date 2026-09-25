r"""The look a reel was given, remembered per reel.

The same shape `music_choices` uses, because it works: one row per
plan, replaced rather than appended.

The row stores the look's *values* and not only its id. A preset whose
definition later changes must not silently restyle a reel that was
already reviewed and approved under the old one.
"""

from __future__ import annotations

import pytest

from engine.store import Store

ROW = dict(look_id="blocky-urban", font="Bungee", caption_size=56,
           spoken="&H0000D7FF", upcoming="&H00FFFFFF", margin_v=300,
           punch_font="Bungee", punch_size=72, punch_animation="letters")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_a_plan_with_no_look_reports_none(store):
    assert store.look_choice("p1") is None


def test_a_chosen_look_comes_back_whole(store):
    store.set_look_choice("p1", **ROW)

    got = store.look_choice("p1")

    assert got["look_id"] == "blocky-urban"
    assert got["font"] == "Bungee"
    assert got["punch_animation"] == "letters"
    assert got["caption_size"] == 56


def test_the_values_are_stored_not_just_the_id(store):
    """A preset can be redefined. A reel reviewed under the old one
    keeps what it was reviewed with."""
    store.set_look_choice("p1", **ROW)

    assert set(ROW) <= set(store.look_choice("p1"))


def test_choosing_again_replaces_rather_than_stacks(store):
    store.set_look_choice("p1", **ROW)
    store.set_look_choice("p1", **{**ROW, "look_id": "poster",
                                   "font": "Staatliches"})

    assert store.look_choice("p1")["font"] == "Staatliches"


def test_each_plan_keeps_its_own(store):
    store.set_look_choice("p1", **ROW)
    store.set_look_choice("p2", **{**ROW, "look_id": "techno"})

    assert store.look_choice("p1")["look_id"] == "blocky-urban"
    assert store.look_choice("p2")["look_id"] == "techno"


def test_a_look_can_be_taken_back_off(store):
    store.set_look_choice("p1", **ROW)

    store.clear_look_choice("p1")

    assert store.look_choice("p1") is None


def test_clearing_a_plan_that_never_chose_is_not_an_error(store):
    store.clear_look_choice("p1")

    assert store.look_choice("p1") is None


def test_a_custom_look_stores_the_same_way(store):
    store.set_look_choice("p1", **{**ROW, "look_id": "custom"})

    assert store.look_choice("p1")["look_id"] == "custom"


def test_an_old_database_gains_the_table(tmp_path):
    path = tmp_path / "t.db"
    Store(path).init()

    second = Store(path)
    second.init()
    second.set_look_choice("p1", **ROW)

    assert second.look_choice("p1")["look_id"] == "blocky-urban"

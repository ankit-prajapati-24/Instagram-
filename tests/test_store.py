import pytest

from engine.omniroute import CostRecord
from engine.store import Store
from tests.factories import make_plan

CREDIT = "Animated icons by Lordicon.com"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_roundtrip_plan(store):
    store.save_plan(make_plan(), status="draft")
    got = store.get_plan("p1")
    assert got.topic.slug == "roopkund-jheel-ke-kankaal"
    assert len(got.script.beats) == 10
    assert got.metadata.pinned_comment.endswith("bharosa karoge?")


def test_get_missing_plan_returns_none(store):
    assert store.get_plan("nope") is None


def test_save_plan_is_idempotent_on_plan_id(store):
    store.save_plan(make_plan(), status="draft")
    store.save_plan(make_plan(), status="approved")
    rows = store.list_plans()
    assert len(rows) == 1
    assert rows[0]["status"] == "approved"


def test_costs_accumulate_per_plan_and_per_day(store):
    store.save_plan(make_plan(), status="draft")
    store.record_cost("p1", "script", CostRecord(usd=0.01, provider="groq"))
    store.record_cost("p1", "images",
                      CostRecord(usd=0.02, provider="together",
                                 fallback_attempts=1))
    assert store.plan_cost("p1") == pytest.approx(0.03)
    assert store.today_usd() == pytest.approx(0.03)


def test_hash_exists_only_counts_plans_that_became_videos(store):
    plan = make_plan()
    assert store.hash_exists(plan.topic.dedupe_hash) is False

    for status in ("draft", "awaiting_approval", "rejected_moderation",
                   "rejected_dedup", "qc_failed"):
        store.save_plan(plan, status=status)
        assert store.hash_exists(plan.topic.dedupe_hash) is False, status

    for status in ("approved", "produced", "published"):
        store.save_plan(plan, status=status)
        assert store.hash_exists(plan.topic.dedupe_hash) is True, status


def test_published_slugs_excludes_rejected_plans(store):
    store.save_plan(make_plan(plan_id="p1", raw="alpha topic"),
                    status="produced")
    store.save_plan(make_plan(plan_id="p2", raw="bravo topic"),
                    status="rejected_moderation")
    assert store.published_slugs() == ["alpha-topic"]


def test_plan_status_roundtrip(store):
    store.save_plan(make_plan(), status="awaiting_approval")
    assert store.plan_status("p1") == "awaiting_approval"
    store.set_status("p1", "approved")
    assert store.plan_status("p1") == "approved"
    assert store.plan_status("missing") is None


def test_entity_cooldown_is_case_insensitive(store):
    store.save_plan(make_plan(), status="draft")
    assert store.entity_last_seen("Roopkund") is None
    store.record_entities("p1", ["Roopkund", "Uttarakhand"])
    assert store.entity_last_seen("roopkund") is not None
    assert store.entities_in_cooldown(["ROOPKUND"], days=45) == ["ROOPKUND"]
    assert store.entities_in_cooldown(["Bhangarh"], days=45) == []


def test_embeddings_roundtrip(store):
    store.save_plan(make_plan(plan_id="p1"), status="draft")
    store.save_plan(make_plan(plan_id="p2", raw="bhangarh fort"),
                    status="draft")
    store.save_embedding("p1", [0.1, 0.2])
    store.save_embedding("p2", [0.3, 0.4])
    rows = dict(store.recent_embeddings(10))
    assert len(rows) == 2
    assert rows["p1"] == [0.1, 0.2]


def test_assets_record_provenance(store):
    store.save_plan(make_plan(), status="draft")
    store.save_asset("p1", "b0", "image", "together/flux", "a.png",
                     source_url=None, checksum="abc")
    row = store.plan_assets("p1")[0]
    assert row["provider"] == "together/flux"
    assert row["licence"] == "ai-generated"
    assert row["checksum"] == "abc"


def test_render_upsert_keeps_latest_output(store):
    store.save_plan(make_plan(), status="draft")
    store.record_render("p1", "render:p1:v1", "failed", error_log="boom")
    store.record_render("p1", "render:p1:v1", "done", output_path="o.mp4",
                        duration_s=44.0)
    assert store.list_plans()[0]["video"] == "o.mp4"


def test_the_attribution_column_is_added_to_a_database_that_predates_it(
        tmp_path):
    """The renders table is created with CREATE TABLE IF NOT EXISTS, which
    does nothing to a table that already exists. Without an explicit widen,
    every clone that had ever rendered would raise "no such column" on the
    publish route -- including the live engine.db this was found in.

    Also asserts init() is safe to run twice, because it runs on every app
    start, and that the pre-existing row keeps the NULL that makes it mean
    "this video owes no credit".
    """
    import sqlite3

    db = tmp_path / "old.db"
    old = Store(db)
    old.init()
    # Put the table back in its pre-column shape, with a row in it, exactly
    # as a database rendered against before this change looks.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE renders")
        conn.execute(
            "CREATE TABLE renders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "plan_id TEXT NOT NULL, idempotency_key TEXT UNIQUE, "
            "status TEXT NOT NULL, attempts INTEGER DEFAULT 0, "
            "output_path TEXT, duration_s REAL, error_log TEXT, "
            "created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO renders(plan_id, idempotency_key, status, "
            "output_path, created_at) VALUES('p1','render:p1:v1','done',"
            "'o.mp4','2026-09-18T08:51:03+00:00')")

    reopened = Store(db)
    reopened.init()
    reopened.init()                      # every app start runs this

    assert reopened.render_attribution("p1") is None
    reopened.record_render("p1", "render:p1:v2", "done", output_path="n.mp4",
                           attribution=CREDIT)
    assert reopened.render_attribution("p1") == CREDIT


def test_a_failed_retry_does_not_leave_a_stale_credit_behind(store):
    """The upsert shares one row per plan, so a re-render that fails must
    not leave the previous success's credit sitting on it."""
    store.save_plan(make_plan(), status="draft")
    store.record_render("p1", "render:p1:v1", "done", output_path="o.mp4",
                        attribution=CREDIT)
    assert store.render_attribution("p1")
    store.record_render("p1", "render:p1:v1", "failed", error_log="boom")
    assert store.render_attribution("p1") is None


def test_list_plans_reports_cost(store):
    store.save_plan(make_plan(), status="draft")
    store.record_cost("p1", "script", CostRecord(usd=0.05))
    assert store.list_plans()[0]["usd"] == pytest.approx(0.05)


def test_a_sticker_choice_is_remembered_per_plan_and_trigger(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    assert store.sticker_choices("p1") == {}

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p1", "science", "440-dna")
    assert store.sticker_choices("p1") == {
        "death": "2130-skull-poison", "science": "440-dna"}


def test_choosing_again_replaces_rather_than_accumulates(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p1", "death", "2841-crashed-skull")
    assert store.sticker_choices("p1") == {"death": "2841-crashed-skull"}


def test_two_plans_choosing_the_same_trigger_do_not_collide(tmp_path):
    store = Store(tmp_path / "s.db")
    store.init()

    store.choose_sticker("p1", "death", "2130-skull-poison")
    store.choose_sticker("p2", "death", "2816-skull-halloween")
    assert store.sticker_choices("p1") == {"death": "2130-skull-poison"}
    assert store.sticker_choices("p2") == {"death": "2816-skull-halloween"}


def test_the_choices_table_is_added_to_a_database_that_predates_it(tmp_path):
    """The same shape as the `attribution` column: an existing engine.db
    must gain the table on the next start, not on a fresh install only."""
    path = tmp_path / "old.db"
    store = Store(path)
    store.init()

    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE sticker_choices")

    store.init()
    assert store.sticker_choices("p1") == {}


def test_two_beats_round_trip_their_own_slugs(tmp_path):
    """A plain round trip, not a guard: two distinct beat ids are two
    distinct values under either the old trigger-keyed schema or the new
    beat-keyed one, so this cannot see the defect the task fixes. The
    schema itself is asserted in
    ``test_the_choice_table_is_keyed_by_beat_not_by_trigger`` below, and
    the end-to-end proof (two beats sharing one trigger word) lives in the
    ``prepare()`` test in the next task."""
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p1", "b7", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "27-globe",
                                           "b7": "1875-planet"}


def test_the_choice_table_is_keyed_by_beat_not_by_trigger(tmp_path):
    """The defect this task fixes is a key shape, so the key is what the
    test has to look at. Keyed by trigger name, a reel whose script named
    one concept at two beats could only ever wear one icon for both -- and
    a round-trip test cannot see that, because two beat ids are two
    distinct values under either schema.
    """
    store = Store(tmp_path / "t.db")
    store.init()
    with store._conn() as conn:
        info = list(conn.execute("PRAGMA table_info(sticker_choices)"))
    names = [row["name"] for row in info]
    primary = sorted(row["name"] for row in info if row["pk"])
    assert "beat_id" in names, f"columns are {names}"
    assert "trigger" not in names, f"the old key is still there: {names}"
    assert primary == ["beat_id", "plan_id"], f"primary key is {primary}"


def test_choosing_again_for_one_beat_replaces_it(tmp_path):
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p1", "b3", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "1875-planet"}


def test_choices_do_not_leak_between_plans(tmp_path):
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p2", "b3", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "27-globe"}
    assert store.sticker_choices("p2") == {"b3": "1875-planet"}

import pytest

from engine.omniroute import CostRecord
from engine.store import Store
from tests.factories import make_plan


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


def test_hash_exists_detects_exact_repeat(store):
    plan = make_plan()
    assert store.hash_exists(plan.topic.dedupe_hash) is False
    store.save_plan(plan, status="draft")
    assert store.hash_exists(plan.topic.dedupe_hash) is True


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


def test_list_plans_reports_cost(store):
    store.save_plan(make_plan(), status="draft")
    store.record_cost("p1", "script", CostRecord(usd=0.05))
    assert store.list_plans()[0]["usd"] == pytest.approx(0.05)

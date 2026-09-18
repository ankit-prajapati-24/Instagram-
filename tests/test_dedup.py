import pytest

from engine.contract import Topic
from engine.gates.dedup import check, cosine, trigram_similarity
from engine.store import Store
from tests.factories import make_plan


def test_trigram_identical_is_one():
    assert trigram_similarity("roopkund skeletons",
                              "roopkund skeletons") == pytest.approx(1.0)


def test_trigram_reworded_is_high():
    assert trigram_similarity("roopkund-lake-skeletons",
                              "skeletons-of-roopkund-lake") > 0.5


def test_trigram_unrelated_is_low():
    assert trigram_similarity("roopkund-skeletons",
                              "bhangarh-fort-curse") < 0.3


def test_cosine_identical_orthogonal_and_degenerate():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0], [1.0, 2.0]) == 0.0


class FakeEmbedder:
    """Stands in for OmniRouteClient.embed with one fixed vector."""

    def __init__(self, vector):
        self.vector = vector

    def embed(self, texts):
        return [self.vector]


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_fresh_topic_passes_all_layers(store):
    result = check(Topic.make("bhangarh fort ka raat ka curfew"), store)
    assert result.passed
    assert result.layer is None


def test_exact_repeat_fails_on_layer_one(store):
    plan = make_plan()
    store.save_plan(plan, status="produced")
    result = check(plan.topic, store)
    assert not result.passed
    assert result.layer == "exact"


def test_reworded_topic_fails_on_trigram(store):
    store.save_plan(make_plan(raw="Roopkund jheel ke kankaal"),
                    status="produced")
    result = check(Topic.make("kankaal ke Roopkund jheel"), store)
    assert not result.passed
    assert result.layer == "trigram"


def test_a_gate_rejected_topic_can_be_retried(store):
    """Rejections stay for the audit trail; they must not block a retry.

    Counting every row made a topic that failed the moderation gate
    permanently un-retryable, reported as "slug already produced".
    """
    plan = make_plan()
    store.save_plan(plan, status="rejected_moderation")
    assert check(plan.topic, store).passed

    store.save_plan(make_plan(plan_id="p2", raw="Bhangarh fort ka raaz"),
                    status="rejected_dedup")
    assert check(Topic.make("Bhangarh fort ka raaz"), store).passed


def test_a_draft_does_not_block_its_own_topic(store):
    """A plan still awaiting approval has not produced anything yet."""
    plan = make_plan()
    store.save_plan(plan, status="awaiting_approval")
    assert check(plan.topic, store).passed


def test_semantic_layer_catches_different_wording(store):
    store.save_plan(make_plan(plan_id="p1"), status="draft")
    store.save_embedding("p1", [1.0, 0.0, 0.0])
    result = check(Topic.make("a completely unrelated looking phrase"),
                   store, FakeEmbedder([0.99, 0.01, 0.0]))
    assert not result.passed
    assert result.layer == "semantic"


def test_semantic_layer_allows_a_genuinely_new_angle(store):
    store.save_plan(make_plan(plan_id="p1"), status="draft")
    store.save_embedding("p1", [1.0, 0.0, 0.0])
    result = check(Topic.make("kuldhara gaon khaali kyun hua"),
                   store, FakeEmbedder([0.0, 1.0, 0.0]))
    assert result.passed


def test_semantic_layer_skipped_when_no_client(store):
    store.save_plan(make_plan(plan_id="p1"), status="draft")
    store.save_embedding("p1", [1.0, 0.0, 0.0])
    assert check(Topic.make("something totally else entirely"),
                 store, None).passed


def test_entity_cooldown_fails_on_layer_four(store):
    store.save_plan(make_plan(plan_id="p1"), status="draft")
    store.record_entities("p1", ["Roopkund"])
    result = check(Topic.make("ek aur anokhi kahani",
                              entities=["Roopkund"]), store)
    assert not result.passed
    assert result.layer == "cooldown"
    assert "Roopkund" in result.detail


# --- the cooldown layer must not lock the whole engine ----------------------
# Regression: FakeOmniRoute returned the same three entities for every topic.
# They were written against the first approved plan, and from then on every
# new topic tripped the cooldown layer — the panel was unusable in exactly
# the fallback mode it ships in.

def test_fake_client_derives_entities_from_the_typed_topic():
    from engine.fake_client import FakeOmniRoute
    client = FakeOmniRoute()

    def entities(topic):
        prompt = f"STAGE:research\nTOPIC: {topic}\n"
        return client.chat([{"role": "user", "content": prompt}],
                           want_json=True).data["entities"]

    taj = entities("tajmahal ke andar jana mana kyu hai")
    kuldhara = entities("Kuldhara gaon ek raat mein khaali kyun hua")
    assert taj != kuldhara
    assert "Tajmahal" in taj
    assert "Kuldhara" in kuldhara
    # Hinglish particles carry no identity and must not become entities.
    assert not {"Kyu", "Hai", "Mein", "Ek"} & set(taj + kuldhara)


def test_two_different_topics_both_pass_the_cooldown_layer(store):
    store.save_plan(make_plan(plan_id="p1", raw="Kuldhara gaon"),
                    status="produced")
    store.record_entities("p1", ["Kuldhara", "Jaisalmer"])
    fresh = Topic.make("Tajmahal ke andar", entities=["Tajmahal", "Agra"])
    assert check(fresh, store).passed


def test_cooldown_detail_reports_days_remaining(store):
    store.save_plan(make_plan(), status="produced")
    store.record_entities("p1", ["Roopkund"])
    detail = store.cooldown_detail(["Roopkund"], days=45)
    assert detail[0][0] == "Roopkund"
    assert detail[0][1] == 45          # recorded just now
    result = check(Topic.make("x", entities=["Roopkund"]), store)
    assert "free in 45d" in result.detail


def test_clearing_entities_unblocks_a_topic(store):
    store.save_plan(make_plan(), status="produced")
    store.record_entities("p1", ["Roopkund", "Uttarakhand"])
    blocked = Topic.make("naya topic", entities=["Roopkund"])
    assert not check(blocked, store).passed

    assert store.clear_entities() == 2
    assert check(blocked, store).passed


def test_clearing_entities_for_one_plan_leaves_the_rest(store):
    store.save_plan(make_plan(plan_id="p1"), status="produced")
    store.save_plan(make_plan(plan_id="p2", raw="other topic"),
                    status="produced")
    store.record_entities("p1", ["Roopkund"])
    store.record_entities("p2", ["Bhangarh"])
    assert store.clear_entities("p1") == 1
    assert store.entity_last_seen("Roopkund") is None
    assert store.entity_last_seen("Bhangarh") is not None

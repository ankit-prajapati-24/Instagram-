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
    store.save_plan(plan, status="draft")
    result = check(plan.topic, store)
    assert not result.passed
    assert result.layer == "exact"


def test_reworded_topic_fails_on_trigram(store):
    store.save_plan(make_plan(raw="Roopkund jheel ke kankaal"),
                    status="draft")
    result = check(Topic.make("kankaal ke Roopkund jheel"), store)
    assert not result.passed
    assert result.layer == "trigram"


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

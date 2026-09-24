"""The hand-written-script path.

Every test here exists because the feature's whole point is that a human can
produce a video while the gateway is dead. So the load-bearing test drives
the path with no reachable provider AND with every agent entry point booby
trapped, rather than mocking the thing under test.
"""

import inspect
import threading

import pytest
from fastapi.testclient import TestClient

import engine.agents as agents
import engine.pipeline as pipeline
from engine.app import create_app
from engine.config import (Settings, beat_count, speech_rate, spoken_seconds,
                           word_budget)
from engine.contract import ReelPlan
from engine.gates.qc import pre_render_range, run_qc
from engine.publish.payloads import publish_checklist
from engine.store import Store
from tests.factories import make_plan

DEVA = "रहस्य"
ROMAN = "rahasya"
ROLES = ["hook", "setup", "escalation", "reveal", "twist", "escalation",
         "reveal", "twist", "cliffhanger", "cta"]


def beats_of(total_words, beats=None):
    """``beats`` hand-written beats carrying exactly ``total_words`` words."""
    beats = beats or beat_count()
    base, extra = divmod(total_words, beats)
    assert base >= 1, "this helper cannot build a beat with no words"
    out = []
    for i in range(beats):
        n = base + (1 if i < extra else 0)
        # Ending each line properly is not decoration: QC's sentence_variance
        # check needs sentences to count, and a fixture with no sentence
        # punctuation would quietly drop a check and make the "both paths
        # get the same scorecard" comparison weaker than it looks.
        out.append({
            "role": ROLES[i % len(ROLES)],
            "voice_text": " ".join([DEVA] * n) + "।",
            "caption_text": " ".join([ROMAN] * n) + ".",
            "visual_prompt": "dark cinematic lake at night, scene %d" % i,
        })
    return out


def payload(total_words=None, **over):
    body = {
        "topic": "Roopkund jheel ke 500 kankaal",
        "beats": beats_of(total_words or word_budget()),
        "sources": [{"text": "Roopkund mein ~500 kankaal",
                     "source_url": "https://example.org/roopkund",
                     "confidence": "high"}],
    }
    body.update(over)
    return body


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.db_path = tmp_path / "t.db"
    s.out_dir = tmp_path / "out"
    s.work_dir = tmp_path / "work"
    # A dead port, exactly as tests/test_app.py does for health.
    s.omniroute_base = "http://127.0.0.1:1/v1"
    return s


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings=settings))


@pytest.fixture()
def no_gateway(monkeypatch):
    """Make every route to a model an immediate, loud failure.

    The dead port alone only proves nothing *succeeded* over the network.
    These make an attempt itself the test failure, including one through the
    fake client -- a built-in sample script is still not the user's script.
    """
    def forbidden(*a, **k):
        raise AssertionError(
            "the manual path reached the LLM gateway; it must not")

    import engine.fake_client as fake_client
    import engine.omniroute as omniroute

    monkeypatch.setattr(omniroute.OmniRouteClient, "__init__", forbidden)
    monkeypatch.setattr(fake_client.FakeOmniRoute, "__init__", forbidden)
    for name in ("run_research", "run_hooks", "run_script", "run_metadata"):
        monkeypatch.setattr(pipeline, name, forbidden)
    return forbidden


# --- the load-bearing test --------------------------------------------------

def test_a_manual_plan_is_produce_ready_with_the_gateway_unreachable(
        client, no_gateway):
    response = client.post("/api/plan/manual", json=payload())
    assert response.status_code == 200, response.text
    plan = ReelPlan.model_validate(response.json()["plan"])

    assert len(plan.script.beats) == beat_count()
    assert plan.hooks, "approve needs a hook to choose"
    assert plan.chosen() is not None
    assert plan.script.chosen_hook == plan.hooks[0].variant_id
    assert all(b.voice_text.strip() and b.caption_text.strip()
               and b.visual_prompt.strip() for b in plan.script.beats)
    assert all(b.target_seconds > 0 for b in plan.script.beats)

    store: Store = client.app.state.store
    assert store.plan_status(plan.plan_id) == "awaiting_approval"


def test_the_manual_builder_has_no_client_to_call():
    """Structural, not behavioural: it cannot make an LLM call because it is
    never handed anything that could."""
    params = inspect.signature(pipeline.manual_plan_stage).parameters
    assert "client" not in params


# --- convergence ------------------------------------------------------------

def test_a_manual_plan_approves_and_produces_like_a_generated_one(
        client, monkeypatch):
    manual = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=payload(
            metadata={"yt_title": "Roopkund: 500 Kankaal",
                      "yt_description": "Uttarakhand ki ek jheel.",
                      "ig_caption": "500 kankaal, ek hi raat.",
                      "pinned_comment": "Tum kispe bharosa karoge?",
                      "hashtags": ["#rahasya"]})).json()["plan"])

    store: Store = client.app.state.store
    generated = make_plan(plan_id="gen-1", raw="Kuldhara ka shraap")
    store.save_plan(generated, status="awaiting_approval")

    seen = {}
    done = threading.Event()

    def fake_produce(plan, client_, store_, settings_, **kwargs):
        seen[plan.plan_id] = plan
        done.set()
        return {"video": "x.mp4", "ass": "x.ass", "probe": {},
                "scorecard": {}, "cost_usd": 0.0, "providers": {}}

    import engine.app as app_module
    monkeypatch.setattr(app_module, "produce_stage", fake_produce)

    for plan in (manual, generated):
        hook = plan.hooks[0].variant_id
        approved = client.post("/api/plan/%s/approve" % plan.plan_id,
                               json={"chosen_hook": hook})
        assert approved.status_code == 200, approved.text
        assert store.plan_status(plan.plan_id) == "approved"

        done.clear()
        started = client.post("/api/plan/%s/produce" % plan.plan_id,
                              json={"use_fake": True})
        assert started.status_code == 200, started.text
        assert done.wait(10), "produce never reached produce_stage"

    assert set(seen) == {manual.plan_id, generated.plan_id}
    # The plan handed to produce carries the chosen hook in beat 1 either way.
    for plan in seen.values():
        assert plan.script.beats[0].voice_text == plan.chosen().voice_text

    # And nothing downstream branches on how the script was authored: QC asks
    # the same questions of both.
    def names(plan):
        return [c.name for c in run_qc(plan, actual_duration=45.0,
                                       narration_seconds=45.0,
                                       expect_render=True).checks]

    assert names(seen[manual.plan_id]) == names(seen[generated.plan_id])


# --- the numbers come from the server ---------------------------------------

def test_authoring_numbers_are_the_config_functions(client):
    body = client.get("/api/authoring").json()
    assert body["beat_count"] == beat_count()
    assert body["word_budget"] == word_budget()
    assert body["speech_rate"] == pytest.approx(speech_rate())
    assert body["predicted_seconds"] == pytest.approx(
        spoken_seconds(word_budget()))
    assert body["word_tolerance"] == pytest.approx(agents.WORD_TOLERANCE)
    assert body["latin_pattern"] == agents.LATIN_LETTERS_RE.pattern
    settings: Settings = client.app.state.settings
    assert body["duration_min"] == pytest.approx(settings.duration_min)
    assert body["duration_max"] == pytest.approx(settings.duration_max)
    gate_min, gate_max = pre_render_range(settings.duration_min,
                                          settings.duration_max)
    assert body["gate_min"] == pytest.approx(gate_min)
    assert body["gate_max"] == pytest.approx(gate_max)
    assert len(body["default_roles"]) == beat_count()


def test_the_panel_reads_the_budget_from_the_server(client):
    """The panel must own none of these numbers, only render them."""
    page = client.get("/").text
    assert "/api/authoring" in page
    for field in ("beat_count", "word_budget", "word_tolerance", "word_min",
                  "word_max", "speech_rate", "duration_min", "duration_max",
                  "gate_min", "gate_max", "default_roles", "latin_pattern"):
        assert field in page, f"the panel never reads {field} from the server"

    settings: Settings = client.app.state.settings
    for literal in (str(word_budget()), str(speech_rate()),
                    "%.1f" % settings.duration_min,
                    "%.1f" % settings.duration_max):
        assert literal not in page, \
            f"{literal!r} is written into the panel instead of fetched"


# --- word budget ------------------------------------------------------------

def _band():
    budget = word_budget()
    tol = agents.WORD_TOLERANCE
    return budget, round(budget * (1 - tol)), round(budget * (1 + tol))


def test_a_script_on_the_budget_is_in_band(client, no_gateway):
    budget, _, _ = _band()
    body = client.post("/api/plan/manual", json=payload(budget)).json()
    assert body["budget"]["words"] == budget
    assert body["budget"]["in_band"] is True
    assert body["budget"]["predicted_seconds"] == pytest.approx(
        spoken_seconds(budget))


@pytest.mark.parametrize("edge", ["low", "high"])
def test_the_band_edges_are_still_in_band(client, no_gateway, edge):
    budget, low, high = _band()
    words = low if edge == "low" else high
    body = client.post("/api/plan/manual", json=payload(words)).json()
    assert body["budget"]["in_band"] is True, body["budget"]


@pytest.mark.parametrize("side", ["under", "over"])
def test_just_outside_the_band_warns_but_still_builds(client, no_gateway,
                                                      side):
    budget, low, high = _band()
    words = (low - 1) if side == "under" else (high + 1)
    response = client.post("/api/plan/manual", json=payload(words))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["budget"]["in_band"] is False
    assert body["budget"]["warning"], "the user must be told"


def test_a_script_the_length_gate_would_refuse_is_refused_at_the_form(
        client, no_gateway, settings):
    gate_min, gate_max = pre_render_range(settings.duration_min,
                                          settings.duration_max)
    too_long = int(gate_max * speech_rate()) + 20
    response = client.post("/api/plan/manual", json=payload(too_long))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "length" in detail.lower()
    assert ("%.0f" % gate_max) in detail


def test_a_script_far_too_short_is_refused_at_the_form(client, no_gateway,
                                                       settings):
    gate_min, _ = pre_render_range(settings.duration_min,
                                   settings.duration_max)
    too_short = max(beat_count(), int(gate_min * speech_rate()) - 20)
    response = client.post("/api/plan/manual", json=payload(too_short))
    assert response.status_code == 400
    assert "length" in response.json()["detail"].lower()


# --- the Devanagari rule ----------------------------------------------------

def test_the_devanagari_rule_is_the_agents_one_not_a_copy():
    assert pipeline.latin_violations is agents.latin_violations


def test_latin_script_in_voice_text_is_refused(client, no_gateway):
    body = payload()
    body["beats"][3]["voice_text"] = "यह एक magnetic anomaly है"
    response = client.post("/api/plan/manual", json=body)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "magnetic" in detail and "anomaly" in detail
    assert "b4" in detail


def test_one_stray_latin_letter_is_still_refused(client, no_gateway):
    body = payload()
    body["beats"][0]["voice_text"] = body["beats"][0]["voice_text"] + "s"
    assert client.post("/api/plan/manual", json=body).status_code == 400


def test_ascii_digits_in_voice_text_are_fine(client, no_gateway):
    body = payload()
    body["beats"][2]["voice_text"] = \
        body["beats"][2]["voice_text"].replace(DEVA, "1965", 1)
    assert client.post("/api/plan/manual", json=body).status_code == 200


def test_devanagari_abbreviation_and_punctuation_are_fine(client, no_gateway):
    body = payload()
    body["beats"][5]["voice_text"] = \
        body["beats"][5]["voice_text"].replace(DEVA, "ई॰पी॰", 1) + " — ठीक।"
    assert client.post("/api/plan/manual", json=body).status_code == 200


def test_roman_caption_text_is_never_flagged(client, no_gateway):
    body = payload()
    body["beats"][1]["caption_text"] = "DNA report adhoori chhod di gayi"
    assert client.post("/api/plan/manual", json=body).status_code == 200


# --- provenance -------------------------------------------------------------

def test_a_source_without_a_url_is_refused_at_the_form(client, no_gateway):
    body = payload()
    body["sources"] = [{"text": "500 kankaal mile", "source_url": ""}]
    response = client.post("/api/plan/manual", json=body)
    assert response.status_code == 400
    assert "source" in response.json()["detail"].lower()


def test_no_sources_needs_an_explicit_acknowledgement(client, no_gateway):
    body = payload()
    body["sources"] = []
    response = client.post("/api/plan/manual", json=body)
    assert response.status_code == 400
    assert "source" in response.json()["detail"].lower()

    body["acknowledge_unsourced"] = True
    ok = client.post("/api/plan/manual", json=body)
    assert ok.status_code == 200, ok.text
    plan = ReelPlan.model_validate(ok.json()["plan"])
    assert plan.provenance.claims == []
    # No claims means nothing unsourced, so QC cannot fail it after a render.
    card = run_qc(plan, actual_duration=45.0)
    assert [c for c in card.checks if c.name == "claim_provenance"][0].ok


def test_supplied_sources_reach_the_plan_and_clear_qc(client, no_gateway):
    plan = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=payload()).json()["plan"])
    assert [c.source_url for c in plan.provenance.claims] == [
        "https://example.org/roopkund"]
    card = run_qc(plan, actual_duration=45.0)
    assert [c for c in card.checks if c.name == "claim_provenance"][0].ok


# --- metadata ---------------------------------------------------------------

def test_metadata_left_blank_leaves_the_publish_409_to_handle_it(
        client, no_gateway):
    plan = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=payload()).json()["plan"])
    assert plan.metadata is None
    assert client.get(
        "/api/plan/%s/publish" % plan.plan_id).status_code == 409


def test_metadata_written_by_hand_reaches_the_publish_payload(client,
                                                              no_gateway):
    body = payload(metadata={"yt_title": "Roopkund: 500 Kankaal",
                             "yt_description": "Ek jheel ka sach.",
                             "ig_caption": "500 kankaal.",
                             "pinned_comment": "Tum kya sochte ho?",
                             "hashtags": ["#rahasya", "#roopkund"]})
    plan_id = client.post(
        "/api/plan/manual", json=body).json()["plan"]["plan_id"]
    preview = client.get("/api/plan/%s/publish" % plan_id)
    assert preview.status_code == 200
    assert preview.json()["youtube"]["snippet"]["title"] == \
        "Roopkund: 500 Kankaal"


# --- moderation -------------------------------------------------------------

def test_a_manual_plan_never_looks_moderated(client, no_gateway):
    plan = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=payload()).json()["plan"])
    assert plan.safety.moderation_passed is False
    assert plan.safety.moderation_unavailable
    card = run_qc(plan, actual_duration=45.0)
    check = [c for c in card.checks if c.name == "moderation"][0]
    assert check.kind == "warn" and not check.ok
    assert "NOT CHECKED" in check.detail
    assert any("MODERATION WAS NOT RUN" in item
               for item in publish_checklist(plan, "x.mp4"))


# --- dedup ------------------------------------------------------------------

def test_the_cheap_dedup_layers_still_run_without_a_gateway(client,
                                                            no_gateway):
    """Turned on explicitly: the gate ships off, because while the
    pipeline is being built the same topic gets run over and over. This
    test is about what the layers do when they run, not about the
    default, so it says which it wants."""
    client.app.state.settings.dedup_enabled = True
    first = client.post("/api/plan/manual", json=payload())
    assert first.status_code == 200
    plan_id = first.json()["plan"]["plan_id"]
    store: Store = client.app.state.store
    store.set_status(plan_id, "produced")

    again = client.post("/api/plan/manual", json=payload())
    assert again.status_code == 409
    assert "dedup" in again.json()["detail"]


def test_entities_supplied_by_hand_drive_the_cooldown(client, no_gateway):
    client.app.state.settings.dedup_enabled = True
    body = payload(entities=["Roopkund", "Uttarakhand"])
    plan = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=body).json()["plan"])
    assert plan.topic.entities == ["Roopkund", "Uttarakhand"]

    store: Store = client.app.state.store
    store.record_entities(plan.plan_id, plan.topic.entities)

    clash = client.post("/api/plan/manual", json=payload(
        entities=["Roopkund"], topic="Ek pahaadi jheel ka anjaana sach"))
    assert clash.status_code == 409
    assert "cooldown" in clash.json()["detail"]


def test_the_same_script_can_be_submitted_again_with_the_gate_off(
        client, no_gateway):
    """The default, and the reason the switch exists: a script gets
    rewritten and resubmitted a dozen times while it is being tuned."""
    first = client.post("/api/plan/manual", json=payload())
    assert first.status_code == 200
    client.app.state.store.set_status(first.json()["plan"]["plan_id"],
                                      "produced")

    again = client.post("/api/plan/manual", json=payload())

    assert again.status_code == 200
    assert again.json()["plan"]["plan_id"] !=         first.json()["plan"]["plan_id"]


def test_an_off_gate_says_so_on_the_manual_path_too(client, no_gateway):
    """Off must not read as clean here either."""
    body = client.post("/api/plan/manual", json=payload()).json()
    rows = [e for e in body.get("events", [])
            if e.get("stage") == "dedup"]
    assert rows, "the dedup stage vanished instead of reporting itself off"
    said = " ".join(str(e.get("detail", "")).lower() for e in rows)
    assert "off" in said
    assert all(e.get("status") != "done" for e in rows)


def test_without_entities_the_cooldown_layer_is_simply_inert(client,
                                                             no_gateway):
    plan = ReelPlan.model_validate(
        client.post("/api/plan/manual", json=payload()).json()["plan"])
    assert plan.topic.entities == []


# --- the shape of the form --------------------------------------------------

def test_a_beat_missing_its_required_text_is_refused(client, no_gateway):
    body = payload()
    body["beats"][4]["visual_prompt"] = "   "
    response = client.post("/api/plan/manual", json=body)
    assert response.status_code == 400
    assert "visual_prompt" in response.json()["detail"]


def test_an_invalid_motion_is_refused_by_the_literal(client, no_gateway):
    body = payload()
    body["beats"][0]["motion"] = "spin"
    assert client.post("/api/plan/manual", json=body).status_code == 422


def test_the_beat_count_is_checked_against_the_configured_one(client,
                                                              no_gateway):
    body = payload(beats=beats_of(word_budget(), beats=3))
    response = client.post("/api/plan/manual", json=body)
    assert response.status_code == 400
    assert str(beat_count()) in response.json()["detail"]

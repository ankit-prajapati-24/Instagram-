import inspect

import pytest
from fastapi.testclient import TestClient

import engine.publish.payloads as payloads
from engine.app import create_app
from engine.config import Settings
from engine.contract import Metadata
from engine.store import Store
from tests.factories import make_plan


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    # Point the gateway at a dead port so health resolves fast and
    # deterministically rather than reaching a real OmniRoute.
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def test_health_reports_three_gateway_states_and_ffmpeg(client):
    body = client.get("/api/health").json()
    assert body["omniroute"] in {"ready", "no_provider", "down"}
    assert body["ffmpeg"] is True
    assert body["voice"].startswith("hi-IN")
    assert body["daily_ceiling"] > 0
    assert "research" in body["stages"]


def test_root_serves_the_control_panel(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Rahasya" in response.text
    assert "Evidence log" in response.text


def test_plans_list_starts_empty(client):
    assert client.get("/api/plans").json() == []


def test_unknown_plan_is_404(client):
    assert client.get("/api/plan/nope").status_code == 404


def test_empty_topic_is_rejected(client):
    assert client.post("/api/plan", json={"topic": "   "}).status_code == 400


def _seed(client):
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")
    return store


def test_approve_requires_a_known_hook(client):
    _seed(client)
    bad = client.post("/api/plan/p1/approve", json={"chosen_hook": "h99"})
    assert bad.status_code == 400


def test_approve_persists_edits_and_clears_stale_timings(client):
    store = _seed(client)
    response = client.post("/api/plan/p1/approve", json={
        "chosen_hook": "h3",
        "beats": [{"beat_id": "b4", "voice_text": "नया वाक्य",
                   "caption_text": "Naya vaakya", "on_screen_text": "NAYA",
                   "visual_prompt": "a new scene", "motion": "zoom_out",
                   "transition": "blur"}]})
    assert response.status_code == 200

    plan = store.get_plan("p1")
    assert plan.script.chosen_hook == "h3"
    edited = next(b for b in plan.script.beats if b.beat_id == "b4")
    assert edited.caption_text == "Naya vaakya"
    assert edited.motion == "zoom_out"
    # Text changed, so the previous measurement must not be reused.
    assert edited.measured_seconds is None
    assert edited.words == []


def test_approve_copies_the_chosen_hook_into_beat_one(client):
    store = _seed(client)
    client.post("/api/plan/p1/approve", json={"chosen_hook": "h2"})
    plan = store.get_plan("p1")
    hook = next(h for h in plan.hooks if h.variant_id == "h2")
    assert plan.script.beats[0].caption_text == hook.caption_text
    assert plan.script.beats[0].measured_seconds is None


def test_approve_records_entities_for_the_cooldown_layer(client):
    store = _seed(client)
    client.post("/api/plan/p1/approve", json={"chosen_hook": "h1"})
    assert store.entity_last_seen("Roopkund") is not None


def test_publish_preview_builds_payloads_without_uploading(client):
    _seed(client)
    body = client.get("/api/plan/p1/publish").json()
    assert body["youtube"]["status"]["containsSyntheticMedia"] is True
    assert body["youtube"]["status"]["privacyStatus"] == "private"
    assert body["instagram"]["media_type"] == "REELS"
    assert "bharosa karoge?" in body["youtube"]["_pinned_comment"]
    assert any("sound on" in item for item in body["checklist"])
    assert "Nothing here has been published" in body["note"]


def test_publish_preview_needs_metadata(client):
    store: Store = client.app.state.store
    store.save_plan(make_plan(plan_id="p2", with_metadata=False),
                    status="draft")
    assert client.get("/api/plan/p2/publish").status_code == 409


def test_media_route_refuses_path_traversal(client):
    assert client.get("/media/..%2F..%2Fengine%2Fapp.py").status_code == 404
    assert client.get("/media/nothing.mp4").status_code == 404


def test_frame_route_404s_without_an_image(client):
    _seed(client)
    assert client.get("/api/frame/p1/b0").status_code == 404


def test_app_exposes_no_publish_mutation(client):
    routes = {(r.path, tuple(sorted(r.methods)))
              for r in client.app.routes if hasattr(r, "methods")}
    posts = {path for path, methods in routes if "POST" in methods}
    assert not any("publish" in path for path in posts)


def test_payload_module_makes_no_network_calls():
    source = inspect.getsource(payloads)
    for forbidden in ("httpx", "requests", "urllib", "socket", "aiohttp"):
        assert forbidden not in source


def test_youtube_payload_appends_sources_to_the_description():
    plan = make_plan()
    body = payloads.youtube_payload(plan, "out.mp4")
    assert "Sources:" in body["snippet"]["description"]
    assert "example.org/roopkund" in body["snippet"]["description"]
    assert body["snippet"]["defaultAudioLanguage"] == "hi"


def test_instagram_caption_carries_hashtags_and_stays_in_limit():
    plan = make_plan()
    body = payloads.instagram_payload(plan, "https://x/v.mp4")
    assert "#roopkund" in body["caption"]
    assert len(body["caption"]) <= 2200
    assert body["video_url"] == "https://x/v.mp4"


def test_title_is_truncated_to_the_platform_limit():
    plan = make_plan()
    plan.metadata = Metadata(yt_title="x" * 260, pinned_comment="a? b?")
    assert len(payloads.youtube_payload(plan, "o.mp4")["snippet"]["title"]) \
        == 100


def test_checklist_flags_placeholder_visuals():
    plan = make_plan()
    for beat in plan.script.beats:
        beat.image_provider = "placeholder"
    items = payloads.publish_checklist(plan, "o.mp4")
    assert any("placeholder visuals" in item for item in items)


def test_checklist_flags_unsourced_claims():
    items = payloads.publish_checklist(make_plan(sourced=False), "o.mp4")
    assert any("UNSOURCED" in item for item in items)

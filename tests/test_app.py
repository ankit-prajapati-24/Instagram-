import base64
import inspect
import io
import os
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import engine.publish.payloads as payloads
from engine.app import create_app
from engine.config import Settings
from engine.contract import Clip, Metadata
from engine.media.voice import caption_timings
from engine.store import Store
from tests.factories import make_plan

# A minimal, real, decodable 1x1 PNG. Used so "served directly" tests prove
# the route streams the file byte-for-byte rather than merely returning 200.
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII=")


def _clip(path, provider="pexels") -> Clip:
    return Clip(path=str(path), query="skeletal lake", provider=provider,
               duration=4.0)


def _seed_with_visual(client, *, clips=None, image_path=None, plan_id="p1"):
    """A one-beat plan whose beat has the given clips/image_path."""
    store: Store = client.app.state.store
    plan = make_plan(plan_id=plan_id, beats=1)
    beat = plan.script.beats[0].model_copy(update={
        "clips": clips or [], "image_path": image_path})
    plan.script.beats = [beat]
    store.save_plan(plan, status="awaiting_approval")
    return plan


def _skip_without_ffmpeg(settings: Settings) -> None:
    if not Path(settings.ffmpeg).exists():
        pytest.skip("ffmpeg is not available in this environment")


def _make_test_video(path: Path, ffmpeg: str) -> None:
    """A tiny, real 64x64 1-frame-per-second mp4, via ffmpeg's testsrc."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=64x64:rate=1", "-t", "1", "-pix_fmt",
         "yuv420p", "-y", str(path)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert path.is_file()


def _under_work(client, name: str) -> Path:
    """A path under this client's settings.work_dir.

    The frame route only serves/writes files contained under work_dir or
    out_dir (mirroring /media's containment check), so any file a test
    wants the route to treat as legitimate has to live there rather than
    at a bare tmp_path.
    """
    settings: Settings = client.app.state.settings
    path = Path(settings.work_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
    assert body["voice"], "a voice must always be reported"
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


def test_publish_preview_credits_lordicon_when_the_render_recorded_it(client):
    """The credit follows the render record, not today's filesystem.

    Drives the real route rather than the payload builders, because the
    defect this guards lived in the route: it called stickers_mod.prepare()
    and credited whatever a render started *now* would use.
    """
    from engine.assembly import stickers as stk
    store = _seed(client)
    store.record_render("p1", "render:p1:v1", "done", output_path="o.mp4",
                        duration_s=50.0, attribution=stk.ATTRIBUTION)

    body = client.get("/api/plan/p1/publish").json()

    assert stk.ATTRIBUTION in body["youtube"]["snippet"]["description"]
    assert stk.ATTRIBUTION in body["instagram"]["caption"]


def test_publish_preview_stays_silent_when_the_render_recorded_nothing(
        client, monkeypatch):
    """The regression, and the licence-relevant half of it.

    This plan's render recorded NULL -- it is one of the six in the live
    engine.db whose MP4 was written on 2026-09-17/18, before any baked art
    existed. prepare() is forced to return a baked sticker here, so the only
    way the credit can stay out of these payloads is if the route reads the
    record instead of recomputing. Before the fix this test fails on both
    payloads.
    """
    from engine.assembly import stickers as stk
    store = _seed(client)
    store.record_render("p1", "render:p1:v1", "done", output_path="o.mp4",
                        duration_s=50.0)          # NULL attribution

    baked = [stk.Sticker(
        name="death", emoji="💀", word="kankaal", beat_index=0,
        start=1.0, slot=0, style="dark", baked=True, size=184, canvas=232,
        frames=42, pattern="x-%03d.png", png="x-041.png",
        licence=stk.LICENCES["lordicon"])]
    # Patched on the module itself, so it bites whether the route holds a
    # reference to stickers_mod or imports prepare directly.
    monkeypatch.setattr(stk, "prepare", lambda *a, **k: baked)

    body = client.get("/api/plan/p1/publish").json()

    assert stk.ATTRIBUTION not in body["youtube"]["snippet"]["description"], (
        "credited Lordicon on a video rendered before any baked art existed")
    assert stk.ATTRIBUTION not in body["instagram"]["caption"], (
        "credited Lordicon on a video rendered before any baked art existed")


def test_publish_preview_stays_silent_when_there_was_no_render(client):
    """No render row at all -- the same NULL, reached another way."""
    from engine.assembly import stickers as stk
    _seed(client)
    body = client.get("/api/plan/p1/publish").json()
    assert stk.ATTRIBUTION not in body["youtube"]["snippet"]["description"]
    assert stk.ATTRIBUTION not in body["instagram"]["caption"]


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


def test_both_payloads_carry_the_lordicon_credit_when_art_rendered():
    """Proves both directions: the credit must show up when baked art
    rendered, and must NOT show up when it did not -- a suite that only
    checked the positive case would let a video with zero Lordicon art
    ship with a Lordicon credit on it, which is its own kind of wrong."""
    from engine.assembly import stickers as stk
    baked = [stk.Sticker(
        name="death", emoji="\U0001f480", word="kankaal", beat_index=0,
        start=1.0, slot=0, style="punchy", baked=True, size=60, canvas=76,
        frames=48, pattern="x-%03d.png", png="x-047.png",
        licence=stk.LICENCES["lordicon"])]
    plan = make_plan()

    credit = stk.attribution_for(baked)
    yt = payloads.youtube_payload(plan, "out.mp4", attribution=credit)
    ig = payloads.instagram_payload(plan, "https://x/v.mp4",
                                    attribution=credit)
    assert stk.ATTRIBUTION in yt["snippet"]["description"]
    assert stk.ATTRIBUTION in ig["caption"]

    plain = payloads.youtube_payload(plan, "out.mp4")
    assert stk.ATTRIBUTION not in plain["snippet"]["description"]
    plain_ig = payloads.instagram_payload(plan, "https://x/v.mp4")
    assert stk.ATTRIBUTION not in plain_ig["caption"]

    unbaked = [stk.Sticker(
        name="water", emoji="\U0001f4a7", word="paani", beat_index=0,
        start=1.0, slot=0, style="punchy", baked=False, size=60, canvas=76,
        frames=7, pattern="e-%03d.png", png="e-006.png")]
    emoji_credit = stk.attribution_for(unbaked)
    yt_emoji = payloads.youtube_payload(plan, "out.mp4",
                                        attribution=emoji_credit)
    ig_emoji = payloads.instagram_payload(plan, "https://x/v.mp4",
                                          attribution=emoji_credit)
    assert stk.ATTRIBUTION not in yt_emoji["snippet"]["description"]
    assert stk.ATTRIBUTION not in ig_emoji["caption"]


def test_a_long_caption_loses_its_own_tail_not_the_lordicon_credit():
    """The credit is a licence requirement; the caption body is not.

    Appending the credit and then truncating drops it on exactly the
    hashtag-heavy captions this channel writes, and does so silently.
    """
    from engine.assembly import stickers as stk
    baked = [stk.Sticker(
        name="death", emoji="\U0001f480", word="kankaal", beat_index=0,
        start=1.0, slot=0, style="punchy", baked=True, size=184, canvas=232,
        frames=42, pattern="x-%03d.png", png="x-041.png",
        licence=stk.LICENCES["lordicon"])]

    plan = make_plan()
    plan.metadata.ig_caption = "x" * 3000

    body = payloads.instagram_payload(
        plan, "https://x/v.mp4", attribution=stk.attribution_for(baked))

    assert len(body["caption"]) <= payloads.MAX_IG_CAPTION
    assert body["caption"].endswith(stk.ATTRIBUTION)


def test_checklist_flags_placeholder_visuals():
    """Every beat's clips are placeholder-provider — the worst case, where
    the render leaned on blank gradient frames for the whole video."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.clips = [_clip("still.png", provider="placeholder")]
    items = payloads.publish_checklist(plan, "o.mp4")
    assert any("placeholder frames" in item for item in items)


def test_checklist_flags_still_image_fallback_visuals():
    """Every beat's clips fell back to a real still (e.g. an image-gateway
    provider), never reaching a blank placeholder frame. That is a milder
    problem than the placeholder case and should read differently."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.clips = [_clip("still.png", provider="omniroute")]
    items = payloads.publish_checklist(plan, "o.mp4")
    assert any("fell back to still images" in item for item in items)
    assert not any("placeholder frames" in item for item in items)


def test_checklist_flags_mixed_pexels_and_fallback_beats():
    """The common production shape: some beats got real Pexels footage,
    others fell back (one to a still, one to a placeholder). The checklist
    should call out only the beats that actually fell back, and should
    still separate the placeholder beat from the milder still-image one."""
    plan = make_plan()
    beats = plan.script.beats
    beats[0].clips = [_clip("clip.mp4", provider="pexels")]
    beats[1].clips = [_clip("still.png", provider="keyless")]
    beats[2].clips = [_clip("still.png", provider="placeholder")]
    for beat in beats[3:]:
        beat.clips = [_clip("clip.mp4", provider="pexels")]
    items = payloads.publish_checklist(plan, "o.mp4")
    placeholder_items = [i for i in items if "placeholder frames" in i]
    stills_items = [i for i in items if "fell back to still images" in i]
    assert len(placeholder_items) == 1
    assert beats[2].beat_id in placeholder_items[0]
    assert len(stills_items) == 1
    assert beats[1].beat_id in stills_items[0]
    assert beats[0].beat_id not in stills_items[0]
    assert beats[0].beat_id not in placeholder_items[0]


def test_checklist_flags_single_beat_with_mixed_providers():
    """A single beat whose clips list contains both real and fallback visuals.
    This is the production shape: roughly one clip per 2.5 seconds of narration,
    each resolved independently, so one beat can have 2 Pexels clips and 1
    placeholder or keyless still. The checklist must detect this within a single
    beat, not just across beats."""
    plan = make_plan()
    beats = plan.script.beats
    # Beat 0: pexels + placeholder in the same beat — worst case, should flag
    # as placeholder
    beats[0].clips = [
        _clip("clip1.mp4", provider="pexels"),
        _clip("still.png", provider="placeholder"),
    ]
    # Beat 1: pexels + keyless in the same beat — a milder fallback
    beats[1].clips = [
        _clip("clip1.mp4", provider="pexels"),
        _clip("still.png", provider="keyless"),
    ]
    # Beat 2+: all pexels (control)
    for beat in beats[2:]:
        beat.clips = [_clip("clip.mp4", provider="pexels")]

    items = payloads.publish_checklist(plan, "o.mp4")
    placeholder_items = [i for i in items if "placeholder frames" in i]
    stills_items = [i for i in items if "fell back to still images" in i]

    # Beat 0 should be in both (placeholder is the worst case)
    assert len(placeholder_items) == 1
    assert beats[0].beat_id in placeholder_items[0]

    # Beat 1 should be in stills_items but not placeholder_items
    assert len(stills_items) == 1
    assert beats[1].beat_id in stills_items[0]
    assert beats[0].beat_id not in stills_items[0]


def test_checklist_flags_legacy_image_provider_placeholder():
    """A plan stored before clips existed has no beat.clips at all — the
    legacy beat.image_provider field is the only signal, and must still be
    honoured so old stored plans keep getting warned."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.image_provider = "placeholder"
    items = payloads.publish_checklist(plan, "o.mp4")
    assert any("placeholder frames" in item for item in items)


def test_checklist_silent_when_all_beats_used_pexels():
    plan = make_plan()
    for beat in plan.script.beats:
        beat.clips = [_clip("clip.mp4", provider="pexels")]
    items = payloads.publish_checklist(plan, "o.mp4")
    assert not any("placeholder" in item or "fell back" in item
                   for item in items)


def test_checklist_does_not_flag_all_upload_beat_as_fallback():
    """A clip the user watched and replaced through the review gate carries
    provider="upload" — a deliberate, successful outcome, not a fallback.
    A beat made entirely of uploads must not be flagged, and the checklist
    should instead note that the whole video was hand-supplied."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.clips = [_clip("mine.mp4", provider="upload")]
    items = payloads.publish_checklist(plan, "o.mp4")
    assert not any("placeholder" in item or "fell back" in item
                   for item in items)
    assert any("hand-supplied" in item for item in items)


def test_checklist_does_not_flag_pexels_and_upload_mix():
    """Some beats got real Pexels footage, others were deliberately replaced
    by the user via upload. Both are successful, on-purpose outcomes, so
    neither should read as a fallback."""
    plan = make_plan()
    beats = plan.script.beats
    beats[0].clips = [_clip("clip.mp4", provider="pexels")]
    beats[1].clips = [_clip("mine.mp4", provider="upload")]
    for beat in beats[2:]:
        beat.clips = [_clip("clip.mp4", provider="pexels")]
    items = payloads.publish_checklist(plan, "o.mp4")
    assert not any("placeholder" in item or "fell back" in item
                   for item in items)
    # Not every visual was an upload here, so the informational upload-only
    # line must not appear either.
    assert not any("hand-supplied" in item for item in items)


def test_checklist_still_flags_upload_mixed_with_placeholder():
    """A beat mixing a deliberate upload with a genuine image-chain fallback
    (placeholder) must still be flagged — the upload does not launder the
    placeholder into looking fine."""
    plan = make_plan()
    beats = plan.script.beats
    beats[0].clips = [
        _clip("mine.mp4", provider="upload"),
        _clip("still.png", provider="placeholder"),
    ]
    for beat in beats[1:]:
        beat.clips = [_clip("clip.mp4", provider="pexels")]
    items = payloads.publish_checklist(plan, "o.mp4")
    placeholder_items = [i for i in items if "placeholder frames" in i]
    assert len(placeholder_items) == 1
    assert beats[0].beat_id in placeholder_items[0]


def test_checklist_flags_unsourced_claims():
    items = payloads.publish_checklist(make_plan(sourced=False), "o.mp4")
    assert any("UNSOURCED" in item for item in items)


# --- the human gate, which had three separate holes in it -------------------

def test_produce_refuses_a_plan_that_was_never_approved(client):
    """The gate is a constraint, so the API enforces it, not just the UI."""
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")
    response = client.post("/api/plan/p1/produce", json={})
    assert response.status_code == 409
    assert "not approved" in response.json()["detail"]


def test_produce_accepts_an_approved_plan(client, monkeypatch):
    """The gate lets an approved plan through, and the work starts.

    ``produce_stage`` is stubbed, and that is not laziness. The route
    starts a *daemon* thread and returns immediately, so an unstubbed call
    here leaves a real voice synthesis running for the rest of the pytest
    session — into later tests, three of which replace ``subprocess.run``
    with a counting stub. ``monkeypatch.setattr("engine.app.subprocess.
    run", ...)`` resolves to the shared ``subprocess`` module, so that
    replacement is process-wide: the leaked thread's ffmpeg calls landed on
    their counter and those tests failed roughly one run in six, on this
    branch and before it. Waiting for the stub to be called is what makes
    the thread finish inside the test that started it.
    """
    import engine.app as app_mod

    started = threading.Event()

    def _stub(plan, client_, store_, settings_, **kwargs):
        started.set()
        return {"video": "stub.mp4", "probe": {}, "scorecard": {}}

    monkeypatch.setattr(app_mod, "produce_stage", _stub)

    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="approved")
    assert client.post("/api/plan/p1/produce",
                       json={"use_fake": True}).status_code == 200
    assert started.wait(30), "the produce stage was never reached"


def test_approve_rejects_an_invalid_motion_instead_of_storing_it(client):
    """`model_copy` does not validate, so "spin" used to persist and then
    made every later read of that plan raise ValidationError - a permanent
    500 on that plan with no repair path."""
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")

    response = client.post("/api/plan/p1/approve", json={
        "chosen_hook": "h1",
        "beats": [{"beat_id": "b4", "voice_text": "क", "caption_text": "k",
                   "visual_prompt": "p", "motion": "spin",
                   "transition": "fade"}]})
    assert response.status_code == 422

    # and the stored plan is still readable
    assert store.get_plan("p1") is not None
    assert client.get("/api/plan/p1").status_code == 200


def test_approve_rejects_an_invalid_transition(client):
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")
    response = client.post("/api/plan/p1/approve", json={
        "chosen_hook": "h1",
        "beats": [{"beat_id": "b4", "voice_text": "क", "caption_text": "k",
                   "visual_prompt": "p", "motion": "zoom_in",
                   "transition": "warp"}]})
    assert response.status_code == 422


def test_approve_keeps_a_human_edit_to_the_hook_beat(client):
    """Beat 1 is the retention lever, and it was the one beat the gate
    silently overwrote: the hook was applied after the edits."""
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")

    client.post("/api/plan/p1/approve", json={
        "chosen_hook": "h3",
        "beats": [{"beat_id": "b0",
                   "voice_text": "मेरा लिखा हुआ हुक",
                   "caption_text": "HUMAN EDIT OF HOOK",
                   "visual_prompt": "a reworked opening frame",
                   "motion": "zoom_in", "transition": "fade"}]})

    beat = store.get_plan("p1").script.beats[0]
    assert beat.caption_text == "HUMAN EDIT OF HOOK"
    assert beat.visual_prompt == "a reworked opening frame"


def test_approve_still_seeds_the_hook_when_beat_one_is_not_edited(client):
    store: Store = client.app.state.store
    store.save_plan(make_plan(), status="awaiting_approval")
    client.post("/api/plan/p1/approve", json={"chosen_hook": "h2"})

    plan = store.get_plan("p1")
    hook = next(h for h in plan.hooks if h.variant_id == "h2")
    assert plan.script.beats[0].caption_text == hook.caption_text


def test_health_reports_the_voice_engine_actually_in_use(client):
    """It reported the edge-tts fallback voice while Piper was working."""
    body = client.get("/api/health").json()
    assert body["voice_engine"] == "piper"
    assert body["voice"] == "pratham"


def test_a_pasted_list_of_topics_is_refused_with_a_useful_message(client):
    """Five topics at once produced an unopenable filename and a script
    that tried to cover five unrelated stories."""
    five = ("Jodhpur mein 2012 ka dhamaka jiska koi malba nahi mila "
            "Kongka La pass par ITBP jawano ne kya dekha Mumbai ke Grant "
            "Road par 1982 mein ek poori building raatorat khaali kyun "
            "karayi gayi Nagaur ka woh kuan jisme 1947 ke baad koi nahi "
            "utra Shani Shingnapur ke gharon mein darwaze kyun nahi hote")
    for route in ("/api/plan", "/api/plan/async"):
        response = client.post(route, json={"topic": five})
        assert response.status_code == 400, route
        detail = response.json()["detail"]
        assert "more than one topic" in detail
        assert "One mystery per video" in detail


def test_a_normal_length_topic_passes_the_check(client):
    """It must reject a pasted list without rejecting a real topic."""
    response = client.post("/api/plan/async", json={
        "topic": "Kongka La pass par ITBP jawano ne kya dekha",
        "use_fake": True})
    assert response.status_code == 200


def test_every_pipeline_stage_has_a_row_in_the_panel(client):
    """markStage() drops an event whose stage has no row, silently. A gate
    that refuses the run and reports nothing to the panel is worse than the
    stage order it protects.

    Matched as a STAGES entry, not as a bare quoted word: "hooks",
    "captions", "metadata" and "dedup" all occur elsewhere in the page, so
    the loose form passed for four stages with their rows deleted.
    """
    from engine.pipeline import Stage

    page = client.get("/").text
    for stage in Stage.ORDER:
        assert f'["{stage}",' in page, f"no panel row for {stage}"


def test_the_panel_reports_clip_providers(client):
    """A run that fell back to stills must not look like a run that got
    footage. The old strip only knew about image_provider.

    The regression this guards against is subtler than "the word clips
    appears somewhere" (it always does, e.g. in STAGES) or "image_provider
    is gone" (necessary but not sufficient): the mixed-beat flag has to be
    an ANY-clip-fell-back check, not an ALL-clips-fell-back one. A beat that
    got one real Pexels clip and one placeholder still is a fallback beat
    and must be flagged; an all-or-nothing check would hide it."""
    page = client.get("/").text
    assert "image_provider" not in page
    # Pins the exact "ph" (needs-attention) condition in renderStrip. If a
    # future edit regresses this to an all-or-nothing `.every(...)` check,
    # this substring disappears from the page and the test fails.
    assert "b.clips.some((c) => c.provider !== 'pexels')" in page


# -- frame route --------------------------------------------------------


def test_frame_serves_an_image_clip_directly(client):
    image = _under_work(client, "still.png")
    image.write_bytes(PNG_1x1)
    _seed_with_visual(client, clips=[_clip(image)])

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 200
    assert response.content == PNG_1x1


def test_frame_extracts_a_poster_from_a_video_clip(client):
    settings: Settings = client.app.state.settings
    _skip_without_ffmpeg(settings)
    video = _under_work(client, "clip.mp4")
    _make_test_video(video, settings.ffmpeg)
    _seed_with_visual(client, clips=[_clip(video)])

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 200
    # A real, decodable JPEG -- not the mp4 bytes, and not a stub.
    assert response.content[:2] == b"\xff\xd8"
    image = Image.open(io.BytesIO(response.content))
    image.verify()
    image = Image.open(io.BytesIO(response.content))  # verify() closes it
    assert image.format == "JPEG"
    assert image.size == (64, 64)


def test_frame_reuses_the_cached_poster_instead_of_re_extracting(
        client, monkeypatch):
    settings: Settings = client.app.state.settings
    _skip_without_ffmpeg(settings)
    video = _under_work(client, "clip.mp4")
    _make_test_video(video, settings.ffmpeg)
    _seed_with_visual(client, clips=[_clip(video)])

    first = client.get("/api/frame/p1/b0")
    assert first.status_code == 200

    def _boom(*args, **kwargs):
        raise AssertionError(
            "ffmpeg ran again; the cached poster should have been reused")
    monkeypatch.setattr("engine.app.subprocess.run", _boom)

    second = client.get("/api/frame/p1/b0")

    assert second.status_code == 200
    assert second.content == first.content


def test_frame_falls_back_to_the_legacy_image_path(client):
    """Plans stored before clips existed still show a thumbnail."""
    image = _under_work(client, "legacy.png")
    image.write_bytes(PNG_1x1)
    _seed_with_visual(client, clips=[], image_path=str(image))

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 200
    assert response.content == PNG_1x1


def test_frame_prefers_an_existing_clip_over_stale_ones_and_the_legacy_image(
        client):
    missing = _under_work(client, "gone.mp4")  # never written to disk
    used = _under_work(client, "second.png")
    used.write_bytes(PNG_1x1)
    legacy = _under_work(client, "legacy.jpg")
    legacy.write_bytes(b"not the file that should be served")
    _seed_with_visual(client, clips=[_clip(missing), _clip(used)],
                      image_path=str(legacy))

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 200
    assert response.content == PNG_1x1


def test_frame_404s_naming_which_case_it_hit(client):
    _seed_with_visual(client, clips=[], image_path=None)

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 404
    detail = response.json()["detail"].lower()
    assert "clip" in detail
    assert "image" in detail


# -- frame route: cache poisoning (round-1 review finding (a)) ----------


def test_frame_leaves_no_cache_file_and_retries_after_a_nonzero_ffmpeg_exit(
        client, monkeypatch):
    """ffmpeg exiting non-zero must not leave anything at the cache path,
    or the next request would see "cache present" and serve a corrupt or
    truncated leftover forever instead of retrying."""
    video = _under_work(client, "clip.mp4")
    video.write_bytes(b"stand-in bytes; the fake ffmpeg below never reads "
                      b"them, only is_file() sees this clip exists")
    _seed_with_visual(client, clips=[_clip(video)])
    poster = video.with_name(video.name + ".poster.jpg")

    calls = {"n": 0}

    def _fails(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"",
                                           stderr=b"decode error")
    monkeypatch.setattr("engine.app.subprocess.run", _fails)

    first = client.get("/api/frame/p1/b0")
    assert first.status_code == 404
    assert not poster.exists()
    assert calls["n"] == 1

    second = client.get("/api/frame/p1/b0")
    assert second.status_code == 404
    assert not poster.exists()
    assert calls["n"] == 2, "a failed extraction must be retried, not cached"


def test_frame_leaves_no_cache_file_and_retries_after_an_empty_ffmpeg_output(
        client, monkeypatch):
    """ffmpeg can exit 0 while writing a malformed/undecodable jpg (an empty
    file stands in for that here). The size check must catch it, and -- like
    the non-zero case -- nothing may be left at the cache path."""
    video = _under_work(client, "clip.mp4")
    video.write_bytes(b"stand-in bytes; existence is all is_file() checks")
    _seed_with_visual(client, clips=[_clip(video)])
    poster = video.with_name(video.name + ".poster.jpg")

    calls = {"n": 0}

    def _writes_empty(cmd, **kwargs):
        calls["n"] += 1
        # The output path is ffmpeg's last argument, a temp file the route
        # created -- write it, but empty, the way an ffmpeg that "succeeded"
        # without decoding a frame would.
        Path(cmd[-1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"",
                                           stderr=b"")
    monkeypatch.setattr("engine.app.subprocess.run", _writes_empty)

    first = client.get("/api/frame/p1/b0")
    assert first.status_code == 404
    assert not poster.exists()
    assert calls["n"] == 1

    second = client.get("/api/frame/p1/b0")
    assert second.status_code == 404
    assert not poster.exists()
    assert calls["n"] == 2, "an empty extraction must be retried, not cached"


# -- frame route: path containment (round-1 review finding (b)) ---------


def test_frame_route_refuses_a_clip_outside_work_and_out_dirs(
        client, tmp_path):
    """Not reachable today (no API lets a caller set a beat's clip path),
    but the route reads from disk keyed by URL input, so it gets the same
    containment check /media has, mirroring test_media_route_refuses_path_
    traversal."""
    outside = tmp_path / "outside.png"  # sibling of settings.work_dir/out_dir
    outside.write_bytes(PNG_1x1)
    _seed_with_visual(client, clips=[_clip(outside)])

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 404


def test_frame_route_refuses_a_legacy_image_path_outside_work_and_out_dirs(
        client, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_1x1)
    _seed_with_visual(client, clips=[], image_path=str(outside))

    response = client.get("/api/frame/p1/b0")

    assert response.status_code == 404


# -- frame route: stale poster after a re-produce (round-1 finding (c)) -


def test_frame_re_extracts_when_the_clip_is_newer_than_the_cached_poster(
        client):
    """A re-produce of the same plan overwrites clip_00.mp4 in place with
    a newer mtime (generate_plan_clips does mkdir(exist_ok=True), no
    clearing, deterministic filenames). The stale poster next to it must
    not be served forever -- it must be re-extracted."""
    settings: Settings = client.app.state.settings
    _skip_without_ffmpeg(settings)
    video = _under_work(client, "clip.mp4")
    _make_test_video(video, settings.ffmpeg)
    _seed_with_visual(client, clips=[_clip(video)])

    first = client.get("/api/frame/p1/b0")
    assert first.status_code == 200
    poster = video.with_name(video.name + ".poster.jpg")
    assert poster.is_file()
    stale_mtime = poster.stat().st_mtime

    # Simulate the re-produce: the clip is overwritten in place and its
    # mtime moves forward, while the cached poster is left where it was.
    future = stale_mtime + 120
    os.utime(video, (future, future))

    second = client.get("/api/frame/p1/b0")

    assert second.status_code == 200
    assert poster.stat().st_mtime > stale_mtime, (
        "the poster must be re-extracted when the clip is newer than it")


# -- sticker picker: candidates, choose, preview -------------------------


def _approved_plan_with_voice(tmp_path, plan_id="p1"):
    """A client plus a saved, approved plan whose beat 0 carries real word
    timings.

    Modelled on the ``client`` fixture above, not built from it, because
    these tests need the ``store`` the app was built with, and the fixture
    only hands back the client. Beat 0 keeps ``make_plan``'s stock caption
    ("...kankaal mile"), which fires the ``death`` trigger -- but only once
    it has ``words`` on it. ``find_cues`` reads ``Beat.words`` for its
    clock, and those are only ever filled by
    ``engine.media.voice.caption_timings``, the same call the VOICE stage
    makes; a plan fresh out of ``make_plan`` has none, and every cue-driven
    test below would see an empty candidate list without this.
    """
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    client = TestClient(create_app(settings=settings))
    store: Store = client.app.state.store

    plan = make_plan(plan_id=plan_id)
    beat = plan.script.beats[0]
    beat.words = caption_timings(beat.caption_text, beat.seconds())
    store.save_plan(plan, status="approved")
    return client, store, plan_id


def test_sticker_candidates_lists_one_row_per_fired_cue(tmp_path, monkeypatch):
    from engine.assembly import sticker_catalog as cat
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: (
        "2130-skull-poison", "2841-crashed-skull", "1195-earthworm"))

    body = client.get(f"/api/plan/{plan_id}/stickers").json()

    assert body["rows"], "a cue should have fired"
    row = body["rows"][0]
    assert row["trigger"] == "death"
    assert row["word"]
    assert row["candidates"]
    assert all("slug" in c and "preview" in c for c in row["candidates"])
    assert "1195-earthworm" not in [c["slug"] for c in row["candidates"]]


def test_sticker_candidates_say_so_when_the_catalogue_is_unreachable(
        tmp_path, monkeypatch):
    from engine.assembly import noto_catalog as noto
    from engine.assembly import sticker_catalog as cat
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    def boom(*a, **k):
        raise cat.CatalogUnavailable("no route to host")

    # Both, because there are two libraries now. Stubbing only one left the
    # other reaching the real network from inside a unit test -- it fetched
    # the live manifest and answered with real candidates, which is how
    # this assertion started failing.
    monkeypatch.setattr(cat, "refresh", boom)
    monkeypatch.setattr(noto, "refresh", boom)

    body = client.get(f"/api/plan/{plan_id}/stickers").json()
    assert body["rows"] == [] or all(
        r["candidates"] == [] for r in body["rows"])
    assert "no route to host" in body.get("note", "")


def test_one_library_being_down_does_not_cost_the_other_its_candidates(
        tmp_path, monkeypatch):
    """Two libraries, so "the catalogue is unreachable" is no longer one
    fact. Lordicon being down must still leave Noto's emoji on the board,
    and the note must say which one is missing."""
    import json

    from engine.assembly import noto_catalog as noto
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc

    client, store, plan_id = _approved_plan_with_voice(tmp_path)
    cache = sc.cache_root(client.app.state.settings)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / noto.CACHE_NAME).write_text(json.dumps({"icons": [
        {"codepoint": "1f480", "popularity": 9, "tags": [":skull:"],
         "categories": ["Smileys and emotions"]}]}), encoding="utf-8")

    def boom(*a, **k):
        raise cat.CatalogUnavailable("no route to host")

    monkeypatch.setattr(cat, "refresh", boom)
    monkeypatch.setattr(noto, "refresh", lambda *a, **k: None)

    body = client.get(f"/api/plan/{plan_id}/stickers").json()
    assert "no route to host" in body.get("note", "")
    offered = [c["slug"] for r in body["rows"] for c in r["candidates"]]
    assert offered, "one library down took the other down with it"
    assert all(s.startswith("noto:") for s in offered), offered


def test_choosing_a_slug_outside_the_catalogue_is_refused_without_fetching(
        tmp_path, monkeypatch):
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: ("2130-skull-poison",))

    fetched = []
    monkeypatch.setattr(sc, "ensure_baked",
                        lambda *a, **k: fetched.append(a))

    resp = client.post(f"/api/plan/{plan_id}/sticker/b0",
                       json={"slug": "http://evil.test/x.gif"})
    assert resp.status_code == 400
    assert fetched == [], "nothing may be fetched for an unknown slug"
    assert store.sticker_choices(plan_id) == {}


def test_a_refused_icon_leaves_the_previous_choice_alone(tmp_path,
                                                          monkeypatch):
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: (
        "2130-skull-poison", "2816-skull-halloween"))
    monkeypatch.setattr(sc, "ensure_baked", lambda *a, **k: None)
    client.post(f"/api/plan/{plan_id}/sticker/b0",
                json={"slug": "2130-skull-poison"})

    def holed(*a, **k):
        raise ValueError("2816-skull-halloween has a pocket of white")

    monkeypatch.setattr(sc, "ensure_baked", holed)
    resp = client.post(f"/api/plan/{plan_id}/sticker/b0",
                       json={"slug": "2816-skull-halloween"})

    assert resp.status_code == 422
    assert "pocket of white" in resp.json()["detail"]
    assert store.sticker_choices(plan_id) == {"b0": "2130-skull-poison"}


def test_a_slug_outside_the_catalogue_404s_before_anything_is_fetched(
        tmp_path, monkeypatch):
    """The preview route's gate, and the order it has to hold in.

    ``preview_png`` downloads its source GIF on a cache miss, so a slug
    that reaches it is a slug the panel fetched an arbitrary URL for with
    its own network access -- the same hole the choose route's catalogue
    check exists to close. A status code alone cannot show that nothing was
    fetched, so the spy stands in for ``preview_png`` and returns a real
    file: without it, a route that got past the gate would fail on the
    response instead of on the assertion that matters.
    """
    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    client, store, plan_id = _approved_plan_with_voice(tmp_path)

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: ("2130-skull-poison",))

    stand_in = tmp_path / "preview.png"
    stand_in.write_bytes(PNG_1x1)
    rendered = []

    def spy_preview_png(slug, **kwargs):
        rendered.append(slug)
        return stand_in

    monkeypatch.setattr(sc, "preview_png", spy_preview_png)

    resp = client.get("/api/sticker-preview/1195-earthworm")

    # No `resp.text` here: a route that wrongly served this would put PNG
    # bytes into the failure message. The next assertion is the diagnosis.
    assert resp.status_code == 404
    assert rendered == [], (
        f"nothing may be fetched or rendered for an unknown slug: {rendered}")

    # The control: a slug that *is* in the catalogue goes through, so the
    # 404 above is the gate and not a route that never works.
    ok = client.get("/api/sticker-preview/2130-skull-poison")
    assert ok.status_code == 200, ok.text
    assert rendered == ["2130-skull-poison"]


def test_a_chosen_icon_survives_the_route_and_reaches_a_render(tmp_path,
                                                               monkeypatch):
    """The one test that crosses the route->render seam unmocked.

    Every other test on this feature stops at one end. The four route
    tests replace ``sticker_choices.ensure_baked``; the four ``prepare``
    tests hand-place a bake exactly where the reader expects one. So both
    ends passed six reviews while the writer baked into
    ``work_dir/_lordicon/bakes/`` and the reader looked in
    ``work_dir/bakes/`` -- every chosen icon silently discarded on every
    render, the feature inert, and no test able to see it.

    Nothing is faked between the two ends. The catalogue lookup is stubbed
    so no 5.4MB sitemap is fetched, and ``_download`` is stubbed only to
    hand over a GIF this repo already ships instead of reaching Lordicon.
    The bake, the stored choice, the settings object and the resolution are
    all the real ones -- ``settings`` here *is* the object the routes were
    built with, so the two ends cannot drift apart inside the test.

    ``death`` has committed art, so ``baked is True`` cannot on its own
    tell a hit on the chosen bake from a fall-through to the committed
    one. The slug in the pattern is what discriminates, and it is the
    assertion that goes red when the two roots disagree.
    """
    import shutil

    from engine.assembly import sticker_catalog as cat
    from engine.assembly import sticker_choices as sc
    from engine.assembly import stickers as stk

    source = Path("assets/lordicon/death.gif")
    if not source.exists():                    # pragma: no cover - env
        pytest.skip("run scripts/fetch_sticker_art.py first")

    client, store, plan_id = _approved_plan_with_voice(tmp_path)
    settings = client.app.state.settings
    slug = "2130-skull-poison"

    monkeypatch.setattr(cat, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(cat, "load", lambda *a, **k: (slug,))
    # The only fake between the ends, and it substitutes a local file for a
    # network fetch -- it does not stand in for the bake.
    monkeypatch.setattr(sc, "_download",
                        lambda library, s, dest: shutil.copyfile(source, dest))

    resp = client.post(f"/api/plan/{plan_id}/sticker/b0",
                       json={"slug": slug})
    assert resp.status_code == 200, resp.text
    assert store.sticker_choices(plan_id) == {"b0": slug}

    plan = store.get_plan(plan_id)
    prepared = stk.prepare(plan, settings,
                           choices=store.sticker_choices(plan_id))

    assert prepared, "the death cue must still fire"
    chosen = prepared[0]
    assert chosen.name == "death"
    assert chosen.baked is True
    assert slug in chosen.pattern, (
        "the bake the route just wrote never reached the render -- the "
        f"writer and the reader disagree about the root: {chosen.pattern}")
    assert Path(chosen.pattern % 0).exists(), "the frames must be on disk"


# --- the sticker picker panel -----------------------------------------------
#
# Same test shape the voice board uses (tests/test_voice_review.py, around
# lines 660-713): hand-written HTML with no JS harness gets no behavioural
# coverage from these two, only the two failures that would otherwise reach
# the browser silently -- a typo'd id, and a URL built for a route that does
# not exist. They live here rather than in test_voice_review.py because
# that file is the voice board's own regression guard and is off limits to
# this change; the sticker route tests just above already live in this
# file, so this is the section's other half.


def test_the_sticker_panel_only_reaches_for_ids_that_exist(client):
    """A typo'd id breaks this board the same silent way it breaks the
    voice one: ``$("stickergird")`` is null, the handler throws, and the
    section simply never renders. Only the statically-written ids are
    checked here -- the per-trigger ones (``stickercard-death`` and so on)
    are built at runtime from the trigger name, same as the voice board's
    per-beat ids.
    """
    import re as _re

    page = (Path(__file__).resolve().parents[1] / "engine" / "ui"
            / "index.html").read_text(encoding="utf-8")
    defined = set(_re.findall(r'id="([A-Za-z0-9_-]+)"', page))
    used = set(_re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', page))

    assert not used - defined, f"panel reaches for missing ids: "\
                               f"{sorted(used - defined)}"
    # And the ids the sticker picker added are among them, so the assertion
    # above cannot pass by this section having quietly failed to land.
    assert {"stickergrid", "sticker-note"} <= defined


def test_the_sticker_panel_calls_routes_that_are_registered(client):
    """Every sticker URL the panel builds or consumes resolves to a route.

    The candidates route is the only one the panel builds itself --
    ``/api/plan/${PLAN.plan_id}/stickers`` as a template literal in
    ``loadStickers()``. The choose and preview URLs arrive ready-made in
    that response (each row's ``choose``, each candidate's ``preview``)
    and the panel just forwards them to ``fetch`` and to an ``<img src>``
    rather than rebuilding them, so there is no literal path template for
    those two to grep for. What can still typo silently is the property
    name the panel reads off the JSON (``row.choose``, ``c.preview``), so
    that is what is checked for those two, alongside confirming the route
    they point at actually exists and answers the verb the panel uses.
    """
    page = (Path(__file__).resolve().parents[1] / "engine" / "ui"
            / "index.html").read_text(encoding="utf-8")
    paths = {getattr(r, "path", "") for r in client.app.routes}

    assert "/api/plan/${PLAN.plan_id}/stickers`" in page, \
        "the panel never calls the sticker candidates route"
    assert "/api/plan/{plan_id}/stickers" in paths

    assert "row.choose" in page, \
        "the panel never posts to the URL a row says to choose with"
    assert "/api/plan/{plan_id}/sticker/{beat_id}" in paths

    assert "c.preview" in page, \
        "the panel never shows a candidate's preview image"
    assert "/api/sticker-preview/{slug}" in paths

    verbs = {}
    for route in client.app.routes:
        p = getattr(route, "path", "")
        if p in ("/api/plan/{plan_id}/stickers",
                 "/api/plan/{plan_id}/sticker/{beat_id}",
                 "/api/sticker-preview/{slug}"):
            verbs[p] = set(getattr(route, "methods", []))
    assert "GET" in verbs.get("/api/plan/{plan_id}/stickers", set())
    assert "POST" in verbs.get("/api/plan/{plan_id}/sticker/{beat_id}", set())
    assert "GET" in verbs.get("/api/sticker-preview/{slug}", set())

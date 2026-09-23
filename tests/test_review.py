"""The second human gate: review every clip before the render.

Stock footage frequently does not match the story — a real run produced a
European city park for a script about skeletons in a frozen Himalayan lake —
and until now the first sight of it was the finished MP4, ten minutes later.
This gate stops the pipeline after CLIPS so every slot can be looked at and,
when it is wrong, replaced by hand with a video or a still the user found
themselves.

Why this file runs ffmpeg instead of asserting on strings: three bugs in this
branch shipped past a green suite because the test mocked the thing that was
broken or checked the filtergraph's *text*. A graph is a string until ffmpeg
configures it, and "the uploaded image is in the video" is a claim about
pixels. So the two replacement tests render for real and read the frame back.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.contract import Clip
from engine.store import Store
from tests.factories import make_plan

# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    # A dead port, so nothing in these tests waits on a real gateway.
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _store(client) -> Store:
    return client.app.state.store


def _settings(client) -> Settings:
    return client.app.state.settings


def _seed(client, *, status="awaiting_clip_review", beats=2, clips_per=2,
          plan_id="p1"):
    """A plan whose beats carry ``clips_per`` slots each."""
    store = _store(client)
    plan = make_plan(plan_id=plan_id, beats=beats)
    for beat in plan.script.beats:
        span = beat.seconds() / clips_per
        beat.clips = [
            Clip(path=str(Path(_settings(client).work_dir) / plan_id /
                          "clips" / beat.beat_id / f"clip_{slot:02d}.mp4"),
                 query=f"{beat.visual_prompt} shot {slot}",
                 provider="pexels", duration=span)
            for slot in range(clips_per)]
    store.save_plan(plan, status=status)
    return plan


def _wait_for(store: Store, plan_id: str, statuses: set[str],
              timeout: float = 240.0) -> str:
    """Block until the background thread has moved the plan on."""
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        seen = store.plan_status(plan_id)
        if seen in statuses:
            return seen
        time.sleep(0.05)
    raise AssertionError(
        f"plan {plan_id} is still {seen!r} after {timeout}s, "
        f"never reached one of {sorted(statuses)}")


def _wait_until(predicate, what: str, timeout: float = 30.0) -> None:
    """Block until a stubbed half has actually been called.

    The stubs deliberately do not write a status, so the status is not
    what a stubbed run can be waited on — waiting on one the stub never
    changes is how a test passes without the work having happened.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


PNG_MAGENTA = None       # built per test with ffmpeg; see _flat_still


# --- the split: produce stops after clips -----------------------------------


class _Recorder:
    def __init__(self, result=None, fill=False):
        self.calls = 0
        self.result = result or {}
        self.fill = fill

    def __call__(self, plan, *args, **kwargs):
        self.calls += 1
        if self.fill:
            for beat in plan.script.beats:
                if not beat.clips:
                    beat.clips = [Clip(path=f"{beat.beat_id}.mp4",
                                       query="q", provider="pexels",
                                       duration=beat.seconds())]
        return self.result


def _stub_halves(monkeypatch, *, clips=None, render=None):
    import engine.app as app_mod

    clips = clips or _Recorder({"pexels": 2}, fill=True)
    render = render or _Recorder({"video": "x.mp4", "probe": {},
                                  "scorecard": {"passed": True},
                                  "providers": {}})
    monkeypatch.setattr(app_mod, "clips_stage", clips)
    monkeypatch.setattr(app_mod, "render_stage", render)
    monkeypatch.setattr(app_mod, "produce_stage", _Recorder({
        "video": "one-shot.mp4", "probe": {}, "scorecard": {"passed": True},
        "providers": {}}))
    return clips, render


def test_producing_with_review_stops_after_clips(client, monkeypatch):
    """VOICE -> LENGTH -> CLIPS runs; CAPTIONS -> RENDER -> QC does not."""
    clips, render = _stub_halves(monkeypatch)
    _seed(client, status="approved")

    assert client.post("/api/plan/p1/produce",
                       json={"review_clips": True}).status_code == 200
    _wait_for(_store(client), "p1", {"awaiting_clip_review"}, timeout=30)

    assert clips.calls == 1, "the clip half did not run"
    assert render.calls == 0, "the render half ran through the review gate"


def test_a_plan_awaiting_clip_review_cannot_be_rendered(client, monkeypatch):
    """The gate is a status in the store, not a disabled button."""
    _stub_halves(monkeypatch)
    _seed(client, status="awaiting_clip_review")

    response = client.post("/api/plan/p1/produce", json={})
    assert response.status_code == 409
    assert "review" in response.json()["detail"].lower()


def test_releasing_the_review_runs_the_render_half(client, monkeypatch):
    clips, render = _stub_halves(monkeypatch)
    _seed(client, status="awaiting_clip_review")

    assert client.post("/api/plan/p1/clips/approve").status_code == 200
    _wait_until(lambda: render.calls == 1, "the render half to run")

    assert clips.calls == 0, "releasing re-ran the expensive clip half"
    assert _store(client).plan_status("p1") != "awaiting_clip_review"


def test_release_refuses_a_plan_that_is_not_awaiting_review(client,
                                                            monkeypatch):
    _stub_halves(monkeypatch)
    _seed(client, status="approved")

    response = client.post("/api/plan/p1/clips/approve")
    assert response.status_code == 409


def test_the_one_shot_path_is_still_available(client, monkeypatch):
    """Nobody is forced to review twenty clips to get a video."""
    import engine.app as app_mod
    clips, render = _stub_halves(monkeypatch)
    one_shot = app_mod.produce_stage
    _seed(client, status="approved")

    assert client.post("/api/plan/p1/produce", json={}).status_code == 200
    _wait_until(lambda: one_shot.calls == 1, "the one-shot produce to run")

    assert clips.calls == 0 and render.calls == 0, (
        "the one-shot path went through the review halves and could stop")
    assert _store(client).plan_status("p1") != "awaiting_clip_review"


# --- the review board -------------------------------------------------------


def test_the_review_lists_every_clip_not_every_beat(client):
    _seed(client, beats=3, clips_per=2)
    body = client.get("/api/plan/p1/clips").json()

    assert body["awaiting_review"] is True
    assert body["total"] == 6, "a beat holds several clips; all are reviewable"
    first = body["clips"][0]
    for key in ("beat_id", "slot", "duration", "provider", "query", "thumb"):
        assert key in first, f"the review board needs {key}"
    assert first["query"], "the query is how a wrong clip is understood"
    assert first["thumb"] == "/api/frame/p1/b0?slot=0"


def test_the_frame_route_serves_one_slot_rather_than_the_beat(client,
                                                              tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover - env guard
        pytest.skip("ffmpeg is not available in this environment")
    plan = _seed(client, beats=1, clips_per=2)

    # Only slot 1 exists on disk. Asking for slot 0 must not silently
    # serve slot 1's picture.
    target = Path(plan.script.beats[0].clips[1].path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _flat_still(settings.ffmpeg, target.with_suffix(".png"), "0xff00ff",
                64, 64)
    clip = plan.script.beats[0].clips[1].model_copy(
        update={"path": str(target.with_suffix(".png"))})
    plan.script.beats[0].clips[1] = clip
    _store(client).save_plan(plan, status="awaiting_clip_review")

    assert client.get("/api/frame/p1/b0?slot=1").status_code == 200
    assert client.get("/api/frame/p1/b0?slot=0").status_code == 404
    assert client.get("/api/frame/p1/b0?slot=9").status_code == 404


# --- uploads: the guards ----------------------------------------------------


def _upload(client, body, *, beat="b0", slot=0, content_type="image/png",
            filename="shot.png", plan_id="p1"):
    headers = {"Content-Type": content_type}
    if filename is not None:
        headers["X-Upload-Filename"] = filename
    return client.post(f"/api/plan/{plan_id}/clip/{beat}/{slot}",
                       content=body, headers=headers)


def _png_bytes(ffmpeg, tmp_path, colour="0xff00ff", size=64):
    path = tmp_path / f"{colour}.png"
    _flat_still(ffmpeg, path, colour, size, size)
    return path.read_bytes()


def test_a_traversal_filename_is_refused(client, tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    _seed(client)
    body = _png_bytes(settings.ffmpeg, tmp_path)

    response = _upload(client, body, filename="../../../../evil.png")
    assert response.status_code == 400
    assert "filename" in response.json()["detail"].lower()

    # And nothing was written anywhere near the traversal target.
    assert not (Path(settings.work_dir).parent / "evil.png").exists()


def test_a_wrong_content_type_is_refused(client, tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    _seed(client)

    # Declared as a PNG; the bytes are an MP4.
    video = tmp_path / "v.mp4"
    _flat_video(settings.ffmpeg, video, "0x00ff00", 1.0)
    mismatch = _upload(client, video.read_bytes(), content_type="image/png",
                       filename="lies.png")
    assert mismatch.status_code == 415

    # A type that is not media at all.
    plain = _upload(client, b"#!/bin/sh\nrm -rf /\n",
                    content_type="text/plain", filename="x.sh")
    assert plain.status_code == 415

    # PNG magic with a garbage body: it sniffs right and decodes wrong.
    fake = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
    assert _upload(client, fake, content_type="image/png").status_code == 415


def test_an_oversized_upload_is_refused(client, tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    settings.upload_max_mb = 0.001           # 1048 bytes
    _seed(client)
    body = _png_bytes(settings.ffmpeg, tmp_path, size=512)
    assert len(body) > 1048, "fixture is not actually oversized"

    response = _upload(client, body)
    assert response.status_code == 413
    uploads = Path(settings.work_dir) / "p1" / "uploads"
    assert not list(uploads.glob("*")) if uploads.exists() else True


def test_an_upload_lands_under_work_dir_and_is_marked_as_uploaded(
        client, tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    plan = _seed(client)
    before = plan.script.beats[0].clips[0].duration

    response = _upload(client, _png_bytes(settings.ffmpeg, tmp_path))
    assert response.status_code == 200, response.text

    stored = _store(client).get_plan("p1")
    clip = stored.script.beats[0].clips[0]
    assert clip.provider == "upload", "a replaced clip must be distinguishable"
    assert clip.duration == before, "the slot is fixed by the narration"
    path = Path(clip.path).resolve()
    assert Path(settings.work_dir).resolve() in path.parents
    assert path.is_file() and path.suffix == ".png"
    # The stored name is server-built, never the browser's.
    assert "shot" not in path.name


def test_an_upload_is_refused_outside_the_review_gate(client, tmp_path):
    """Replacing a clip is a thing you do at the gate, not whenever.

    Past it the render has already read every clip, so a swap would put a
    file on disk that the finished video does not contain — a plan that
    lies about its own footage.
    """
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    _seed(client, status="produced")
    response = _upload(client, _png_bytes(settings.ffmpeg, tmp_path))
    assert response.status_code == 409
    assert "review" in response.json()["detail"].lower()


def test_an_upload_refuses_a_slot_that_does_not_exist(client, tmp_path):
    settings = _settings(client)
    if not Path(settings.ffmpeg).exists():   # pragma: no cover
        pytest.skip("ffmpeg is not available in this environment")
    _seed(client, clips_per=2)
    body = _png_bytes(settings.ffmpeg, tmp_path)
    # The same request against a real slot succeeds, so a 404 below is the
    # slot being refused rather than the route being absent.
    assert _upload(client, body, slot=1).status_code == 200
    assert _upload(client, body, slot=7).status_code == 404
    assert _upload(client, body, beat="b99").status_code == 404


# --- real renders -----------------------------------------------------------
#
# Everything below runs ffmpeg end to end: upload, release the gate, render,
# then read the pixels back out of the finished MP4.


def _run_ffmpeg(ffmpeg, args):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


def _flat_still(ffmpeg, path, colour, width, height):
    _run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i",
                         f"color=c={colour}:size={width}x{height}",
                         "-frames:v", "1", str(path)])


def _flat_video(ffmpeg, path, colour, seconds, size=64):
    _run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i",
                         f"color=c={colour}:size={size}x{size}:rate=30:"
                         f"duration={seconds}",
                         "-pix_fmt", "yuv420p", str(path)])


def _source_audio(ffmpeg, path, seconds):
    _run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i",
                         f"sine=frequency=220:duration={seconds}",
                         "-ar", "48000", "-ac", "1", str(path)])


def _frame_rgb(ffmpeg, path, at):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}",
         "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], check=True, capture_output=True)
    assert result.stdout, f"no frame at {at}s of {path}"
    return result.stdout


def _mean_rgb(raw):
    n = len(raw) // 3
    return tuple(sum(raw[c::3]) / n for c in range(3))


def _render_client(tmp_path):
    settings = Settings()
    if not Path(settings.ffmpeg).exists():   # pragma: no cover - env guard
        pytest.skip("ffmpeg is not available in this environment")
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    settings.width, settings.height, settings.fps = 360, 640, 30
    settings.transition_duration = 0.4
    # A flat colour has to stay flat for the frame comparison to mean
    # anything: the grade desaturates and the grain re-rolls every pixel.
    settings.video_grade = False
    settings.video_grain = 0
    settings.music = False
    settings.sfx = False
    settings.stickers = False
    return TestClient(create_app(settings=settings)), settings


SECONDS = 2.2
SLOTS = 2


def _renderable_plan(client, settings, tmp_path):
    """Three beats of real audio, each split into two real clip slots."""
    store = _store(client)
    plan = make_plan(plan_id="p1", beats=3, measured=SECONDS)
    work = Path(settings.work_dir) / "p1"
    (work / "clips").mkdir(parents=True, exist_ok=True)
    for index, beat in enumerate(plan.script.beats):
        beat.role = "setup"           # no push: hold the picture still
        beat.transition = "fade"
        audio = work / f"{beat.beat_id}.wav"
        _source_audio(settings.ffmpeg, audio, SECONDS)
        beat.audio_path = str(audio)
        span = SECONDS / SLOTS
        clips = []
        for slot in range(SLOTS):
            path = work / "clips" / f"{beat.beat_id}-{slot}.mp4"
            _flat_video(settings.ffmpeg, path, "0x303030", span + 0.5)
            clips.append(Clip(path=str(path), query=f"grey plate {slot}",
                              provider="pexels", duration=span))
        beat.clips = clips
    store.save_plan(plan, status="awaiting_clip_review")
    return plan


def _slot_window(plan, settings, beat_index, slot):
    """When slot ``slot`` of beat ``beat_index`` is on screen.

    Derived from ``segment_lengths`` — the renderer's own arithmetic — so
    this test cannot disagree with the graph about where a clip lands.
    """
    from engine.assembly.render import segment_lengths

    durations = [b.seconds() for b in plan.script.beats]
    lengths, offsets, overlaps = segment_lengths(
        durations, settings.transition_duration)
    start = 0.0 if beat_index == 0 else offsets[beat_index - 1]
    slots = [c.duration for c in plan.script.beats[beat_index].clips]
    total = sum(slots)
    spans = [s * lengths[beat_index] / total for s in slots]
    begin = start + sum(spans[:slot])
    return begin, begin + spans[slot]


def _release_and_wait(client, settings):
    assert client.post("/api/plan/p1/clips/approve").status_code == 200
    _wait_for(_store(client), "p1", {"produced", "qc_failed"})
    row = next(r for r in _store(client).list_plans(10)
               if r["plan_id"] == "p1")
    assert row["video"], "no render was recorded"
    return Path(row["video"])


def _segment_for(plan, settings, index):
    """The one filtergraph segment that consumes input ``index``."""
    from engine.assembly.render import build_filter_graph

    graph, _total, _label = build_filter_graph(
        plan, fps=settings.fps, width=settings.width, height=settings.height,
        transition_duration=settings.transition_duration,
        grade=settings.video_grade, grain=settings.video_grain)
    return next(part for part in graph.split(";")
                if part.startswith(f"[{index}:v]"))


@pytest.mark.parametrize("kind,colour,check", [
    ("image", "0xff00ff", lambda r: r[0] > 170 and r[1] < 90 and r[2] > 170),
    ("video", "0x00ff00", lambda r: r[1] > 150 and r[0] < 100 and r[2] < 100),
])
def test_a_replacement_renders_in_its_slot_at_the_slot_duration(
        tmp_path, kind, colour, check):
    """Upload, release the gate, render for real, then read the pixels.

    The image case is the one the still branch exists for: an uploaded
    still gets ``zoompan`` (Ken Burns) where a video does not, and both are
    fitted to the slot the narration timeline fixed.
    """
    client, settings = _render_client(tmp_path)
    plan = _renderable_plan(client, settings, tmp_path)
    narration = sum(b.seconds() for b in plan.script.beats)

    if kind == "image":
        source = tmp_path / "hand-picked.png"
        _flat_still(settings.ffmpeg, source, colour, 240, 426)
        content_type, name = "image/png", "hand picked.png"
    else:
        source = tmp_path / "hand-picked.mp4"
        _flat_video(settings.ffmpeg, source, colour, 1.0)
        content_type, name = "video/mp4", "hand picked.mp4"

    # Beat 1, slot 1: away from both blends, and inside a beat that holds
    # more than one clip, which is the case a per-beat gate would miss.
    response = _upload(client, source.read_bytes(), beat="b1", slot=1,
                       content_type=content_type, filename=name)
    assert response.status_code == 200, response.text

    stored = _store(client).get_plan("p1")
    replaced = stored.script.beats[1].clips[1]
    assert replaced.provider == "upload"
    assert replaced.duration == SECONDS / SLOTS

    # Which render branch it took. `plan_inputs` already decides still vs
    # video and the still branch is the one that carries the Ken Burns
    # move, so an upload that lands on the wrong side of it would render
    # motionless (an image treated as video) or 400 seconds long (a video
    # fed to zoompan, whose `d` is output frames per *input* frame).
    from engine.assembly.render import plan_inputs

    index = 1 * SLOTS + 1
    path, is_video = plan_inputs(stored)[index]
    assert path == replaced.path
    assert is_video is (kind == "video")
    begin, end = _slot_window(stored, settings, 1, 1)
    segment = _segment_for(stored, settings, index)
    if kind == "image":
        assert "zoompan" in segment, segment
        # zoompan's `d` is output frames per input frame, so on a
        # single-frame input it is the slot's own length in frames.
        frames = max(int(round((end - begin) * settings.fps)), 1)
        assert f"d={frames}:" in segment, segment
    else:
        assert "zoompan" not in segment, segment
        assert "loop=" in segment and "trim=duration=" in segment, segment

    video = _release_and_wait(client, settings)

    from engine.assembly.render import probe_video
    probe = probe_video(video, settings.ffmpeg)
    assert abs(probe["duration"] - narration) < 0.25, (
        f"{probe['duration']:.2f}s rendered against {narration:.2f}s of "
        f"narration")

    at = (begin + end) / 2
    mean = _mean_rgb(_frame_rgb(settings.ffmpeg, video, at))
    assert check(mean), (
        f"the {kind} uploaded into b1/slot1 is not on screen at {at:.2f}s; "
        f"mean rgb {mean}")

    # And it is only there: beat 0 still shows the grey plate.
    other = _mean_rgb(_frame_rgb(settings.ffmpeg, video,
                                 sum(_slot_window(stored, settings, 0, 0))
                                 / 2))
    assert not check(other), (
        f"the replacement bled into beat 0; mean rgb {other}")

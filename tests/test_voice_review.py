"""The voice gate: hear every beat, and say any of them yourself.

Sits before the clip gate rather than beside it, and that ordering is the
whole design. A beat's clip count is ``ceil(measured / 2.5)`` and its slot
lengths divide the measured span, so footage fetched before the narration
is final has been cut to a length that no longer exists. Voice first,
clips second, render third.

Why this file runs ffmpeg rather than asserting on calls: the claim being
tested is "the audio the user handed over is in the finished video, at the
right moment". That is a claim about samples. The last time this repo
trusted a green suite built on mocks, five defects shipped — so the
end-to-end test here uploads a tone at one frequency into a narration made
of another, renders, and measures which frequency comes back out.
"""

from __future__ import annotations

import math
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

VOICE_REVIEW = "awaiting_voice_review"
CLIP_REVIEW = "awaiting_clip_review"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    # A dead port, so nothing here waits on a real gateway.
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _store(client) -> Store:
    return client.app.state.store


def _settings(client) -> Settings:
    return client.app.state.settings


def _tone(settings, path: Path, *, seconds: float, hz: int = 220) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
         "-ar", "44100", "-ac", "1", str(path)],
        check=True, capture_output=True)
    return path


def _seed(client, *, status=VOICE_REVIEW, beats=2, seconds=4.0,
          plan_id="p1"):
    """A plan that has been through VOICE: every beat has real audio."""
    settings = _settings(client)
    plan = make_plan(plan_id=plan_id, beats=beats, measured=seconds)
    audio_dir = Path(settings.work_dir) / plan_id / "audio"
    for beat in plan.script.beats:
        path = _tone(settings, audio_dir / f"{beat.beat_id}.mp3",
                     seconds=seconds)
        beat.audio_path = str(path)
        beat.voice_engine = "piper"
        beat.measured_seconds = seconds
    _store(client).save_plan(plan, status=status)
    return plan


def _wait_for(store: Store, plan_id: str, statuses: set[str],
              timeout: float = 240.0) -> str:
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        seen = store.plan_status(plan_id)
        if seen in statuses:
            return seen
        time.sleep(0.05)
    raise AssertionError(
        f"plan {plan_id} is still {seen!r} after {timeout}s, never reached "
        f"one of {sorted(statuses)}")


def _wait_until(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _Recorder:
    """A stubbed stage that remembers when it ran, relative to the others."""

    def __init__(self, result=None, log=None, name="stage", fill=False):
        self.calls = 0
        self.result = result or {}
        self.log = log if log is not None else []
        self.name = name
        self.fill = fill

    def __call__(self, plan, *args, **kwargs):
        self.calls += 1
        self.log.append(self.name)
        if self.fill:
            for beat in plan.script.beats:
                if not beat.clips:
                    beat.clips = [Clip(path=f"{beat.beat_id}.mp4",
                                       query="q", provider="pexels",
                                       duration=beat.seconds())]
        return self.result


def _stub_stages(monkeypatch):
    """Stub all three parts and record the order they ran in."""
    import engine.app as app_mod

    log: list[str] = []
    voice = _Recorder({"interpolated": 2}, log, "voice")
    clips = _Recorder({"pexels": 2}, log, "clips", fill=True)
    render = _Recorder({"video": "x.mp4", "probe": {},
                        "scorecard": {"passed": True}, "providers": {}},
                       log, "render")
    monkeypatch.setattr(app_mod, "voice_stage", voice)
    monkeypatch.setattr(app_mod, "clips_stage", clips)
    monkeypatch.setattr(app_mod, "render_stage", render)
    monkeypatch.setattr(app_mod, "produce_stage",
                        _Recorder({"video": "one-shot.mp4", "probe": {},
                                   "scorecard": {"passed": True},
                                   "providers": {}}, log, "one-shot"))
    return log, voice, clips, render


# --- the gate stops the run -------------------------------------------------


def test_producing_with_review_voice_stops_after_voice(client, monkeypatch):
    """VOICE runs; nothing after it does."""
    log, voice, clips, render = _stub_stages(monkeypatch)
    _seed(client, status="approved")

    assert client.post("/api/plan/p1/produce",
                       json={"review_voice": True}).status_code == 200
    _wait_for(_store(client), "p1", {VOICE_REVIEW})

    assert voice.calls == 1
    assert clips.calls == 0, "clips were fetched before the voice was heard"
    assert render.calls == 0


def test_voice_is_heard_before_clips_are_fetched(client, monkeypatch):
    """The ordering the whole gate exists for.

    Clip count and slot lengths are both derived from the measured
    narration, so footage fetched first is footage cut to a length the
    finished audio may not have.
    """
    log, _voice, _clips, _render = _stub_stages(monkeypatch)
    _seed(client, status="approved")

    client.post("/api/plan/p1/produce", json={"review_clips": True})
    _wait_for(_store(client), "p1", {CLIP_REVIEW})

    assert log[:2] == ["voice", "clips"]


def test_one_shot_produce_still_runs_neither_gate(client, monkeypatch):
    log, voice, clips, render = _stub_stages(monkeypatch)
    _seed(client, status="approved")

    client.post("/api/plan/p1/produce", json={})
    _wait_until(lambda: log == ["one-shot"], "the one-shot path")
    assert voice.calls == 0 and clips.calls == 0


def test_produce_refuses_a_plan_parked_at_the_voice_gate(client):
    """A reload, a retry or a curl meets the same refusal as the button."""
    _seed(client)
    response = client.post("/api/plan/p1/produce", json={})
    assert response.status_code == 409
    assert "voice" in response.json()["detail"]


def test_the_clip_gate_does_not_release_a_voice_parked_plan(client):
    _seed(client)
    response = client.post("/api/plan/p1/clips/approve")
    assert response.status_code == 409


def test_the_voice_gate_does_not_release_a_plan_that_is_not_at_it(client):
    _seed(client, status="approved")
    response = client.post("/api/plan/p1/voice/approve")
    assert response.status_code == 409


# --- releasing it -----------------------------------------------------------


def test_approving_the_voice_can_stop_again_at_the_clip_gate(
        client, monkeypatch):
    log, voice, clips, render = _stub_stages(monkeypatch)
    _seed(client)

    response = client.post("/api/plan/p1/voice/approve",
                           json={"review_clips": True})
    assert response.status_code == 200
    _wait_for(_store(client), "p1", {CLIP_REVIEW})

    assert clips.calls == 1
    assert render.calls == 0
    assert voice.calls == 0, "releasing the gate re-synthesised the voice"


def test_approving_the_voice_goes_on_to_the_render_by_default(
        client, monkeypatch):
    """``review_clips`` is asked again at the gate, and defaults to off."""
    log, voice, clips, render = _stub_stages(monkeypatch)
    _seed(client)

    assert client.post("/api/plan/p1/voice/approve").status_code == 200
    _wait_until(lambda: render.calls == 1, "the render half")
    assert clips.calls == 1
    assert voice.calls == 0


def test_releasing_the_gate_never_re_synthesises(client, monkeypatch):
    """The narration a human uploaded survives by never being regenerated.

    Not a promise about call order: ``voice_stage`` is simply not reachable
    from this route, the way ``render_stage`` cannot reach a model.
    """
    import engine.app as app_mod
    log, voice, clips, render = _stub_stages(monkeypatch)

    def explode(*args, **kwargs):
        raise AssertionError("voice_stage ran after the gate")

    monkeypatch.setattr(app_mod, "voice_stage", explode)
    _seed(client)

    assert client.post("/api/plan/p1/voice/approve").status_code == 200
    _wait_until(lambda: render.calls == 1, "the render half")


def test_a_failed_release_parks_the_plan_back_at_the_gate(
        client, monkeypatch):
    """The audio is still on disk and still correct, so the user can fix
    one more beat and release again."""
    import engine.app as app_mod
    _stub_stages(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("pexels is down")

    monkeypatch.setattr(app_mod, "clips_stage", boom)
    _seed(client)

    client.post("/api/plan/p1/voice/approve")
    _wait_for(_store(client), "p1", {VOICE_REVIEW})


# --- the board --------------------------------------------------------------


def test_the_board_carries_the_text_each_beat_is_meant_to_say(client):
    """The expected use is to paste a beat into another voice tool, so the
    Devanagari has to be on the board beside the upload control."""
    plan = _seed(client)
    data = client.get("/api/plan/p1/voice").json()

    assert data["awaiting_review"] is True
    assert data["total"] == len(plan.script.beats)
    said = [row["voice_text"] for row in data["beats"]]
    assert said == [b.voice_text for b in plan.script.beats]
    assert all(row["caption_text"] for row in data["beats"])
    assert all(row["exists"] for row in data["beats"])


def test_the_board_shows_both_duration_windows(client):
    """The gate window and QC's window are different questions, and a plan
    can pass the first while failing the second."""
    _seed(client, beats=2, seconds=4.0)
    data = client.get("/api/plan/p1/voice").json()

    assert data["gate_min"] < data["duration_min"]
    assert data["gate_max"] > data["duration_max"]
    assert data["narration_seconds"] == pytest.approx(8.0, abs=0.2)
    assert data["in_gate"] is False       # 8s is nowhere near the window
    assert data["in_window"] is False


def test_the_board_can_be_read_after_the_gate_has_been_released(client):
    _seed(client, status="produced")
    data = client.get("/api/plan/p1/voice").json()
    assert data["awaiting_review"] is False
    assert data["total"] == 2


def test_a_beats_audio_is_served_for_the_player(client):
    _seed(client)
    response = client.get("/api/audio/p1/b0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert len(response.content) > 500


def test_audio_is_404_for_a_beat_that_has_none(client):
    plan = _seed(client)
    plan.script.beats[0].audio_path = None
    _store(client).save_plan(plan, status=VOICE_REVIEW)
    assert client.get("/api/audio/p1/b0").status_code == 404


def test_audio_outside_the_work_dir_is_never_served(client, tmp_path):
    """Every path here is server-built today, but it is still keyed by URL
    input and read off disk, so it gets the same containment check as
    /media and /api/frame."""
    outside = tmp_path / "elsewhere" / "secret.mp3"
    _tone(_settings(client), outside, seconds=1.0)
    plan = _seed(client)
    plan.script.beats[0].audio_path = str(outside)
    _store(client).save_plan(plan, status=VOICE_REVIEW)

    assert client.get("/api/audio/p1/b0").status_code == 404


# --- uploading --------------------------------------------------------------


def _upload(client, beat_id, path: Path, *, plan_id="p1",
            filename="take.mp3"):
    return client.post(f"/api/plan/{plan_id}/voice/{beat_id}",
                       content=path.read_bytes(),
                       headers={"Content-Type": "audio/mpeg",
                                "X-Upload-Filename": filename})


def test_an_upload_replaces_the_beat_and_moves_the_whole_total(
        client, tmp_path):
    _seed(client, beats=2, seconds=4.0)
    before = client.get("/api/plan/p1/voice").json()
    assert before["narration_seconds"] == pytest.approx(8.0, abs=0.2)

    source = _tone(_settings(client), tmp_path / "take.wav", seconds=7.0)
    response = _upload(client, "b0", source)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"] == "upload"
    assert body["seconds"] == pytest.approx(7.0, abs=0.2)
    assert body["was_seconds"] == pytest.approx(4.0, abs=0.2)
    # The board's total was recomputed rather than left stale: replacing
    # one beat moves the length of the whole video.
    assert body["narration_seconds"] == pytest.approx(11.0, abs=0.3)

    after = client.get("/api/plan/p1/voice").json()
    assert after["beats"][0]["engine"] == "upload"
    assert after["beats"][0]["replaced"] is True
    assert after["beats"][1]["engine"] == "piper"
    assert after["engines"] == {"upload": 1, "piper": 1}


def test_an_upload_retimes_the_caption_words_with_the_beat(
        client, tmp_path):
    """Caption words are positions inside the measured span, so swapping
    the audio without redoing them times the text to a recording that no
    longer exists."""
    _seed(client, beats=2, seconds=4.0)
    source = _tone(_settings(client), tmp_path / "take.wav", seconds=7.0)
    _upload(client, "b0", source)

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.words
    assert beat.words[-1].end == pytest.approx(beat.measured_seconds,
                                               abs=0.05)


def test_an_upload_is_stored_as_the_format_the_concat_demuxer_needs(
        client, tmp_path):
    """44.1 kHz stereo in, Piper's 24 kHz mono mp3 on disk."""
    _seed(client, beats=2, seconds=4.0)
    source = tmp_path / "stereo.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    settings = _settings(client)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=330:duration=3", "-ar", "44100", "-ac", "2",
         str(source)], check=True, capture_output=True)

    assert _upload(client, "b0", source).status_code == 200

    stored = Path(_store(client).get_plan("p1").script.beats[0].audio_path)
    result = subprocess.run([settings.ffmpeg, "-hide_banner", "-i",
                             str(stored)], capture_output=True, text=True)
    line = next(ln for ln in result.stderr.splitlines() if "Stream #" in ln)
    assert "24000 Hz" in line and "mono" in line and "mp3" in line


def test_the_synthesised_original_is_left_on_disk(client, tmp_path):
    """A destroyed original cannot be compared against or put back, and
    this gate is for people still making up their minds."""
    plan = _seed(client, beats=2, seconds=4.0)
    original = Path(plan.script.beats[0].audio_path)

    source = _tone(_settings(client), tmp_path / "take.wav", seconds=5.0)
    _upload(client, "b0", source)

    assert original.is_file()
    stored = Path(_store(client).get_plan("p1").script.beats[0].audio_path)
    assert stored != original


def test_the_upload_lands_under_the_work_dir(client, tmp_path):
    _seed(client, beats=2, seconds=4.0)
    _upload(client, "b0",
            _tone(_settings(client), tmp_path / "take.wav", seconds=3.0))

    stored = Path(_store(client).get_plan("p1").script.beats[0].audio_path)
    assert Path(_settings(client).work_dir).resolve() in stored.resolve().parents


def test_uploading_is_refused_unless_the_plan_is_at_the_gate(
        client, tmp_path):
    _seed(client, status="approved")
    response = _upload(
        client, "b1",
        _tone(_settings(client), tmp_path / "take.wav", seconds=3.0))
    assert response.status_code == 409


def test_uploading_to_a_beat_that_does_not_exist_is_404(client, tmp_path):
    _seed(client)
    response = _upload(
        client, "nope",
        _tone(_settings(client), tmp_path / "take.wav", seconds=3.0))
    assert response.status_code == 404


def test_bytes_that_are_not_a_media_container_are_refused(client):
    _seed(client)
    response = client.post("/api/plan/p1/voice/b0",
                           content=b"%PDF-1.7\n" + b"x" * 4096,
                           headers={"Content-Type": "audio/mpeg"})
    assert response.status_code == 415
    assert "signature" in response.json()["detail"]


def test_a_container_with_no_audio_in_it_is_refused(client, tmp_path):
    """Sniffs perfectly as mp4 and decodes perfectly. There is just
    nothing to say."""
    settings = _settings(client)
    silent = tmp_path / "picture.mp4"
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=64x64:d=2", "-pix_fmt", "yuv420p",
         str(silent)], check=True, capture_output=True)
    _seed(client)

    response = _upload(client, "b0", silent)
    assert response.status_code == 415


def test_a_silent_upload_is_refused(client, tmp_path):
    settings = _settings(client)
    mute = tmp_path / "mute.wav"
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "3", str(mute)],
        check=True, capture_output=True)
    _seed(client)

    response = _upload(client, "b0", mute)
    assert response.status_code == 415
    assert "silent" in response.json()["detail"]


def test_a_blip_too_short_to_be_a_beat_is_refused(client, tmp_path):
    _seed(client)
    response = _upload(
        client, "b1",
        _tone(_settings(client), tmp_path / "blip.wav", seconds=0.2))
    assert response.status_code == 415
    assert "short" in response.json()["detail"]


def test_an_empty_upload_is_refused(client):
    _seed(client)
    response = client.post("/api/plan/p1/voice/b0", content=b"",
                           headers={"Content-Type": "audio/mpeg"})
    assert response.status_code == 400


def test_a_filename_shaped_like_a_path_is_refused_before_anything_is_read(
        client, tmp_path):
    """The stored name is built from the beat id, so nothing was going to
    use theirs — but a name shaped like a path is refused outright rather
    than quietly cleaned."""
    _seed(client)
    source = _tone(_settings(client), tmp_path / "take.wav", seconds=3.0)
    response = _upload(client, "b0", source,
                       filename="../../../../evil.mp3")
    assert response.status_code == 400


def test_an_upload_over_the_cap_is_stopped_at_the_cap(client, tmp_path):
    _seed(client)
    _settings(client).upload_max_mb = 0.05
    source = _tone(_settings(client), tmp_path / "long.wav", seconds=30.0)
    assert source.stat().st_size > 0.05 * 1024 * 1024

    response = _upload(client, "b0", source)
    assert response.status_code == 413


def test_a_rejected_upload_leaves_the_beat_as_it_was(client, tmp_path):
    plan = _seed(client, beats=2, seconds=4.0)
    before = Path(plan.script.beats[0].audio_path)

    assert _upload(
        client, "b1",
        _tone(_settings(client), tmp_path / "blip.wav",
              seconds=0.2)).status_code == 415

    beat = _store(client).get_plan("p1").script.beats[0]
    assert Path(beat.audio_path) == before
    assert beat.voice_engine == "piper"
    assert beat.measured_seconds == pytest.approx(4.0, abs=0.2)


# --- it actually reaches the video ------------------------------------------


def _goertzel(samples: list[float], hz: float, rate: int) -> float:
    """How much of ``hz`` is in ``samples``. One bin of a DFT, no numpy."""
    k = 2.0 * math.cos(2.0 * math.pi * hz / rate)
    s1 = s2 = 0.0
    for sample in samples:
        s0 = sample + k * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0.0))


def _pcm_at(ffmpeg, path: Path, start: float, seconds: float,
            rate: int = 8000) -> list[float]:
    """A slice of the video's audio, as signed 16-bit mono samples."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-ss", f"{start:.3f}",
         "-i", str(path), "-t", f"{seconds:.3f}", "-map", "0:a:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(rate),
         "-ac", "1", "-"], check=True, capture_output=True)
    raw = result.stdout
    return [int.from_bytes(raw[i:i + 2], "little", signed=True)
            for i in range(0, len(raw) - 1, 2)]


def _dominant(samples, rate, candidates) -> float:
    return max(candidates, key=lambda hz: _goertzel(samples, hz, rate))


def test_uploaded_narration_is_the_audio_that_comes_out_of_the_render(
        tmp_path):
    """The end-to-end claim, measured rather than asserted.

    Two beats of 220 Hz narration with an 880 Hz take uploaded over the
    second one. If the upload reached the render, the back half of the
    finished video's audio is 880 Hz and the front half is still 220 Hz —
    and the video is as long as the *new* narration, not the old.
    """
    settings = Settings()
    if not Path(settings.ffmpeg).exists():   # pragma: no cover - env guard
        pytest.skip("ffmpeg is not available in this environment")
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    settings.width, settings.height, settings.fps = 240, 426, 24
    settings.transition_duration = 0.4
    settings.video_grade = False
    settings.video_grain = 0
    settings.music = False
    settings.sfx = False
    settings.stickers = False
    client = TestClient(create_app(settings=settings))

    spoken = 3.0
    plan = make_plan(plan_id="p1", beats=2, measured=spoken)
    work = Path(settings.work_dir) / "p1"
    for beat in plan.script.beats:
        beat.role = "setup"
        beat.transition = "fade"
        path = _tone(settings, work / "audio" / f"{beat.beat_id}.mp3",
                     seconds=spoken, hz=220)
        beat.audio_path = str(path)
        beat.voice_engine = "piper"
        beat.measured_seconds = spoken
    client.app.state.store.save_plan(plan, status=VOICE_REVIEW)

    # The upload: a different note, and longer than what it replaces.
    replacement = 5.0
    source = _tone(settings, tmp_path / "mine.wav", seconds=replacement,
                   hz=880)
    assert _upload(client, "b1", source).status_code == 200

    # Clips are built *after* the upload, from the new measured spans —
    # which is the order the real pipeline runs them in, and the reason
    # the gate is before CLIPS rather than after it.
    plan = client.app.state.store.get_plan("p1")
    (work / "clips").mkdir(parents=True, exist_ok=True)
    for beat in plan.script.beats:
        clip = work / "clips" / f"{beat.beat_id}.mp4"
        subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi",
             "-i", f"color=c=0x303030:s=64x114:d={beat.seconds() + 0.5}",
             "-pix_fmt", "yuv420p", str(clip)],
            check=True, capture_output=True)
        beat.clips = [Clip(path=str(clip), query="grey plate",
                           provider="pexels", duration=beat.seconds())]

    from engine.pipeline import render_stage
    result = render_stage(plan, client.app.state.store, settings)
    video = Path(result["video"])
    assert video.is_file()

    total = spoken + replacement
    assert result["probe"]["duration"] == pytest.approx(total, abs=0.4), \
        "the render is not as long as the narration after the swap"

    rate = 8000
    notes = [220.0, 880.0]
    front = _pcm_at(settings.ffmpeg, video, 1.0, 1.0, rate)
    back = _pcm_at(settings.ffmpeg, video, spoken + 2.0, 1.0, rate)

    assert _dominant(front, rate, notes) == 220.0, \
        "the first beat's synthesised narration did not survive"
    assert _dominant(back, rate, notes) == 880.0, \
        "the uploaded narration is not in the finished video"


# --- the panel --------------------------------------------------------------


def test_the_panel_only_reaches_for_ids_that_exist(client):
    """A typo'd id is the cheapest way to break this board, and it breaks
    silently: ``$("vioce-note")`` is null, the handler throws, and the
    button simply does nothing. Only the statically-written ids are
    checked — the per-beat ones are built at runtime from the beat id.
    """
    import re as _re

    page = (Path(__file__).resolve().parents[1] / "engine" / "ui"
            / "index.html").read_text(encoding="utf-8")
    defined = set(_re.findall(r'id="([A-Za-z0-9_-]+)"', page))
    used = set(_re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', page))

    assert not used - defined, f"panel reaches for missing ids: "\
                               f"{sorted(used - defined)}"
    # And the ids this feature added are among them, so the assertion above
    # cannot pass by the section having quietly failed to land.
    assert {"s-voice", "beatlist", "voice-length", "review-voice",
            "voice-then-clips", "btn-voice-release"} <= defined


def test_the_panel_calls_routes_that_are_registered(client):
    """Every voice URL the panel builds resolves to a real route."""
    page = (Path(__file__).resolve().parents[1] / "engine" / "ui"
            / "index.html").read_text(encoding="utf-8")
    paths = {getattr(r, "path", "") for r in client.app.routes}

    for wanted in ["/api/plan/${PLAN.plan_id}/voice`",
                   "/api/plan/${PLAN.plan_id}/voice/approve`",
                   "/api/plan/${PLAN.plan_id}/voice/${beat}`"]:
        assert wanted in page, f"the panel never calls {wanted}"

    assert "/api/plan/{plan_id}/voice" in paths
    assert "/api/plan/{plan_id}/voice/approve" in paths
    assert "/api/audio/{plan_id}/{beat_id}" in paths

    # One path, two verbs: POST is the upload, PATCH is the correction.
    verbs = set()
    for route in client.app.routes:
        if getattr(route, "path", "") == "/api/plan/{plan_id}/voice/{beat_id}":
            verbs |= set(getattr(route, "methods", []))
    assert {"POST", "PATCH"} <= verbs, verbs
    assert 'method: "PATCH"' in page, "the panel never sends the correction"


def test_narration_too_long_sends_you_back_to_the_gate_not_to_a_dead_end(
        client):
    """The failure this gate makes likeliest, walked with the real stages.

    Uploading longer takes is the easiest way to push a script out of its
    duration window, and LENGTH is the first thing after the gate. Nothing
    is stubbed here and nothing reaches the network: LENGTH refuses before
    CLIPS runs, so the whole path is local.
    """
    _seed(client, beats=2, seconds=4.0)      # 8s, nowhere near the window

    assert client.post("/api/plan/p1/voice/approve").status_code == 200
    # Back at the gate rather than stranded in "rejected_length": the audio
    # is still on disk, so the user can shorten a beat and release again.
    _wait_for(_store(client), "p1", {VOICE_REVIEW})

    board = client.get("/api/plan/p1/voice").json()
    assert board["awaiting_review"] is True
    assert board["in_gate"] is False


# --- correcting a word and saying it again ----------------------------------
#
# Why this exists: Piper mispronounces a word, and the fix is one character
# in that beat's Devanagari. Before this, the only way to change it was to
# go back to the script screen and re-produce, which re-speaks every beat
# and throws away whatever else had been settled at this gate.


def _fake_engine(monkeypatch, settings, *, seconds_per_word=0.5):
    """Stand in for Piper with an ffmpeg tone whose length tracks the text.

    Honours its argument, which is the whole point: a fixed-length stub
    could not tell "the beat was re-spoken with the new words" from "the
    beat was never re-spoken at all". The real engine is exercised by
    ``test_the_real_engine_speaks_a_corrected_beat`` below.
    """
    import engine.media.voice as voice_mod

    said: list[tuple[str, str]] = []

    def fake(text, target, _settings):
        said.append((str(target), text))
        words = max(len(text.split()), 1)
        _tone(settings, Path(target), seconds=words * seconds_per_word)
        return 0

    monkeypatch.setattr(voice_mod, "synth_beat_piper", fake)
    return said


def test_correcting_a_word_re_speaks_only_that_beat(client, monkeypatch):
    said = _fake_engine(monkeypatch, _settings(client))
    _seed(client, beats=2, seconds=4.0)

    response = client.patch("/api/plan/p1/voice/b0",
                            json={"voice_text": "मेरी फीस माफ़ करदो"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["respoken"] is True
    assert body["engine"] == "piper"
    assert len(said) == 1, "more than one beat was re-spoken"
    assert "फीस" in said[0][1]

    plan = _store(client).get_plan("p1")
    assert plan.script.beats[0].voice_text == "मेरी फीस माफ़ करदो"
    # The other beat was not touched: same audio, same measurement.
    assert plan.script.beats[1].measured_seconds == pytest.approx(4.0,
                                                                 abs=0.2)


def test_the_corrected_beat_is_re_measured_and_re_timed(client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client), seconds_per_word=1.0)
    _seed(client, beats=2, seconds=4.0)

    body = client.patch("/api/plan/p1/voice/b0",
                        json={"voice_text": "एक दो तीन चार पाँच छह"}).json()

    assert body["seconds"] == pytest.approx(6.0, abs=0.3)
    assert body["was_seconds"] == pytest.approx(4.0, abs=0.2)
    assert body["narration_seconds"] == pytest.approx(10.0, abs=0.4)

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.words
    assert beat.words[-1].end == pytest.approx(beat.measured_seconds,
                                               abs=0.05)


def test_the_audio_is_overwritten_rather_than_piling_up(client, monkeypatch):
    """A corrected beat replaces its take. A second file nothing reads is
    how a work dir turns into a graveyard."""
    _fake_engine(monkeypatch, _settings(client))
    plan = _seed(client, beats=2, seconds=4.0)
    before = Path(plan.script.beats[0].audio_path)

    client.patch("/api/plan/p1/voice/b0", json={"voice_text": "नया पाठ"})

    after = Path(_store(client).get_plan("p1").script.beats[0].audio_path)
    assert after == before


def test_fixing_only_the_pronunciation_leaves_the_burned_caption_alone(
        client, monkeypatch):
    """The point of the two fields being separate: a phonetic respelling
    for the voice, the real spelling for the eye."""
    _fake_engine(monkeypatch, _settings(client))
    plan = _seed(client, beats=2, seconds=4.0)
    burned = plan.script.beats[0].caption_text

    client.patch("/api/plan/p1/voice/b0", json={"voice_text": "फीस माफ़"})

    assert _store(client).get_plan("p1").script.beats[0].caption_text \
        == burned


def test_changing_only_the_caption_does_not_re_speak_anything(
        client, monkeypatch):
    """The audio did not change, so there is nothing to say again — only
    the word timings to redo against it."""
    said = _fake_engine(monkeypatch, _settings(client))
    _seed(client, beats=2, seconds=4.0)

    body = client.patch("/api/plan/p1/voice/b0",
                        json={"caption_text": "meri fees maaf kardo"}).json()

    assert body["respoken"] is False
    assert said == [], "the engine ran for a caption-only edit"
    assert body["seconds"] == pytest.approx(4.0, abs=0.2)

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.caption_text == "meri fees maaf kardo"
    assert beat.voice_engine == "piper"
    assert [w.word for w in beat.words] == ["meri", "fees", "maaf", "kardo"]


def test_latin_script_in_the_voice_line_is_refused_with_the_words(
        client, monkeypatch):
    """The same rule the script gate enforces, through the same function:
    Piper mispronounces Latin script whoever typed it."""
    said = _fake_engine(monkeypatch, _settings(client))
    _seed(client, beats=2, seconds=4.0)

    response = client.patch("/api/plan/p1/voice/b0",
                            json={"voice_text": "meri fees maaf kardo"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "meri" in detail and "fees" in detail
    assert said == [], "a refused edit still ran the engine"


def test_the_refused_edit_leaves_the_beat_exactly_as_it_was(
        client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client))
    plan = _seed(client, beats=2, seconds=4.0)
    before = plan.script.beats[0].voice_text

    client.patch("/api/plan/p1/voice/b0", json={"voice_text": "hello ji"})

    assert _store(client).get_plan("p1").script.beats[0].voice_text == before


def test_the_devanagari_rule_is_shared_not_copied():
    """A second copy of "what counts as a violation" drifting from the
    first is the failure this identity check exists to prevent."""
    from engine.agents import latin_words as agents_rule
    from engine.app import latin_words as app_rule

    assert app_rule is agents_rule


def test_an_empty_line_is_refused(client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client))
    _seed(client, beats=2, seconds=4.0)
    assert client.patch("/api/plan/p1/voice/b0",
                        json={"voice_text": "   "}).status_code == 400


def test_an_edit_that_changes_nothing_is_refused(client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client))
    _seed(client, beats=2, seconds=4.0)
    assert client.patch("/api/plan/p1/voice/b0", json={}).status_code == 400


def test_correcting_is_refused_unless_the_plan_is_at_the_gate(
        client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client))
    _seed(client, status="approved")
    assert client.patch("/api/plan/p1/voice/b0",
                        json={"voice_text": "नया"}).status_code == 409


def test_correcting_a_beat_that_does_not_exist_is_404(client, monkeypatch):
    _fake_engine(monkeypatch, _settings(client))
    _seed(client)
    assert client.patch("/api/plan/p1/voice/nope",
                        json={"voice_text": "नया"}).status_code == 404


def test_re_speaking_replaces_narration_that_was_uploaded(
        client, monkeypatch, tmp_path):
    """Correcting the words means Piper says them, so an upload on that
    beat is superseded. The engine flips back, visibly."""
    settings = _settings(client)
    _seed(client, beats=2, seconds=4.0)
    assert _upload(client, "b0",
                   _tone(settings, tmp_path / "mine.wav",
                         seconds=6.0)).status_code == 200
    assert _store(client).get_plan("p1").script.beats[0].voice_engine \
        == "upload"

    _fake_engine(monkeypatch, settings)
    body = client.patch("/api/plan/p1/voice/b0",
                        json={"voice_text": "फीस माफ़"}).json()

    assert body["engine"] == "piper"
    assert body["respoken"] is True
    assert _store(client).get_plan("p1").script.beats[0].voice_engine \
        == "piper"


def test_the_real_engine_speaks_a_corrected_beat(client):
    """One test with nothing stubbed, so the wiring is proven for real and
    not only against a stand-in."""
    from engine.media.piper_voice import PiperUnavailable

    _seed(client, beats=2, seconds=4.0)
    try:
        response = client.patch(
            "/api/plan/p1/voice/b0",
            json={"voice_text": "समुद्र के नीचे एक पुराना शहर मिला है"})
    except PiperUnavailable:            # pragma: no cover - env guard
        pytest.skip("no voice engine is installed in this environment")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["respoken"] is True
    assert body["seconds"] > 0.5

    beat = _store(client).get_plan("p1").script.beats[0]
    assert Path(beat.audio_path).is_file()
    assert beat.measured_seconds == pytest.approx(body["seconds"], abs=0.01)


def test_a_failed_re_speak_does_not_destroy_the_take_it_replaces(
        client, monkeypatch):
    """The loop this feature is for is: try a spelling, listen, try
    another. So a failed attempt has to leave the beat exactly as
    playable as it was.

    Piper writes its mp3 straight onto the target with ``-y``. That was
    harmless while synthesis only ever ran on a beat with nothing there
    yet; re-speaking runs it on a beat that already has a take, and a
    half-written file over a good one is a beat that can no longer be
    played, re-spoken from, or rendered.
    """
    import engine.media.voice as voice_mod

    plan = _seed(client, beats=2, seconds=4.0)
    audio = Path(plan.script.beats[0].audio_path)
    before = audio.read_bytes()
    assert before

    def writes_then_dies(text, target, _settings):
        Path(target).write_bytes(b"\x00" * 64)   # a truncated take
        raise OSError("the engine died halfway through")

    monkeypatch.setattr(voice_mod, "synth_beat_piper", writes_then_dies)
    monkeypatch.setattr(voice_mod, "synth_beat_edge", writes_then_dies)

    response = client.patch("/api/plan/p1/voice/b0",
                            json={"voice_text": "नया पाठ"})
    assert response.status_code == 503, response.text

    assert audio.read_bytes() == before, \
        "a failed re-speak overwrote the take it was replacing"
    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.measured_seconds == pytest.approx(4.0, abs=0.2)
    assert beat.voice_text != "नया पाठ", \
        "the text was persisted although nothing said it"


def test_a_failed_re_speak_leaves_no_scratch_file_behind(
        client, monkeypatch):
    import engine.media.voice as voice_mod

    plan = _seed(client, beats=2, seconds=4.0)
    audio_dir = Path(plan.script.beats[0].audio_path).parent
    before = {p.name for p in audio_dir.iterdir()}

    def dies(text, target, _settings):
        Path(target).write_bytes(b"\x00" * 64)
        raise OSError("nope")

    monkeypatch.setattr(voice_mod, "synth_beat_piper", dies)
    monkeypatch.setattr(voice_mod, "synth_beat_edge", dies)
    client.patch("/api/plan/p1/voice/b0", json={"voice_text": "नया"})
    assert {p.name for p in audio_dir.iterdir()} == before


# --- the cleanup toggle and the raw revert (Task 3) --------------------------
#
# Global Constraint 3 keeps the raw upload on disk beside whatever is
# written; this is the route that reads it back. Every claim here is
# measured off a real file, per Global Constraint 7 -- a beat with an
# internal 2s gap is uploaded once, and the two cleanup states are told
# apart by how long the *written* file actually is, not by inspecting a
# filter string.


def _gap_tone(settings, path: Path) -> Path:
    """tone / 2s silence / tone. Cleanup's ``silenceremove`` caps the gap
    at ``voice_pause_cap`` (0.35s by default), so the written file measures
    shorter with cleanup on than with it off -- the one difference these
    tests can tell apart without decoding samples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
         "-map", "[out]", "-ar", "44100", "-ac", "1", str(path)],
        check=True, capture_output=True)
    return path


def test_uploading_stores_a_raw_copy_the_board_can_point_at(
        client, tmp_path):
    """The upload response and the board both carry the new fields, and the
    raw file on disk is the exact bytes handed over -- not the transcoded
    one this same request also writes."""
    _seed(client, beats=2, seconds=4.0)
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")

    body = _upload(client, "b0", source).json()
    assert body["cleaned"] is True
    assert body["filters_applied"]
    assert body["cleanup_abandoned"] is False
    assert body["raw_audio"] == "/api/audio/p1/b0?raw=1"

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.raw_audio_path
    assert Path(beat.raw_audio_path).read_bytes() == source.read_bytes()
    assert beat.cleanup is not None and beat.cleanup.cleanup_abandoned \
        is False

    board = client.get("/api/plan/p1/voice").json()
    row = board["beats"][0]
    assert row["raw_audio"] == "/api/audio/p1/b0?raw=1"
    assert row["cleaned"] is True
    assert row["cleanup"]["filters_applied"]
    # The beat nothing was ever uploaded for reports no raw, rather than
    # erroring or inventing one.
    assert board["beats"][1]["raw_audio"] is None
    assert board["beats"][1]["cleaned"] is None
    assert board["beats"][1]["cleanup"] is None


def test_reverting_to_raw_measures_like_the_raw_and_is_not_cleaned(
        client, tmp_path):
    """Upload with cleanup on, then ask for cleanup off: the written file
    comes back out at the raw's own length (the gap survives) and the
    board says so."""
    _seed(client, beats=2, seconds=4.0)
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")

    cleaned_body = _upload(client, "b0", source).json()
    # Cleanup trimmed the 2s gap down to the pause cap: shorter than the
    # raw's own ~4s.
    assert cleaned_body["seconds"] < 3.0

    response = client.post("/api/plan/p1/voice/b0/cleanup",
                           json={"enabled": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["cleaned"] is False
    assert body["seconds"] == pytest.approx(4.0, abs=0.3), \
        "reverting to raw should measure like the raw, gap and all"

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.measured_seconds == pytest.approx(4.0, abs=0.3)
    assert beat.cleanup.filters_applied == ""


def test_reverting_to_raw_and_back_to_cleaned_proves_the_raw_survived(
        client, tmp_path):
    """The round trip: cleaned -> raw -> cleaned again lands on the same
    measurement it started at, which only holds if the raw uploaded bytes
    were never touched by the first cleaned write."""
    _seed(client, beats=2, seconds=4.0)
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")

    first = _upload(client, "b0", source).json()["seconds"]

    off = client.post("/api/plan/p1/voice/b0/cleanup",
                      json={"enabled": False}).json()
    assert off["seconds"] > first + 1.0, "cleanup off did not restore the gap"

    back_on = client.post("/api/plan/p1/voice/b0/cleanup",
                          json={"enabled": True}).json()
    assert back_on["cleaned"] is True
    assert back_on["seconds"] == pytest.approx(first, abs=0.2)

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.measured_seconds == pytest.approx(first, abs=0.2)


def test_calling_the_toggle_twice_with_the_same_value_is_not_an_error(
        client, tmp_path):
    """Idempotent, per the brief's own resolution: same value, both calls
    200, same state -- not that the second call is detected and skipped."""
    _seed(client, beats=2, seconds=4.0)
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")
    _upload(client, "b0", source)

    first = client.post("/api/plan/p1/voice/b0/cleanup",
                        json={"enabled": True})
    second = client.post("/api/plan/p1/voice/b0/cleanup",
                         json={"enabled": True})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["seconds"] == pytest.approx(second.json()["seconds"],
                                                     abs=0.1)


def test_raw_equals_one_serves_the_raw_bytes(client, tmp_path):
    _seed(client, beats=2, seconds=4.0)
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")
    _upload(client, "b0", source)

    response = client.get("/api/audio/p1/b0", params={"raw": 1})
    assert response.status_code == 200
    assert response.content == source.read_bytes()


def test_raw_equals_one_is_404_for_a_beat_with_no_raw_stored(client):
    _seed(client, beats=2, seconds=4.0)
    response = client.get("/api/audio/p1/b1", params={"raw": 1})
    assert response.status_code == 404


def test_the_cleanup_route_is_409_off_the_gate(client, tmp_path):
    settings = _settings(client)
    settings.voice_clean = True
    source = _gap_tone(settings, tmp_path / "gapped.wav")
    _seed(client, beats=2, seconds=4.0)
    _upload(client, "b0", source)

    _store(client).set_status("p1", "approved")
    response = client.post("/api/plan/p1/voice/b0/cleanup",
                           json={"enabled": False})
    assert response.status_code == 409


def test_the_cleanup_route_is_404_for_an_unknown_beat(client):
    _seed(client, beats=2, seconds=4.0)
    response = client.post("/api/plan/p1/voice/nope/cleanup",
                           json={"enabled": False})
    assert response.status_code == 404


def test_the_cleanup_route_is_404_for_a_beat_with_no_raw_stored(client):
    """A beat nothing was uploaded for has nothing to revert to."""
    _seed(client, beats=2, seconds=4.0)
    response = client.post("/api/plan/p1/voice/b1/cleanup",
                           json={"enabled": False})
    assert response.status_code == 404


def test_a_raw_path_outside_the_work_dir_is_never_served(client, tmp_path):
    outside = tmp_path / "elsewhere" / "secret-raw.wav"
    _tone(_settings(client), outside, seconds=1.0)
    plan = _seed(client, beats=2, seconds=4.0)
    plan.script.beats[0].raw_audio_path = str(outside)
    _store(client).save_plan(plan, status=VOICE_REVIEW)

    assert client.get("/api/audio/p1/b0",
                      params={"raw": 1}).status_code == 404


# --- a respeak must not be discardable by the toggle (fix round 1) ---------
#
# Upload -> respeak -> toggle used to re-ingest the *old* raw and call
# apply_beat_audio(engine=UPLOAD_ENGINE), silently overwriting whatever the
# respeak had just corrected. raw_audio_path/cleanup are correctly carried
# forward unchanged by the PATCH route (the raw file really is still
# there, Global Constraint 3), but that is exactly what made the toggle
# dangerous: it had no way to tell "this beat's raw is still active" from
# "this beat's raw is stale history" except ``voice_engine``, and it never
# looked.


def test_a_respoken_beat_refuses_the_cleanup_toggle_and_keeps_the_correction(
        client, monkeypatch, tmp_path):
    settings = _settings(client)
    settings.voice_clean = True
    _seed(client, beats=2, seconds=4.0)
    assert _upload(client, "b0",
                   _gap_tone(settings, tmp_path / "gapped.wav")
                   ).status_code == 200
    assert _store(client).get_plan("p1").script.beats[0].voice_engine \
        == "upload"

    _fake_engine(monkeypatch, settings)
    respeak = client.patch("/api/plan/p1/voice/b0",
                           json={"voice_text": "फीस माफ़"})
    assert respeak.status_code == 200, respeak.text
    respoken = _store(client).get_plan("p1").script.beats[0]
    assert respoken.voice_engine == "piper"
    respoken_path = Path(respoken.audio_path)
    respoken_seconds = respoken.measured_seconds
    # The raw upload is still on file -- respeak must not have touched it.
    assert respoken.raw_audio_path

    response = client.post("/api/plan/p1/voice/b0/cleanup",
                           json={"enabled": False})
    assert response.status_code == 409, response.text

    beat = _store(client).get_plan("p1").script.beats[0]
    assert beat.voice_engine == "piper", \
        "the toggle silently reverted the beat to its old upload"
    assert Path(beat.audio_path) == respoken_path
    assert beat.measured_seconds == pytest.approx(respoken_seconds, abs=0.01)


def test_the_board_does_not_advertise_a_revert_control_after_a_respeak(
        client, monkeypatch, tmp_path):
    """``replaced`` already says the upload is no longer active; the
    revert-related fields have to agree with it rather than describing a
    take that is no longer on the timeline."""
    settings = _settings(client)
    settings.voice_clean = True
    _seed(client, beats=2, seconds=4.0)
    _upload(client, "b0", _gap_tone(settings, tmp_path / "gapped.wav"))

    _fake_engine(monkeypatch, settings)
    client.patch("/api/plan/p1/voice/b0", json={"voice_text": "नया पाठ"})

    row = client.get("/api/plan/p1/voice").json()["beats"][0]
    assert row["replaced"] is False
    assert row["raw_audio"] is None
    assert row["cleaned"] is None
    assert row["cleanup"] is None
    assert row["cleanup_toggle"] is None
    # The file itself was never touched -- only the board stopped
    # advertising it, and the route (tested above) stopped acting on it.
    assert Path(_store(client).get_plan("p1").script.beats[0]
               .raw_audio_path).is_file()

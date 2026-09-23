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
    assert "/api/plan/{plan_id}/voice/{beat_id}" in paths
    assert "/api/audio/{plan_id}/{beat_id}" in paths


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

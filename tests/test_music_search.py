"""Searching Openverse for a music bed, from the review board.

``assets/music/`` holds one file and every reel gets it. That is fine
for a placeholder and wrong for a library: a bed is a mood choice, and
the mood is per reel.

Openverse is the source because of one property no other free library
has -- it returns the **licence with every result**. That makes two
things possible that would otherwise be manual:

**NC never reaches the panel.** ``license_type=commercial`` is sent on
every search, so a track nobody may monetise is never offered. Measured
against the live API, "mystery tension" returns 159 results unfiltered
and 138 filtered, so the 21 it drops are real.

**Credits write themselves.** Openverse hands back ready-made
attribution text. CC-BY is allowed here precisely because that text can
flow into the publish payload automatically -- CC0 alone was measured at
10 results for "dark ambient drone" against 240, which is a library that
repeats itself within a week.

A credit is emitted only where the licence demands one. CC0 comes with
attribution text too, and putting it in the caption would be adding an
obligation the licence does not create.

No test here touches the network; each drives an ``httpx.MockTransport``
holding a real Openverse response shape.

The property most worth keeping is the last section. A pick carries an
Openverse **id**, never a URL, so the only links this server fetches are
ones Openverse gave it.
"""

from __future__ import annotations

import httpx
import pytest

from engine.config import Settings
from engine.media.music_search import (SEARCH_URL, Candidate,
                                       SearchUnavailable, credit_line,
                                       fetch_for_plan, search)


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    return s


def _track(tid, *, licence="cc0", version="1.0", title="Dark drone",
           creator="someone", duration=92000):
    """One result, shaped as api.openverse.org/v1/audio/ really returns."""
    slug = {"cc0": "publicdomain/zero/1.0",
            "by": "licenses/by/4.0"}[licence]
    return {
        "id": tid,
        "title": title,
        "creator": creator,
        "license": licence,
        "license_version": version,
        "license_url": f"https://creativecommons.org/{slug}/",
        "attribution": (f'"{title}" by {creator} is marked with '
                        f"{licence.upper()} {version}."),
        "duration": duration,
        "url": f"https://cdn.freesound.org/previews/{tid}.mp3",
        "foreign_landing_url": f"https://freesound.org/s/{tid}",
        "provider": "freesound",
    }


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _results(*tracks):
    def handler(request):
        return httpx.Response(200, json={"result_count": len(tracks),
                                         "results": list(tracks)})
    return _client(handler)


# --- searching --------------------------------------------------------------


def test_a_search_returns_what_the_card_needs(settings):
    client = _results(_track("a1", title="Creepy vinyl"))

    found = search("dark drone", settings, client=client)

    assert len(found) == 1
    assert found[0].title == "Creepy vinyl"
    assert found[0].duration == 92000
    assert found[0].preview.endswith(".mp3")


def test_only_commercially_licensed_tracks_are_ever_asked_for(settings):
    """The filter is sent on the request, not applied to the response:
    a track nobody may monetise should never reach the panel at all."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"results": [_track("a1")]})

    search("dark drone", settings, client=_client(handler))

    assert seen["license_type"] == "commercial"


def test_the_search_goes_to_openverse(settings):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url).split("?")[0]
        return httpx.Response(200, json={"results": []})

    search("x", settings, client=_client(handler))

    assert seen["url"] == SEARCH_URL


def test_an_empty_query_is_refused_before_any_request(settings):
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("no request should be made")

    with pytest.raises(SearchUnavailable):
        search("   ", settings, client=_client(handler))


def test_openverse_being_down_is_reported_not_raised_raw(settings):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(SearchUnavailable) as caught:
        search("dark drone", settings, client=_client(handler))

    assert "openverse" in str(caught.value).lower()


def test_a_non_200_says_what_came_back(settings):
    def handler(request):
        return httpx.Response(503, text="down for maintenance")

    with pytest.raises(SearchUnavailable) as caught:
        search("dark drone", settings, client=_client(handler))

    assert "503" in str(caught.value)


def test_a_track_with_no_playable_url_is_dropped(settings):
    """Openverse indexes records whose audio has gone; a card that
    cannot play is worse than one fewer card."""
    broken = _track("a1")
    broken["url"] = ""
    client = _results(broken, _track("a2"))

    found = search("dark drone", settings, client=client)

    assert [c.openverse_id for c in found] == ["a2"]


def test_the_limit_is_honoured(settings):
    client = _results(*[_track(f"a{i}") for i in range(30)])

    assert len(search("dark drone", settings, limit=5, client=client)) == 5


# --- the credit -------------------------------------------------------------


def test_cc_by_carries_the_credit_openverse_wrote():
    track = Candidate.from_result(_track("a1", licence="by", version="4.0"))

    assert track.needs_credit is True
    assert credit_line(track) == track.attribution


def test_cc0_asks_for_no_credit():
    """CC0 ships attribution text too. Putting it in the caption would
    invent an obligation the licence does not create."""
    track = Candidate.from_result(_track("a1", licence="cc0"))

    assert track.needs_credit is False
    assert credit_line(track) == ""


def test_the_card_can_say_which_tracks_cost_a_credit():
    by = Candidate.from_result(_track("a1", licence="by"))
    cc0 = Candidate.from_result(_track("a2", licence="cc0"))

    assert by.to_dict()["needs_credit"] is True
    assert cc0.to_dict()["needs_credit"] is False


# --- taking one -------------------------------------------------------------


def test_choosing_a_track_downloads_it_under_the_plan(settings, audio_bytes):
    def handler(request):
        if request.url.path.endswith("/a1/"):
            return httpx.Response(200, json=_track("a1"))
        return httpx.Response(200, content=audio_bytes)

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=_client(handler))

    from pathlib import Path
    assert Path(choice.path).is_file()
    assert Path(choice.path).stat().st_size > 1000
    assert "p1" in choice.path


def test_the_downloaded_bed_does_not_land_in_the_shared_assets_folder(
        settings, audio_bytes):
    """A per-reel choice that wrote to assets/music/ would change the bed
    of every other reel, including ones already reviewed."""
    def handler(request):
        if request.url.path.endswith("/a1/"):
            return httpx.Response(200, json=_track("a1"))
        return httpx.Response(200, content=audio_bytes)

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=_client(handler))

    assert "assets" not in choice.path.replace("\\", "/").lower()


def test_the_choice_remembers_the_credit_it_owes(settings, audio_bytes):
    def handler(request):
        if request.url.path.endswith("/a1/"):
            return httpx.Response(200, json=_track("a1", licence="by"))
        return httpx.Response(200, content=audio_bytes)

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=_client(handler))

    assert choice.attribution
    assert choice.licence == "by"


def test_a_dead_download_leaves_no_half_written_file(settings):
    def handler(request):
        if request.url.path.endswith("/a1/"):
            return httpx.Response(200, json=_track("a1"))
        return httpx.Response(404, text="gone")

    with pytest.raises(SearchUnavailable):
        fetch_for_plan("p1", "a1", settings, settings.work_dir,
                       client=_client(handler))

    from pathlib import Path
    music = Path(settings.work_dir) / "p1" / "music"
    assert not any(music.glob("*")) if music.is_dir() else True


# --- the SSRF guard ---------------------------------------------------------


def test_a_pick_carries_an_id_and_the_url_is_read_back_from_openverse(
        settings, audio_bytes):
    """The guard this module exists behind. A route that downloaded the
    link the browser sent would download any link anyone sent, so the
    browser sends an id and the download URL comes from Openverse.
    """
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        if request.url.path.endswith("/a1/"):
            return httpx.Response(200, json=_track("a1"))
        return httpx.Response(200, content=audio_bytes)

    fetch_for_plan("p1", "a1", settings, settings.work_dir,
                   client=_client(handler))

    assert any("api.openverse.org" in u for u in fetched)
    # The only non-Openverse address touched is the one Openverse named.
    # A set, because the size is asked for before the bytes are, so that
    # address is legitimately visited more than once.
    others = {u for u in fetched if "api.openverse.org" not in u}
    assert others == {"https://cdn.freesound.org/previews/a1.mp3"}


# --- not downloading thirteen minutes for a forty-second reel ---------------
#
# Measured against the live CDN before any of this was written:
#
#      16s   0.4 MB     5.9s
#      59s   1.3 MB    19.3s
#     152s   3.5 MB    46.2s
#     457s  10.2 MB   134.4s
#     803s  18.2 MB   238.8s
#
# A reel is 36-66 seconds and the render loops the bed to fill it
# (``aloop=loop=-1`` in engine/assembly/render.py), so the 803-second
# track is eighteen times more audio than can ever be heard -- and four
# minutes of waiting at the gate to get it. The fetch asks for a prefix
# instead, and trims it to a clean file.


def _mp3(path, seconds=8):
    """A real mp3, because the trim runs ffmpeg over what was fetched."""
    import subprocess

    from engine.config import Settings
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [Settings().ffmpeg, "-hide_banner", "-v", "error", "-y", "-f",
         "lavfi", "-i", f"sine=frequency=110:duration={seconds}",
         "-c:a", "libmp3lame", "-q:a", "2", str(path)],
        check=True, capture_output=True)
    return path.read_bytes()


@pytest.fixture()
def audio_bytes(tmp_path):
    return _mp3(tmp_path / "src.mp3")


def _serving(audio, detail):
    """A transport that answers the detail lookup and serves ``audio``,
    honouring Range the way the real CDN does (measured: 206 with a
    Content-Range header)."""
    asked = {}

    def handler(request):
        if "api.openverse.org" in str(request.url):
            return httpx.Response(200, json=detail)
        rng = request.headers.get("Range", "")
        asked["range"] = rng
        if rng.startswith("bytes=0-"):
            end = int(rng.split("-")[1])
            body = audio[:end + 1]
            return httpx.Response(
                206, content=body,
                headers={"Content-Range":
                         f"bytes 0-{len(body) - 1}/{len(audio)}"})
        return httpx.Response(200, content=audio)

    return _client(handler), asked


def test_a_long_track_is_not_downloaded_whole(settings, audio_bytes):
    """The defect this section exists for."""
    client, asked = _serving(audio_bytes,
                             _track("a1", duration=803000))

    fetch_for_plan("p1", "a1", settings, settings.work_dir, client=client)

    assert asked["range"].startswith("bytes=0-")
    wanted = int(asked["range"].split("-")[1]) + 1
    assert wanted < len(audio_bytes), "asked for the whole file anyway"


def test_a_short_track_is_taken_whole(settings, audio_bytes):
    """A 16-second bed has nothing to trim, and asking for a prefix
    longer than the file would be a range the server may reject."""
    client, asked = _serving(audio_bytes, _track("a1", duration=16000))

    fetch_for_plan("p1", "a1", settings, settings.work_dir, client=client)

    assert asked.get("range", "") == ""


def test_what_lands_on_disk_is_a_file_ffmpeg_can_read(settings,
                                                      audio_bytes):
    """A prefix of an mp3 ends mid-frame. The bed is composited, so a
    torn last frame would be audible -- it is re-encoded, not copied."""
    import subprocess

    client, _ = _serving(audio_bytes, _track("a1", duration=803000))

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=client)

    probe = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-i", choice.path,
         "-f", "null", "-"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr[-400:]


def test_the_bed_is_never_longer_than_the_cap(settings, tmp_path):
    from engine.media.music_search import MAX_BED_SECONDS

    long_audio = _mp3(tmp_path / "long.mp3", seconds=MAX_BED_SECONDS + 40)
    client, _ = _serving(long_audio, _track("a1", duration=803000))

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=client)

    from engine.media.voice import probe_duration
    assert probe_duration(choice.path, settings.ffmpeg) <= MAX_BED_SECONDS + 1


def test_a_server_that_ignores_range_still_works(settings, audio_bytes):
    """Not every host answers 206. Getting the whole file is a slower
    answer, not a wrong one."""
    def handler(request):
        if "api.openverse.org" in str(request.url):
            return httpx.Response(200, json=_track("a1", duration=803000))
        return httpx.Response(200, content=audio_bytes)

    choice = fetch_for_plan("p1", "a1", settings, settings.work_dir,
                            client=_client(handler))

    from pathlib import Path
    assert Path(choice.path).is_file()


def test_bytes_that_are_not_audio_are_refused(settings):
    """Better a refusal at the gate than a render that fails much later
    with ffmpeg's own wording."""
    def handler(request):
        if "api.openverse.org" in str(request.url):
            return httpx.Response(200, json=_track("a1"))
        return httpx.Response(200, content=b"<html>not audio</html>")

    with pytest.raises(SearchUnavailable):
        fetch_for_plan("p1", "a1", settings, settings.work_dir,
                       client=_client(handler))

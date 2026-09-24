"""Searching Pexels by hand, from the review board.

The automatic matcher picks one clip per slot and it is often wrong --
not usually because it ranked badly, but because the query it was given
was wrong. A real run searched "ancient greek temple columns" for a beat
about a Himalayan lake, and no amount of picking between that search's
results would have helped. So this offers both: other results for the
query that ran, and the chance to type a different one.

Two design points worth stating, because both are about what is *not*
here.

**Browsing costs nothing but the search.** Pexels returns a thumbnail URL
and a playable mp4 URL with every result, so the candidate strip points
the browser straight at Pexels' CDN. Nothing is downloaded, nothing
touches the disk, until a candidate is actually chosen. At one search
per inspected slot, a session stays far inside the 200-requests-an-hour
free tier.

**The client never supplies a URL.** A route that fetched a link the
browser handed it would fetch anything the browser handed it. So a pick
carries only a Pexels id, and the download link is read back from Pexels
by this module. The only URLs this server ever retrieves are ones Pexels
gave it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from engine.contract import Clip
from engine.media.clips import beat_output_dir

# Imported rather than restated: one definition of where Pexels lives.
from stock_agent import PexelsFetcher

SEARCH_URL = PexelsFetcher.BASE_URL
# The search endpoint is ".../videos/search"; a single video is its
# sibling. Derived so a change to the first moves the second with it.
VIDEO_URL = SEARCH_URL.rsplit("/", 1)[0] + "/videos"

# How many results a hand search offers. Enough to scroll past the
# obviously wrong ones, small enough that the strip stays a strip.
SEARCH_LIMIT = 12

# A hand-picked clip's provider. Distinct from "pexels" (the matcher
# chose it) and "upload" (the user supplied the file), because the
# publish checklist and the board both care which of the three happened.
PICKED_PROVIDER = "pexels-picked"


class SearchUnavailable(Exception):
    """Pexels could not be searched. Carries something worth showing."""


@dataclass(frozen=True)
class Candidate:
    """One search result, as the board needs it."""

    pexels_id: int
    preview: str
    play: str
    duration: int
    author: str
    source_url: str
    width: int
    height: int

    def to_dict(self) -> dict:
        return {"pexels_id": self.pexels_id, "preview": self.preview,
                "play": self.play, "duration": self.duration,
                "author": self.author, "source_url": self.source_url,
                "width": self.width, "height": self.height,
                "portrait": self.height >= self.width}


def _headers(settings) -> dict:
    key = (settings.pexels_api_key or "").strip()
    if not key:
        raise SearchUnavailable(
            "PEXELS_API_KEY is not set, so there is nothing to search. "
            "Add it to .env and restart.")
    return {"Authorization": key}


def _preview_link(video: dict) -> str:
    """A still for the card. Pexels gives one per video."""
    if video.get("image"):
        return str(video["image"])
    pictures = video.get("video_pictures") or []
    return str(pictures[0].get("picture", "")) if pictures else ""


def _playable(video: dict, *, smallest: bool) -> tuple[str, int, int]:
    """An mp4 link, its width and its height.

    ``smallest`` picks the lightest file for the preview strip -- a dozen
    4K files would be several hundred megabytes of browser traffic to
    decide one slot. The download path asks for the largest instead.
    """
    files = [f for f in (video.get("video_files") or [])
             if (f.get("file_type") == "video/mp4"
                 or str(f.get("link", "")).endswith(".mp4"))]
    if not files:
        return "", 0, 0
    files.sort(key=lambda f: int(f.get("width") or 0) * int(f.get("height") or 0))
    pick = files[0] if smallest else files[-1]
    return (str(pick.get("link", "")), int(pick.get("width") or 0),
            int(pick.get("height") or 0))


def _candidate(video: dict) -> Candidate | None:
    play, width, height = _playable(video, smallest=True)
    if not play:
        return None
    return Candidate(
        pexels_id=int(video.get("id") or 0),
        preview=_preview_link(video),
        play=play,
        duration=int(video.get("duration") or 0),
        author=str((video.get("user") or {}).get("name", "")),
        source_url=str(video.get("url", "")),
        width=width, height=height)


def search(query: str, settings, *, limit: int = SEARCH_LIMIT,
           client: httpx.Client | None = None) -> list[Candidate]:
    """Results for a query the user typed, portrait first.

    Orientation is a preference, not a filter: a hand search that
    returned nothing because the good footage was landscape would send
    the user back to guessing, and the render crops to portrait anyway.
    """
    query = query.strip()
    if not query:
        raise SearchUnavailable("type something to search for.")

    owned = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        out: list[Candidate] = []
        seen: set[int] = set()
        for orientation in ("portrait", None):
            if len(out) >= limit:
                break
            params = {"query": query, "per_page": limit}
            if orientation:
                params["orientation"] = orientation
            try:
                response = client.get(SEARCH_URL, params=params,
                                      headers=_headers(settings))
            except httpx.HTTPError as exc:
                raise SearchUnavailable(f"Pexels did not answer: {exc}")
            if response.status_code == 429:
                raise SearchUnavailable(
                    "Pexels is rate limiting this key (the free tier is "
                    "200 requests an hour). Wait a little and search "
                    "again.")
            if response.status_code != 200:
                raise SearchUnavailable(
                    f"Pexels answered {response.status_code}.")
            for video in response.json().get("videos", []):
                candidate = _candidate(video)
                if candidate and candidate.pexels_id not in seen:
                    seen.add(candidate.pexels_id)
                    out.append(candidate)
        return out[:limit]
    finally:
        if owned:
            client.close()


def used_ids(plan) -> dict[int, list[str]]:
    """Which Pexels ids this plan already uses, and where.

    Not a refusal -- the board says so and the user decides. Repeating a
    shot is occasionally deliberate, and a picker that silently refused
    would be wrong more often than the duplicate is.
    """
    out: dict[int, list[str]] = {}
    for beat in plan.script.beats:
        for slot, clip in enumerate(beat.clips):
            if clip.pexels_id:
                out.setdefault(int(clip.pexels_id), []).append(
                    f"{beat.beat_id} slot {slot}")
    return out


def place_in_slot(plan, beat, slot: int, pexels_id: int, settings,
                  work_dir: str | Path, *,
                  client: httpx.Client | None = None,
                  downloader=None) -> Clip:
    """Put a chosen Pexels video into one slot, and return the new clip.

    The download link is read back from Pexels here rather than taken
    from the request: the browser sends an id, and the only URL this
    server fetches is one Pexels handed it.

    ``duration`` is untouched. The slot is fixed by the narration and the
    render trims or loops the source to it, exactly as it does for an
    uploaded file.
    """
    owned = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        try:
            response = client.get(f"{VIDEO_URL}/{int(pexels_id)}",
                                  headers=_headers(settings))
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"Pexels did not answer: {exc}")
        if response.status_code == 404:
            raise SearchUnavailable(
                f"Pexels has no video {pexels_id} any more.")
        if response.status_code != 200:
            raise SearchUnavailable(
                f"Pexels answered {response.status_code} for video "
                f"{pexels_id}.")
        video = response.json()
        link, _w, _h = _playable(video, smallest=False)
        if not link:
            raise SearchUnavailable(
                f"Pexels video {pexels_id} has no mp4 to download.")
    finally:
        if owned:
            client.close()

    if downloader is None:
        from stock_agent import ClipDownloader
        downloader = ClipDownloader()
    target_dir = beat_output_dir(work_dir, beat.beat_id)
    path = downloader.download_clip(link, slot, output_dir=target_dir)

    existing = beat.clips[slot]
    beat.clips[slot] = Clip.model_validate({
        **existing.model_dump(),
        "path": str(path),
        "provider": PICKED_PROVIDER,
        "pexels_id": int(video.get("id") or pexels_id),
        "source_url": str(video.get("url", "")) or None,
        "author": str((video.get("user") or {}).get("name", "")) or None,
        # duration is deliberately absent: the narration owns it.
    })
    return beat.clips[slot]

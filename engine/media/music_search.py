"""Searching Openverse for a music bed, from the review board.

``assets/music/`` holds one file and ``audio.find_music`` hands it to
every reel. That is right for a placeholder and wrong for a library: the
bed is a mood choice and the mood belongs to the reel, so a chosen track
is stored per plan and the shared folder stays the fallback.

Openverse is the source for one reason no other free library gives: it
returns the **licence with every result**. Two things follow from that,
and neither is possible against a library that does not.

**A track nobody may monetise is never offered.** ``license_type`` is
sent as ``commercial`` on every search rather than filtered out of the
response, so NC tracks are refused by the API before the panel sees
them. Measured live, "mystery tension" returns 159 results unfiltered
and 138 filtered.

**Credits write themselves.** Openverse returns ready-made attribution
text, which is why CC-BY is allowed here at all: the credit can flow
into the publish payload without anyone remembering. CC0 alone measured
10 results for "dark ambient drone" against 240 for CC0 and CC-BY
together -- a library small enough to repeat itself inside a week.

A credit is emitted only where the licence requires one. CC0 results
carry attribution text as well, and putting that in a caption would be
inventing an obligation the licence does not create.

**The client never supplies a URL.** A pick carries an Openverse id and
the download link is read back from Openverse here, exactly as
``clip_search`` does with Pexels. A route that fetched the link the
browser handed it would fetch any link anyone handed it.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

SEARCH_URL = "https://api.openverse.org/v1/audio/"

# How many results a search offers. Matches clip_search.SEARCH_LIMIT for
# the same reason: enough to scroll past the wrong ones, few enough that
# the strip stays a strip.
SEARCH_LIMIT = 12

# Openverse rejects requests without a User-Agent that identifies the
# caller, and asking anonymously is rate limited harder than asking
# politely.
USER_AGENT = "rahasya-engine/0.1 (+local panel)"

# Licences that oblige a credit. Anything Openverse returns under
# ``license_type=commercial`` that is not on this list still may be used
# commercially -- it simply does not ask for a line in the caption.
#
# A whitelist rather than "cc0 is the exception": a licence code this
# code has never seen should default to *owing* a credit, because the
# failure that matters is the silent one where a reel ships without a
# credit it needed.
NO_CREDIT_LICENCES = frozenset({"cc0", "pdm"})

# What a chosen track is recorded as, alongside "pexels" and "upload" on
# the clip side.
PICKED_PROVIDER = "openverse"

# How much of a track is ever worth having. The render loops the bed to
# fill the video (``aloop=loop=-1`` in engine/assembly/render.py) and the
# longest reel this pipeline will render is 66 seconds, so anything past
# this is audio nobody can hear.
#
# It is a download bound, not a taste one. Measured against the CDN
# Openverse serves from, at roughly 75 KB/s:
#
#      16s   0.4 MB     5.9s
#      59s   1.3 MB    19.3s
#     152s   3.5 MB    46.2s
#     457s  10.2 MB   134.4s
#     803s  18.2 MB   238.8s
#
# Four minutes to put a bed on a forty-second reel is not a gate anyone
# will use twice.
#
# 75s because the longest reel this pipeline will render is 66.1s
# (``pre_render_range`` against the shipped duration window), so a bed
# this long covers every renderable reel outright and never even reaches
# its loop seam. It is deliberately not much more than that: the margin
# is audio nobody can hear, bought at roughly 23 KB of download a
# second. Raise it if the duration window is ever widened.
MAX_BED_SECONDS = 75.0

# Asked for on top of the bound, because the prefix is estimated from an
# average bitrate and a VBR track's opening can be denser than its mean.
# Cheap insurance: 15% of two minutes is about 250 KB.
PREFIX_MARGIN = 1.15

# When the track's length or the file's size is unknown there is nothing
# to estimate from, so a flat cap applies. Comfortably more than two
# minutes of anything Openverse serves.
PREFIX_BYTE_CAP = 6 * 1024 * 1024

_AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".opus"}


class SearchUnavailable(Exception):
    """Openverse could not be reached. Carries something worth showing."""


@dataclass(frozen=True)
class Candidate:
    """One search result, as the board needs it."""

    openverse_id: str
    title: str
    creator: str
    licence: str
    licence_version: str
    licence_url: str
    attribution: str
    duration: int
    preview: str
    source_url: str
    provider: str

    @property
    def needs_credit(self) -> bool:
        return self.licence.lower() not in NO_CREDIT_LICENCES

    @classmethod
    def from_result(cls, result: dict) -> "Candidate":
        return cls(
            openverse_id=str(result.get("id") or ""),
            title=str(result.get("title") or "untitled"),
            creator=str(result.get("creator") or ""),
            licence=str(result.get("license") or ""),
            licence_version=str(result.get("license_version") or ""),
            licence_url=str(result.get("license_url") or ""),
            attribution=str(result.get("attribution") or ""),
            # Openverse reports milliseconds. Kept as it comes rather
            # than converted, so nothing has to guess which unit a
            # number is in.
            duration=int(result.get("duration") or 0),
            preview=str(result.get("url") or ""),
            source_url=str(result.get("foreign_landing_url") or ""),
            provider=str(result.get("provider") or ""))

    def to_dict(self) -> dict:
        return {
            "openverse_id": self.openverse_id,
            "title": self.title,
            "creator": self.creator,
            "licence": self.licence,
            "licence_version": self.licence_version,
            "licence_url": self.licence_url,
            "attribution": self.attribution,
            "duration": self.duration,
            "seconds": round(self.duration / 1000.0, 1),
            "preview": self.preview,
            "source_url": self.source_url,
            "provider": self.provider,
            "needs_credit": self.needs_credit,
        }


@dataclass(frozen=True)
class MusicChoice:
    """A track that has been downloaded and belongs to one plan."""

    openverse_id: str
    path: str
    title: str
    creator: str
    licence: str
    attribution: str
    source_url: str

    def to_dict(self) -> dict:
        return {"openverse_id": self.openverse_id, "path": self.path,
                "title": self.title, "creator": self.creator,
                "licence": self.licence, "attribution": self.attribution,
                "source_url": self.source_url,
                "credit": credit_line(self)}


def credit_line(track) -> str:
    """The caption line this track obliges, or "" when it obliges none.

    Takes a ``Candidate`` or a ``MusicChoice``: both carry a licence and
    the text Openverse wrote, and the rule is the same for each.
    """
    licence = str(getattr(track, "licence", "") or "").lower()
    if licence in NO_CREDIT_LICENCES:
        return ""
    return str(getattr(track, "attribution", "") or "")


def _headers() -> dict:
    return {"User-Agent": USER_AGENT}


def search(query: str, settings, *, limit: int = SEARCH_LIMIT,
           client: httpx.Client | None = None) -> list[Candidate]:
    """Commercially-licensed audio matching a query the user typed.

    ``settings`` is unused today and taken anyway, so the signature
    matches ``clip_search.search`` and a future key or mirror has
    somewhere to go without every call site changing.
    """
    query = query.strip()
    if not query:
        raise SearchUnavailable("type something to search for.")

    owned = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        try:
            response = client.get(
                SEARCH_URL,
                params={"q": query,
                        # Sent, not post-filtered: a track nobody may
                        # monetise should never reach the panel.
                        "license_type": "commercial",
                        "page_size": limit},
                headers=_headers())
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"Openverse did not answer: {exc}")
        if response.status_code == 429:
            raise SearchUnavailable(
                "Openverse is rate limiting this machine. Wait a little "
                "and search again.")
        if response.status_code != 200:
            raise SearchUnavailable(
                f"Openverse answered {response.status_code}.")

        out: list[Candidate] = []
        for result in response.json().get("results", []):
            candidate = Candidate.from_result(result)
            # Openverse indexes records whose audio has since gone. A
            # card that cannot play is worse than one card fewer.
            if candidate.openverse_id and candidate.preview:
                out.append(candidate)
        return out[:limit]
    finally:
        if owned:
            client.close()


def _suffix(url: str, fallback: str = ".mp3") -> str:
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if suffix in _AUDIO_SUFFIXES else fallback


def _prefix_bytes(track: "Candidate", total: int | None) -> int | None:
    """How many bytes hold ``MAX_BED_SECONDS`` of this track.

    None means "take the whole thing": either there is nothing to
    estimate from, or the track is already short enough that asking for
    a prefix would be a range past the end of the file.
    """
    seconds = track.duration / 1000.0
    if not total or seconds <= MAX_BED_SECONDS:
        return None
    share = (MAX_BED_SECONDS / seconds) * PREFIX_MARGIN
    wanted = min(int(math.ceil(total * share)), PREFIX_BYTE_CAP)
    return wanted if wanted < total else None


def _fetch_prefix(client: httpx.Client, track: "Candidate",
                  part: Path) -> None:
    """Download enough of ``track`` to fill a reel, into ``part``.

    Range is a request, not a requirement. A host that answers 200 with
    the whole file has given a slower answer, not a wrong one, and the
    trim downstream makes the two identical on disk.
    """
    # HEAD, so the size is learned without pulling a body that would
    # then be pulled again. A host that refuses HEAD simply leaves the
    # size unknown, and the flat cap applies instead.
    total = None
    try:
        probe = client.head(track.preview, headers=_headers())
        if probe.status_code < 400:
            total = int(probe.headers.get("Content-Length") or 0) or None
    except httpx.HTTPError:
        pass

    headers = dict(_headers())
    wanted = _prefix_bytes(track, total)
    if wanted:
        headers["Range"] = f"bytes=0-{wanted - 1}"

    response = client.get(track.preview, headers=headers)
    if response.status_code not in (200, 206):
        raise SearchUnavailable(
            f"the audio file answered {response.status_code}.")
    part.write_bytes(response.content)


def _trim(part: Path, target: Path, settings) -> None:
    """Re-encode the fetched prefix into a clean, bounded mp3.

    Re-encoded rather than copied because a prefix of an mp3 ends
    mid-frame, and the bed is composited into the reel -- a torn last
    frame is audible. Running ffmpeg over it is also the check that what
    came back is audio at all: an HTML error page saved under a .mp3
    name would otherwise fail much later, inside the render, wearing
    ffmpeg's wording instead of this gate's.
    """
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-i", str(part), "-t", f"{MAX_BED_SECONDS:.3f}", "-vn",
         "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(target)],
        capture_output=True, text=True)
    if result.returncode != 0 or not target.is_file() \
            or target.stat().st_size == 0:
        raise SearchUnavailable(
            "that track did not decode as audio, so it cannot be used as "
            f"a bed: {(result.stderr or '').strip()[-200:]}")


def fetch_for_plan(plan_id: str, openverse_id: str, settings,
                   work_dir: str | Path, *,
                   client: httpx.Client | None = None) -> MusicChoice:
    """Download a chosen track and return it as this plan's bed.

    The download link is read back from Openverse rather than taken from
    the request: the browser sends an id, and the only URL this server
    fetches is one Openverse named.

    The file lands under the plan's own work directory. Writing it to
    ``assets/music/`` would be a per-reel choice silently re-scoring
    every other reel, including ones already reviewed and waiting.
    """
    openverse_id = str(openverse_id or "").strip()
    if not openverse_id:
        raise SearchUnavailable("no track was chosen.")

    owned = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        detail_url = f"{SEARCH_URL}{openverse_id}/"
        try:
            response = client.get(detail_url, headers=_headers())
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"Openverse did not answer: {exc}")
        if response.status_code != 200:
            raise SearchUnavailable(
                f"Openverse answered {response.status_code} for that "
                f"track; it may have been withdrawn.")
        track = Candidate.from_result(response.json())
        if not track.preview:
            raise SearchUnavailable(
                "Openverse has no audio file for that track any more.")

        target_dir = Path(work_dir) / plan_id / "music"
        target_dir.mkdir(parents=True, exist_ok=True)
        # Always mp3: the fetched prefix is re-encoded below, so the
        # source container stops mattering once it is on disk.
        target = target_dir / f"{openverse_id}.mp3"
        # Fetched to .part and only renamed once ffmpeg has accepted it,
        # as every other download here does: a bed half on disk is one
        # the render would happily composite.
        part = target_dir / f"{openverse_id}.part"
        try:
            _fetch_prefix(client, track, part)
            _trim(part, target, settings)
        except (httpx.HTTPError, SearchUnavailable, OSError) as exc:
            part.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            if isinstance(exc, SearchUnavailable):
                raise
            raise SearchUnavailable(f"the audio file did not download: {exc}")
        finally:
            part.unlink(missing_ok=True)

        return MusicChoice(
            openverse_id=track.openverse_id,
            path=str(target),
            title=track.title,
            creator=track.creator,
            licence=track.licence,
            attribution=track.attribution,
            source_url=track.source_url)
    finally:
        if owned:
            client.close()

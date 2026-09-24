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

import os
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
        target = target_dir / f"{openverse_id}{_suffix(track.preview)}"
        # Written to .part and renamed, as every other download here
        # does: a bed half on disk is one the render would happily
        # composite.
        part = target.with_suffix(target.suffix + ".part")
        try:
            audio = client.get(track.preview, headers=_headers())
            if audio.status_code != 200:
                raise SearchUnavailable(
                    f"the audio file answered {audio.status_code}.")
            part.write_bytes(audio.content)
            os.replace(part, target)
        except httpx.HTTPError as exc:
            part.unlink(missing_ok=True)
            raise SearchUnavailable(f"the audio file did not download: {exc}")
        except SearchUnavailable:
            part.unlink(missing_ok=True)
            raise

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

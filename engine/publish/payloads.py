"""Publish payload builders.

These are pure functions. This module imports no HTTP library, makes no
network call, and is never imported by the scheduler or the pipeline — a
human runs publishing, deliberately, with the video in front of them. A test
asserts the no-network property so it cannot drift.

Both builders set the synthetic-media disclosure. That is a QC hard-fail
elsewhere in the pipeline, and mandatory since the Studio disclosure tightening
in January 2026.

Instagram note: the Content Publishing API needs a Business or Creator account
linked to a Facebook Page, and a publicly reachable ``video_url`` — it will not
accept a local file. That is a human setup prerequisite, not something this
code can arrange.
"""

from __future__ import annotations

from engine.contract import ReelPlan

YOUTUBE_CATEGORY_PEOPLE_BLOGS = "22"
MAX_TITLE = 100
MAX_IG_CAPTION = 2200


def _hashtag_block(plan: ReelPlan, limit: int) -> str:
    if not plan.metadata or not plan.metadata.hashtags:
        return ""
    tags = [t if t.startswith("#") else f"#{t}"
            for t in plan.metadata.hashtags[:limit]]
    return " ".join(tags)


def _sources_block(plan: ReelPlan) -> str:
    urls = sorted({c.source_url for c in plan.provenance.claims
                   if c.source_url})
    if not urls:
        return ""
    return "Sources:\n" + "\n".join(urls)


def youtube_payload(plan: ReelPlan, video_path: str) -> dict:
    """Body for ``youtube.videos.insert``, plus the local file to upload.

    Privacy is ``private`` on purpose: the upload lands unlisted so a human
    flips it public after watching it back.
    """
    if not plan.metadata:
        raise ValueError("plan has no metadata; run the metadata agent first")

    description_parts = [plan.metadata.yt_description.strip()]
    sources = _sources_block(plan)
    if sources:
        description_parts.append(sources)
    tags_line = _hashtag_block(plan, 8)
    if tags_line:
        description_parts.append(tags_line)

    return {
        "_video_file": video_path,
        "_pinned_comment": plan.metadata.pinned_comment,
        "snippet": {
            "title": plan.metadata.yt_title[:MAX_TITLE],
            "description": "\n\n".join(p for p in description_parts if p),
            "tags": [t.lstrip("#") for t in plan.metadata.hashtags[:15]],
            "categoryId": YOUTUBE_CATEGORY_PEOPLE_BLOGS,
            "defaultLanguage": "hi",
            "defaultAudioLanguage": "hi",
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
            "license": "youtube",
            "embeddable": True,
        },
    }


def instagram_payload(plan: ReelPlan, video_url: str) -> dict:
    """Body for the Instagram Content Publishing API container step.

    ``video_url`` must be publicly reachable — the API fetches it rather than
    accepting an upload.
    """
    if not plan.metadata:
        raise ValueError("plan has no metadata; run the metadata agent first")

    caption = plan.metadata.ig_caption.strip()
    tags_line = _hashtag_block(plan, 8)
    if tags_line:
        caption = f"{caption}\n\n{tags_line}"

    return {
        "_pinned_comment": plan.metadata.pinned_comment,
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption[:MAX_IG_CAPTION],
        "share_to_feed": True,
        "audio_name": "original",
    }


def publish_checklist(plan: ReelPlan, video_path: str, *,
                      actual_duration: float | None = None) -> list[str]:
    """What a human should confirm before publishing. Shown in the UI.

    ``actual_duration`` is the probed MP4 length. The beat sum is longer,
    because xfade transitions overlap, so quoting it here would tell someone
    to watch five seconds that do not exist.
    """
    length = actual_duration if actual_duration else plan.duration()
    items = [
        f"Watch all {length:.0f}s back with sound on",
        "First 3 seconds: does it stop a scroll?",
        "Captions readable on a phone, nothing clipped",
        "Synthetic-media disclosure toggle set in Studio",
        "Pinned comment ready to post immediately after publishing",
    ]
    unsourced = [c.text[:50] for c in plan.provenance.claims
                 if not c.source_url]
    if unsourced:
        items.append(f"UNSOURCED CLAIMS present: {unsourced}")
    placeholders = [b.beat_id for b in plan.script.beats
                    if b.image_provider == "placeholder"]
    if placeholders:
        items.append(
            f"{len(placeholders)} beats use placeholder visuals "
            f"({', '.join(placeholders[:4])}...) — add an image provider "
            f"before publishing this publicly")
    return items

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


def youtube_payload(plan: ReelPlan, video_path: str, *,
                    attribution: str | None = None) -> dict:
    """Body for ``youtube.videos.insert``, plus the local file to upload.

    Privacy is ``private`` on purpose: the upload lands unlisted so a human
    flips it public after watching it back.

    ``attribution`` is the credit line the render recorded when it ran (see
    ``Store.record_render``), or None. It is a stored fact about the MP4, not
    something worked out here: deriving it from today's settings and today's
    baked art would put the credit on videos that contain no designed art and
    — the licence-breaking direction — take it off videos that do.
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
    credit = attribution
    if credit:
        description_parts.append(credit)

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


def instagram_payload(plan: ReelPlan, video_url: str, *,
                      attribution: str | None = None) -> dict:
    """Body for the Instagram Content Publishing API container step.

    ``video_url`` must be publicly reachable — the API fetches it rather than
    accepting an upload.

    ``attribution`` is the credit line the render recorded when it ran (see
    ``Store.record_render``), or None. It is a stored fact about the MP4, not
    something worked out here: deriving it from today's settings and today's
    baked art would put the credit on videos that contain no designed art and
    — the licence-breaking direction — take it off videos that do.
    """
    if not plan.metadata:
        raise ValueError("plan has no metadata; run the metadata agent first")

    caption = plan.metadata.ig_caption.strip()
    tags_line = _hashtag_block(plan, 8)
    if tags_line:
        caption = f"{caption}\n\n{tags_line}"

    # The credit is a licence requirement, so it outranks the tail of the
    # caption: reserve its room *before* the cut and append it after. Doing
    # it the other way round silently drops the credit on exactly the
    # captions long enough to need trimming -- which are the hashtag-heavy
    # ones this channel actually writes.
    credit = attribution
    if credit:
        caption = caption[:MAX_IG_CAPTION - len(credit) - 2].rstrip()
        caption = f"{caption}\n\n{credit}"
    else:
        caption = caption[:MAX_IG_CAPTION]

    return {
        "_pinned_comment": plan.metadata.pinned_comment,
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
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
    if plan.safety.moderation_unavailable:
        items.insert(1, "MODERATION WAS NOT RUN - read the script yourself "
                        f"before publishing ({plan.safety.moderation_unavailable})")

    unsourced = [c.text[:50] for c in plan.provenance.claims
                 if not c.source_url]
    if unsourced:
        items.append(f"UNSOURCED CLAIMS present: {unsourced}")

    # Visuals are Pexels stock clips now (see engine/media/clips.py), with
    # still images as the per-slot fallback and a generated gradient frame
    # as the last resort. A beat with clips is judged by its clips'
    # providers; a beat with none is a plan stored before clips existed, and
    # is judged by the legacy beat.image_provider field instead — that field
    # is never set by the current pipeline, so this branch only fires for
    # old plans.
    #
    # A provider is "deliberate" when it reflects a human or the pipeline
    # choosing exactly what ended up in the video, on purpose: a fetched
    # Pexels clip, or a clip the user watched and replaced through the
    # review gate (provider="upload"). Everything else is some tier of the
    # image-fallback chain (generate_beat_image's "keyless"/"placeholder"/
    # gateway-name returns) or an unfilled slot, and genuinely means "we did
    # not get the footage we wanted" — including any new provider name that
    # chain grows later, since it starts out unlisted here rather than
    # silently passing as deliberate.
    DELIBERATE_PROVIDERS = {"pexels", "upload"}
    fallback_ids = []
    placeholder_ids = []
    all_clip_providers: set[str] = set()
    any_clips = False
    for beat in plan.script.beats:
        if beat.clips:
            any_clips = True
            providers = {c.provider for c in beat.clips}
            all_clip_providers |= providers
            if providers - DELIBERATE_PROVIDERS:
                fallback_ids.append(beat.beat_id)
            if "placeholder" in providers:
                placeholder_ids.append(beat.beat_id)
        elif beat.image_provider == "placeholder":
            fallback_ids.append(beat.beat_id)
            placeholder_ids.append(beat.beat_id)

    if placeholder_ids:
        items.append(
            f"{len(placeholder_ids)} beats show blank placeholder frames "
            f"({', '.join(placeholder_ids[:4])}...) — add a Pexels key or "
            f"an image provider before publishing this publicly")
    stills_only = [b for b in fallback_ids if b not in placeholder_ids]
    if stills_only:
        items.append(
            f"{len(stills_only)} beats fell back to still images instead "
            f"of stock video ({', '.join(stills_only[:4])}...) — add a "
            f"Pexels key before publishing this publicly")

    # Informational, not a warning: every clip in the render was a hand-
    # supplied upload, so there is no stock footage to double-check — but a
    # publisher may still want to know the whole visual track was manually
    # curated rather than fetched.
    if any_clips and all_clip_providers == {"upload"}:
        items.append(
            "All visuals were hand-supplied uploads (no stock footage "
            "was used) — informational, not a warning")
    return items

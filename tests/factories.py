"""Shared builders so every test starts from a valid ReelPlan."""

import os

from engine.contract import (Beat, Claim, Clip, Hook, Metadata, Provenance,
                             ReelPlan, Safety, Script, Topic)
from engine.media.clips import clip_count, slot_durations

HINGLISH = [
    ("रूपकुंड झील में पाँच सौ कंकाल मिले",
     "Roopkund jheel mein 500 kankaal mile"),
    ("कोई नहीं जानता ये लोग कौन थे",
     "Koi nahi jaanta ye log kaun the"),
    ("सिर पर गहरी चोट के निशान थे",
     "Sir par gehri chot ke nishaan the"),
    ("सब एक ही समय पर मरे थे",
     "Sab ek hi samay par mare the"),
    ("डीएनए ने कुछ और बताया",
     "DNA ne kuch aur bataya"),
    ("कुछ कंकाल भूमध्य सागर से थे",
     "Kuch kankaal Bhumadhya Saagar se the"),
    ("वो भारत क्यों आए थे",
     "Wo Bharat kyun aaye the"),
    ("लेकिन रिपोर्ट अधूरी छोड़ दी गई",
     "Lekin report adhoori chhod di gayi"),
    ("आज भी जवाब नहीं मिला",
     "Aaj bhi jawaab nahi mila"),
    ("तुम्हें क्या लगता है सच क्या है",
     "Tumhe kya lagta hai sach kya hai"),
]

# Every RAHASYA_* key that a checked-in default also defines. A developer's
# own .env must not decide whether a test of the shipped defaults passes:
# this repo's .env still pins the old speech rate, and reading it would have
# hidden the very change that fixes it.
TUNING_KEYS = (
    "RAHASYA_WORDS_PER_SEC",
    "RAHASYA_MODEL_STRONG",
    "RAHASYA_MODEL_CHEAP",
    "RAHASYA_MODEL_TRIM",
    "RAHASYA_MODEL_IMAGE",
    "RAHASYA_MODEL_EMBED",
    "RAHASYA_STICKERS",
    "RAHASYA_STICKER_MAX",
    "RAHASYA_STICKER_FONT",
    "RAHASYA_STICKER_SCALE",
    "RAHASYA_ALIGN",
    "RAHASYA_ALIGN_MODEL",
    "RAHASYA_ALIGN_COMPUTE",
    "RAHASYA_ALIGN_DEVICE",
    "RAHASYA_ALIGN_LANG",
    "RAHASYA_VOICE_CLEAN",
    "RAHASYA_VOICE_HIGHPASS",
    "RAHASYA_VOICE_DENOISE",
    "RAHASYA_VOICE_PAUSE_CAP",
    "RAHASYA_VOICE_SILENCE_DB",
)


def shipped_settings(**overrides):
    """``Settings`` as the repo ships it, ignoring the local .env.

    ``overrides`` are passed straight through to the constructor -- e.g.
    ``shipped_settings(target_seconds=60.0)`` -- so a test can ask what the
    shipped defaults would be at a target other than the configured one,
    without hand-rolling a second ``Settings()`` that silently picks up
    whatever the local .env pins.
    """
    from engine.config import Settings

    saved = {k: os.environ.pop(k, None) for k in TUNING_KEYS}
    try:
        return Settings(**overrides)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


MOTIONS = ["zoom_in", "zoom_out", "move_left", "move_right"]
ROLES = ["hook", "setup", "escalation", "reveal", "twist", "escalation",
         "reveal", "twist", "cliffhanger", "cta"]


def make_beat(i, *, target=4.4, measured=None, words=None):
    voice, caption = HINGLISH[i % len(HINGLISH)]
    return Beat(
        beat_id=f"b{i}", role=ROLES[i % len(ROLES)],
        voice_text=voice, caption_text=caption,
        on_screen_text=None, target_seconds=target,
        visual_prompt=f"dark cinematic scene {i}",
        motion=MOTIONS[i % len(MOTIONS)], transition="fade",
        measured_seconds=measured,
        words=words or [],
    )


def fill_clips(plan, *, provider="pexels"):
    """Give every beat the clips ``generate_plan_clips`` would give it.

    Production puts SEVERAL clips inside one beat — the slot count comes from
    ``clip_count`` and the slots subdivide the beat's own span. A fixture that
    hands each beat a single clip is better behaved than anything the pipeline
    produces, and it hides exactly the bugs that confuse a beat with a visual.
    """
    for beat in plan.script.beats:
        span = beat.seconds()
        beat.clips = [
            Clip(path=f"{beat.beat_id}-{slot}.mp4", query=beat.visual_prompt,
                 provider=provider, duration=duration)
            for slot, duration in enumerate(
                slot_durations(span, clip_count(span)))]
    return plan


def make_plan(*, plan_id="p1", raw="Roopkund jheel ke kankaal", beats=10,
              measured=4.4, sourced=True, moderated=True,
              with_metadata=True, clips=False):
    styles = ["question", "claim", "number", "contradiction", "threat"]
    hooks = [Hook(variant_id=f"h{i + 1}", voice_text=HINGLISH[0][0],
                  caption_text=HINGLISH[0][1], style=styles[i], seconds=3.0)
             for i in range(5)]
    plan = ReelPlan(
        plan_id=plan_id,
        topic=Topic.make(raw, entities=["Roopkund", "Uttarakhand"]),
        hooks=hooks,
        script=Script(total_seconds=44.0, chosen_hook="h1",
                      beats=[make_beat(i, measured=measured)
                             for i in range(beats)]),
        provenance=Provenance(claims=[
            Claim(beat_id="b0", text="Roopkund mein ~500 kankaal",
                  source_url=("https://example.org/roopkund" if sourced
                              else None),
                  confidence="high")]),
        safety=Safety(moderation_passed=moderated, flags=[]),
    )
    if with_metadata:
        plan.metadata = Metadata(
            yt_title="Roopkund: 500 Kankaal, Ek Bhi Jawaab Nahi",
            yt_description="Uttarakhand ki ek jheel ka anzaana sach.",
            ig_caption="500 kankaal, ek hi raat. Sach kya hai?",
            pinned_comment=("Official report kehti hai ye teerthyatri the. "
                            "Locals 60 saal se kuch aur bolte hain. "
                            "Tum kispe bharosa karoge?"),
            hashtags=["#roopkund", "#rahasya", "#unsolved"],
            thumbnail_prompt="skeletal remains on a frozen lake, moonlight")
    if clips:
        fill_clips(plan)
    return plan

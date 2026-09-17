"""Bridge to the existing EDITOR-_BACKEND renderer.

The ffmpeg graph in ``engine.assembly.render`` is the default because it is
verified working on this machine. This module keeps the moviepy service usable
as an alternative renderer, so the work already in that repo is not stranded:
feed the returned body to ``POST /generate-video``.

Requires the three fixes in docs/fix-editor-backend.md — notably the hardcoded
``BASE`` path in ``final/generate_video_final.py``, which otherwise rewrites
every absolute path we pass.
"""

from __future__ import annotations

from pathlib import Path

from engine.contract import ReelPlan

# The legacy renderer validates against its own vocabulary and raises on
# anything else, so unsupported values are mapped rather than passed through.
LEGACY_TRANSITIONS = {"fade", "slide_left", "slide_right", "slide_up",
                      "slide_down", "zoom", "blur", "random"}
LEGACY_MOTIONS = {"zoom_in", "zoom_out", "move_left", "move_right"}


def to_generate_video_body(plan: ReelPlan, output_name: str = "output.mp4", *,
                           audio_path: str | None = None,
                           transition_duration: float = 0.5,
                           frame_size: tuple[int, int] = (1080, 1920),
                           layout_mode: str = "blur_bg") -> dict:
    """Shape a ReelPlan into the body that endpoint already accepts.

    Durations come from measured TTS audio via ``Beat.seconds()``; the model's
    ``target_seconds`` is only ever a script-length hint.
    """
    beats = plan.script.beats
    if not beats:
        raise ValueError("plan has no beats")

    missing = [b.beat_id for b in beats if not b.image_path]
    if missing:
        raise ValueError(f"beats without an image: {missing}")

    if audio_path is None:
        audio_path = next((b.audio_path for b in beats if b.audio_path), None)
    if not audio_path:
        raise ValueError("no audio track available for the legacy renderer")

    images = []
    for beat in beats:
        transition = (beat.transition if beat.transition in LEGACY_TRANSITIONS
                      else "fade")
        motion = beat.motion if beat.motion in LEGACY_MOTIONS else "zoom_in"
        images.append({
            "path": str(Path(beat.image_path)),
            "duration": round(beat.seconds(), 3),
            "transition": transition,
            "motion": motion,
            "motion_speed": 1.0,
        })

    return {
        "images": images,
        "audio_path": str(Path(audio_path)),
        "output_name": output_name,
        "settings": {
            "duration": round(plan.duration(), 3),
            "cinematic": True,
            "transition_duration": transition_duration,
            "transition_type": "fade",
            "motion_type": "zoom_in",
            "motion_speed": 1.0,
            "layout_mode": layout_mode,
            "frame_size": list(frame_size),
        },
    }

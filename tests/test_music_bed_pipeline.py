"""The reel's own bed reaching the render, and its credit reaching the record.

``resolve_bed`` and ``join_credits`` are unit-tested next door. This is
the wire between them and ``render_stage`` -- the part that, missing,
leaves a picked track sitting in the database while every reel keeps
rendering the shared placeholder.

The credit case is the one that matters most. A CC-BY bed obliges a
line in the caption, the publish payload reads that line from the render
record, and the record is written here. Break this and the reel ships
without the credit its licence required, silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.pipeline import render_stage
from tests.test_pipeline import _attribution_harness


def _bed(tmp_path, name="a1.mp3"):
    path = Path(tmp_path) / "bed" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + b"\0" * 200)
    return str(path)


def _capture_music(monkeypatch):
    """Records the music_path the render was actually handed."""
    seen = {}
    import engine.pipeline as pipeline_mod
    original = pipeline_mod.render

    def spy(plan_, settings_, out_path, *, report=None, music_path=None,
            **kwargs):
        seen["music_path"] = music_path
        return original(plan_, settings_, out_path, report=report,
                        music_path=music_path, **kwargs)

    monkeypatch.setattr(pipeline_mod, "render", spy)
    return seen


def test_the_reels_own_bed_reaches_the_render(tmp_path, monkeypatch):
    plan, store, settings, _ = _attribution_harness(tmp_path, monkeypatch,
                                                    None)
    seen = _capture_music(monkeypatch)
    chosen = _bed(tmp_path)
    store.set_music_choice(plan.plan_id, openverse_id="a1", path=chosen,
                           title="Creepy", creator="someone",
                           licence="cc0", attribution="", source_url="")

    render_stage(plan, store, settings)

    assert seen["music_path"] == chosen


def test_a_reel_that_chose_nothing_renders_as_it_always_did(tmp_path,
                                                            monkeypatch):
    """The regression that matters: every existing plan."""
    plan, store, settings, _ = _attribution_harness(tmp_path, monkeypatch,
                                                    None)
    seen = _capture_music(monkeypatch)

    render_stage(plan, store, settings)

    from engine.assembly import audio
    assert seen["music_path"] == audio.find_music(settings)


def test_a_cc_by_bed_puts_its_credit_in_the_render_record(tmp_path,
                                                          monkeypatch):
    plan, store, settings, _ = _attribution_harness(tmp_path, monkeypatch,
                                                    None)
    _capture_music(monkeypatch)
    store.set_music_choice(
        plan.plan_id, openverse_id="a1", path=_bed(tmp_path),
        title="Creepy", creator="someone", licence="by",
        attribution='"Creepy" by someone, CC BY 4.0', source_url="")

    render_stage(plan, store, settings)

    assert "CC BY 4.0" in store.render_attribution(plan.plan_id)


def test_art_and_music_credits_both_survive(tmp_path, monkeypatch):
    """A reel can owe two. Dropping either breaks a licence, and the
    direction that breaks it is the quiet one."""
    plan, store, settings, _ = _attribution_harness(
        tmp_path, monkeypatch, "Animated icons by Lordicon.com")
    _capture_music(monkeypatch)
    store.set_music_choice(
        plan.plan_id, openverse_id="a1", path=_bed(tmp_path),
        title="Creepy", creator="someone", licence="by",
        attribution='"Creepy" by someone, CC BY 4.0', source_url="")

    render_stage(plan, store, settings)

    recorded = store.render_attribution(plan.plan_id)
    assert "Lordicon" in recorded
    assert "CC BY 4.0" in recorded


def test_a_cc0_bed_adds_no_credit_to_the_art_credit(tmp_path, monkeypatch):
    plan, store, settings, _ = _attribution_harness(
        tmp_path, monkeypatch, "Animated icons by Lordicon.com")
    _capture_music(monkeypatch)
    store.set_music_choice(
        plan.plan_id, openverse_id="a1", path=_bed(tmp_path),
        title="Creepy", creator="someone", licence="cc0",
        attribution='"Creepy" by someone is marked CC0 1.0',
        source_url="")

    render_stage(plan, store, settings)

    assert store.render_attribution(plan.plan_id) \
        == "Animated icons by Lordicon.com"


def test_an_explicit_music_path_still_wins(tmp_path, monkeypatch):
    """render_stage's own parameter is the caller being explicit; a
    stored choice must not quietly override it."""
    plan, store, settings, _ = _attribution_harness(tmp_path, monkeypatch,
                                                    None)
    seen = _capture_music(monkeypatch)
    store.set_music_choice(plan.plan_id, openverse_id="a1",
                           path=_bed(tmp_path, "stored.mp3"), title="",
                           creator="", licence="cc0", attribution="",
                           source_url="")
    explicit = _bed(tmp_path, "explicit.mp3")

    render_stage(plan, store, settings, music_path=explicit)

    assert seen["music_path"] == explicit

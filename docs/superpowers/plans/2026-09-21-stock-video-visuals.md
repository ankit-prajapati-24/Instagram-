# Stock Video Visuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace still-image scene visuals with real Pexels stock footage, matched per beat by `stock_agent.py`, keeping the image chain as a fallback.

**Architecture:** A new `engine/media/clips.py` mirrors `engine/media/images.py` and wraps the existing `StockVideoMatcherAgent` as a library, one `match()` call per beat across a thread pool. Each beat's measured narration span is divided into evenly sized clip slots; clips are hard-cut inside a beat and crossfaded only at beat boundaries, so the beat-level timeline that `segment_lengths` already guarantees is untouched. Any slot the agent cannot fill falls through to the existing image chain.

**Tech Stack:** Python 3.14, Pexels REST API, `openai` SDK (via OmniRoute), ffmpeg v7.1 from `imageio-ffmpeg`, pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-stock-video-visuals-design.md`

## Global Constraints

- One clip per 2.5 seconds: `ceil(duration / 2.5)`, already implemented by `VisualQueryGenerator.calculate_clip_count`.
- A clip never straddles a beat boundary. Per-beat segmentation is what preserves A/V sync.
- `beat.seconds() == sum(clip.duration for clip in beat.clips)` — exact, not approximate. Remainder goes in the last slot.
- Clip count and slot size derive from `beat.measured_seconds` only. Never `target_seconds`.
- Visuals run **after** voice. Stage order is `VOICE → CLIPS → CAPTIONS → RENDER`.
- `stock_agent.py` is not modified. Its `main()` CLI must keep working.
- Source audio is dropped from every video input.
- Source footage is fitted to its slot: longer is trimmed, shorter is looped.
- A missing clip never fails a render. Fallback order: Pexels clip → image chain → placeholder.
- Hard cuts inside a beat. `xfade` only at beat boundaries, still driven by `Beat.transition`.
- `zoompan` applies only to fallback stills, never to video clips.

---

### Task 1: Green baseline

Three dependencies that arrived with `stock_agent.py` are not installed, so `tests/test_stock_agent.py` errors at setup. Nothing else can be trusted until the suite is clean.

**Files:**
- Modify: none
- Test: `tests/test_stock_agent.py` (existing, currently erroring)

**Interfaces:**
- Consumes: nothing
- Produces: a green test suite, which every later task's "run the tests" step depends on

- [ ] **Step 1: Confirm the failure**

Run: `python -m pytest -q`
Expected: `3 failed, 223 passed, 5 errors` — errors are `tests/test_stock_agent.py` at setup.

- [ ] **Step 2: Install the missing dependencies**

```bash
pip install -r requirements.txt
```

- [ ] **Step 3: Confirm they import**

```bash
python -c "import dotenv, openai, rich, requests; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests pass, 0 failed, 0 errors. If any test still fails, stop and report — do not proceed with a red baseline.

- [ ] **Step 5: Commit**

Nothing to commit if `requirements.txt` was already correct. If pip changed a lockfile or `requirements.txt`, commit it:

```bash
git add requirements.txt
git commit -m "chore: install the stock agent's dependencies"
```

---

### Task 2: The Clip model

**Files:**
- Modify: `engine/contract.py` (add `Clip` after `WordTiming`, add `clips` field to `Beat`)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `Coercing` base class from `engine/contract.py`
- Produces: `Clip` model with fields `path: str`, `query: str`, `provider: str`, `duration: float`, `source_url: str | None`, `pexels_id: int | None`, `author: str | None`, `licence: str | None`; and `Beat.clips: list[Clip]` defaulting to `[]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contract.py`:

```python
from engine.contract import Clip


def test_clip_requires_only_the_four_render_critical_fields():
    clip = Clip(path="c.mp4", query="dark forest fog", provider="pexels",
                duration=2.5)
    assert clip.source_url is None
    assert clip.pexels_id is None
    assert clip.licence is None


def test_clip_coerces_a_string_duration():
    """The agent's JSON round-trip can hand back "2.5s"."""
    clip = Clip(path="c.mp4", query="q", provider="pexels", duration="2.5s")
    assert clip.duration == 2.5


def test_beat_starts_with_no_clips():
    beat = Beat(beat_id="b1", role="hook", voice_text="क",
                caption_text="k", target_seconds=3.0,
                visual_prompt="p", motion="zoom_in", transition="fade")
    assert beat.clips == []


def test_beat_carries_clips():
    beat = Beat(beat_id="b1", role="hook", voice_text="क",
                caption_text="k", target_seconds=3.0,
                visual_prompt="p", motion="zoom_in", transition="fade",
                clips=[{"path": "a.mp4", "query": "q", "provider": "pexels",
                        "duration": 1.5},
                       {"path": "b.mp4", "query": "q2", "provider": "pexels",
                        "duration": 1.5}])
    assert len(beat.clips) == 2
    assert beat.clips[0].path == "a.mp4"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_contract.py -q -k clip`
Expected: FAIL with `ImportError: cannot import name 'Clip'`

- [ ] **Step 3: Add the model**

In `engine/contract.py`, after the `WordTiming` class:

```python
class Clip(Coercing):
    """One stock-footage slot inside a beat.

    ``duration`` is the slot this clip fills on the narration timeline, not
    the length of the source file. Source footage is trimmed or looped to
    match; its own length never moves the timeline.
    """

    path: str
    query: str
    provider: str
    duration: float
    source_url: str | None = None
    pexels_id: int | None = None
    author: str | None = None
    licence: str | None = None
```

In the `Beat` class, after the `words` field:

```python
    # Stock footage filling this beat's span. Empty means the render falls
    # back to image_path for the whole beat.
    clips: list[Clip] = Field(default_factory=list)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_contract.py -q`
Expected: PASS, and no previously-passing contract test breaks.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass. `Beat` gained an optional field, so stored plans still load.

- [ ] **Step 6: Commit**

```bash
git add engine/contract.py tests/test_contract.py
git commit -m "feat: a Beat can carry stock clips"
```

---

### Task 3: Slot arithmetic

A beat's span is divided into evenly sized slots. This is pure arithmetic, separated from any network call so it can be tested exhaustively.

**Files:**
- Create: `engine/media/clips.py`
- Test: `tests/test_clips.py`

**Interfaces:**
- Consumes: nothing
- Produces: `clip_count(seconds: float) -> int` and `slot_durations(total: float, count: int) -> list[float]` in `engine.media.clips`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clips.py`:

```python
import math

import pytest

from engine.media.clips import clip_count, slot_durations


@pytest.mark.parametrize("seconds,expected", [
    (1.0, 1), (2.5, 1), (2.6, 2), (5.0, 2), (5.1, 3), (12.0, 5),
])
def test_clip_count_is_one_per_two_and_a_half_seconds(seconds, expected):
    assert clip_count(seconds) == expected


def test_clip_count_never_returns_zero():
    assert clip_count(0.0) == 1
    assert clip_count(0.1) == 1


def test_slots_sum_to_the_total_exactly():
    """Approximately right is not right: the render reads these as a
    timeline, and a rounding crumb per slot becomes visible drift."""
    for total in (3.0, 4.7, 12.345, 0.9):
        for count in (1, 2, 3, 5, 7):
            slots = slot_durations(total, count)
            assert len(slots) == count
            assert sum(slots) == pytest.approx(total, abs=1e-9)


def test_slots_are_even_apart_from_the_remainder():
    slots = slot_durations(10.0, 4)
    assert slots == [2.5, 2.5, 2.5, 2.5]


def test_the_remainder_lands_in_the_last_slot():
    slots = slot_durations(10.0, 3)
    assert slots[0] == slots[1]
    assert slots[2] >= slots[0]
    assert sum(slots) == pytest.approx(10.0, abs=1e-9)


def test_a_single_slot_spans_the_whole_beat():
    assert slot_durations(4.2, 1) == [4.2]


def test_zero_count_is_rejected():
    with pytest.raises(ValueError):
        slot_durations(5.0, 0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_clips.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.media.clips'`

- [ ] **Step 3: Create the module with just the arithmetic**

Create `engine/media/clips.py`:

```python
"""Stock footage as the visual layer.

One Pexels clip per 2.5 seconds of narration, matched per beat so a clip
never straddles a beat boundary. That constraint is what keeps the A/V sync
work in ``engine/assembly/render.py`` intact: the clip layer only subdivides
a span the beat already owns.

Falls back to the still-image chain in ``images.py`` for any slot the agent
cannot fill, so a missing clip never fails a render.
"""

from __future__ import annotations

import math

# The agent generates one query per this many seconds. Mirrors
# VisualQueryGenerator.calculate_clip_count so the two cannot drift apart.
SECONDS_PER_CLIP = 2.5


def clip_count(seconds: float) -> int:
    """How many clips fill a span of narration."""
    return max(1, math.ceil(max(1e-9, float(seconds)) / SECONDS_PER_CLIP))


def slot_durations(total: float, count: int) -> list[float]:
    """Divide ``total`` into ``count`` slots that sum to it exactly.

    The remainder goes in the last slot rather than being spread, because
    the sum has to be exact: the render reads these as a timeline, and a
    rounding crumb per slot accumulates into visible drift.
    """
    if count < 1:
        raise ValueError(f"a beat needs at least one clip slot, got {count}")
    even = total / count
    slots = [even] * (count - 1)
    return slots + [total - sum(slots)]
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_clips.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/media/clips.py tests/test_clips.py
git commit -m "feat: slot arithmetic for dividing a beat into clips"
```

---

### Task 4: Fill a beat's clips from the agent

**Files:**
- Modify: `engine/media/clips.py`
- Test: `tests/test_clips.py`

**Interfaces:**
- Consumes: `clip_count`, `slot_durations` from Task 3; `Clip` from Task 2; `StockVideoMatcherAgent.match(script_segment, duration_seconds, download, output_dir, mock)` returning `StockMatcherResult` with `.matches: list[ClipMatch]`, each having `.query.search_query`, `.video`, `.download_path`, `.error`
- Produces: `beat_clips(agent, beat, target_dir) -> list[Clip]` in `engine.media.clips`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_clips.py`:

```python
from types import SimpleNamespace
from unittest.mock import MagicMock

from engine.contract import Beat
from engine.media.clips import beat_clips


def _beat(seconds=5.0):
    beat = Beat(beat_id="b1", role="hook", voice_text="कुछ",
                caption_text="kuch", target_seconds=seconds,
                visual_prompt="p", motion="zoom_in", transition="fade")
    beat.measured_seconds = seconds
    return beat


def _match(index, path="clip.mp4", error=None, video=True):
    return SimpleNamespace(
        clip_index=index,
        query=SimpleNamespace(search_query=f"query {index}"),
        video=SimpleNamespace(id=100 + index, url="https://pexels/x",
                              user_name="Someone") if video else None,
        download_path=path,
        error=error)


def test_beat_clips_returns_one_clip_per_slot(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1)])
    clips = beat_clips(agent, _beat(5.0), tmp_path)
    assert len(clips) == 2
    assert [c.provider for c in clips] == ["pexels", "pexels"]


def test_beat_clip_durations_sum_to_the_measured_beat(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(i) for i in range(3)])
    beat = _beat(7.3)
    clips = beat_clips(agent, beat, tmp_path)
    assert sum(c.duration for c in clips) == pytest.approx(7.3, abs=1e-9)


def test_beat_clips_uses_measured_seconds_not_target(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    beat = _beat(3.0)
    beat.target_seconds = 99.0          # the model's guess, must be ignored
    beat.measured_seconds = 2.0
    beat_clips(agent, beat, tmp_path)
    assert agent.match.call_args.kwargs["duration_seconds"] == 2.0


def test_a_slot_the_agent_could_not_fill_is_marked_unfilled(tmp_path):
    """The caller falls those back to the image chain; this function only
    reports them honestly rather than dropping the slot."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1, path=None, video=False,
                                   error="no results")])
    clips = beat_clips(agent, _beat(5.0), tmp_path)
    assert len(clips) == 2
    assert clips[1].provider == "unfilled"
    assert clips[1].path == ""


def test_slot_count_wins_when_the_agent_returns_too_few(tmp_path):
    """clip_count is the timeline's contract. If the agent under-delivers
    the missing slots still exist, unfilled."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    clips = beat_clips(agent, _beat(7.5), tmp_path)   # wants 3
    assert len(clips) == 3
    assert [c.provider for c in clips] == ["pexels", "unfilled", "unfilled"]


def test_extra_matches_beyond_the_slot_count_are_discarded(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(i) for i in range(6)])
    clips = beat_clips(agent, _beat(2.4), tmp_path)   # wants 1
    assert len(clips) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_clips.py -q -k beat_clip`
Expected: FAIL with `ImportError: cannot import name 'beat_clips'`

- [ ] **Step 3: Implement**

Add to `engine/media/clips.py`:

```python
from pathlib import Path

from engine.contract import Beat, Clip

PEXELS_LICENCE = "pexels"


def beat_clips(agent, beat: Beat, target_dir: str | Path) -> list[Clip]:
    """Match and download footage for one beat.

    The slot count is the timeline's contract, not a suggestion: if the
    agent returns fewer matches than slots, the missing slots still exist and
    are marked ``unfilled`` for the caller to fall back. Dropping them would
    silently shorten the beat's picture.
    """
    seconds = beat.seconds()
    count = clip_count(seconds)
    durations = slot_durations(seconds, count)

    result = agent.match(script_segment=beat.voice_text,
                         duration_seconds=seconds,
                         download=True,
                         output_dir=str(target_dir))
    matches = list(result.matches)[:count]

    clips: list[Clip] = []
    for index, duration in enumerate(durations):
        match = matches[index] if index < len(matches) else None
        if match is None or not match.download_path or match.video is None:
            clips.append(Clip(path="", query="", provider="unfilled",
                              duration=duration))
            continue
        clips.append(Clip(
            path=str(match.download_path),
            query=match.query.search_query,
            provider="pexels",
            duration=duration,
            source_url=match.video.url,
            pexels_id=match.video.id,
            author=match.video.user_name,
            licence=PEXELS_LICENCE))
    return clips
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_clips.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/media/clips.py tests/test_clips.py
git commit -m "feat: match a beat's clips, and keep unfilled slots visible"
```

---

### Task 5: The plan-level clip stage, with image fallback

**Files:**
- Modify: `engine/media/clips.py`
- Test: `tests/test_clips.py`

**Interfaces:**
- Consumes: `beat_clips` from Task 4; `generate_beat_image(client, beat, out_path, *, seed, model, use_keyless, browser_image_api) -> tuple[str, str]` from `engine.media.images`; `store.save_asset(plan_id, beat_id, kind, provider, path, source_url=None, checksum=None, licence="ai-generated")`
- Produces: `generate_plan_clips(plan, agent, client, work_dir, store=None, *, model=None, use_keyless=True, browser_image_api="", workers=4, progress=None) -> dict[str, int]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_clips.py`:

```python
from engine.media.clips import generate_plan_clips
from tests.factories import make_plan


def test_generate_plan_clips_fills_every_beat(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2)

    assert all(beat.clips for beat in plan.script.beats)
    assert counts["pexels"] == len(plan.script.beats)


def test_an_unfilled_slot_falls_back_to_the_image_chain(tmp_path, monkeypatch):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0, path=None, video=False, error="nothing")])

    calls = []

    def fake_image(client, beat, out_path, **kwargs):
        calls.append(beat.beat_id)
        Path(out_path).write_bytes(b"png")
        return str(out_path), "keyless"

    monkeypatch.setattr("engine.media.clips.generate_beat_image", fake_image)

    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2)

    assert calls, "the image chain was never reached"
    assert counts.get("keyless") == len(plan.script.beats)
    assert all(c.provider == "keyless"
               for beat in plan.script.beats for c in beat.clips)


def test_every_clip_is_recorded_as_an_asset(tmp_path):
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(matches=[_match(0)])
    store = MagicMock()
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        store=store, workers=2)

    assert store.save_asset.call_count == len(plan.script.beats)
    kwargs = store.save_asset.call_args.kwargs
    assert kwargs["licence"] == "pexels"


def test_the_slot_sum_invariant_holds_after_fallback(tmp_path, monkeypatch):
    """Falling back must not change the timeline."""
    agent = MagicMock()
    agent.match.return_value = SimpleNamespace(
        matches=[_match(0), _match(1, path=None, video=False)])

    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))

    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 5.0

    generate_plan_clips(plan, agent, client=None, work_dir=tmp_path,
                        workers=2)

    for beat in plan.script.beats:
        assert sum(c.duration for c in beat.clips) == pytest.approx(
            beat.seconds(), abs=1e-9)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_clips.py -q -k generate_plan`
Expected: FAIL with `ImportError: cannot import name 'generate_plan_clips'`

- [ ] **Step 3: Implement**

Add to `engine/media/clips.py`:

```python
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from engine.contract import ReelPlan
from engine.media.images import generate_beat_image


def _checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]


def generate_plan_clips(plan: ReelPlan, agent, client, work_dir: str | Path,
                        store=None, *, model: str | None = None,
                        use_keyless: bool = True,
                        browser_image_api: str = "",
                        workers: int = 4,
                        progress=None) -> dict[str, int]:
    """Fill ``beat.clips`` for every beat; report provider counts.

    Beats run concurrently for the same reason images do: each is an
    independent network call that spends its time waiting.

    Any slot the agent could not fill is replaced by a still from the image
    chain, which keeps its slot duration. A fallback changes what is on
    screen, never when.
    """
    target_dir = Path(work_dir) / plan.plan_id / "clips"
    target_dir.mkdir(parents=True, exist_ok=True)
    beats = plan.script.beats
    counts: dict[str, int] = {}

    def make(item):
        index, beat = item
        clips = beat_clips(agent, beat, target_dir)
        for slot, clip in enumerate(clips):
            if clip.provider != "unfilled":
                continue
            still = target_dir / f"{beat.beat_id}-{slot}.png"
            path, provider = generate_beat_image(
                client, beat, still, seed=index * 100 + slot, model=model,
                use_keyless=use_keyless,
                browser_image_api=browser_image_api)
            clips[slot] = Clip(path=path, query=beat.visual_prompt,
                               provider=provider, duration=clip.duration,
                               licence="ai-generated")
        return index, beat, clips

    done = 0
    effective_workers = 1 if browser_image_api else max(workers, 1)
    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [pool.submit(make, item) for item in enumerate(beats)]
        for future in as_completed(futures):
            index, beat, clips = future.result()
            beat.clips = clips
            for clip in clips:
                counts[clip.provider] = counts.get(clip.provider, 0) + 1
                if store is not None and clip.path:
                    store.save_asset(
                        plan.plan_id, beat.beat_id, "clip", clip.provider,
                        clip.path, source_url=clip.source_url,
                        checksum=_checksum(clip.path),
                        licence=clip.licence or "ai-generated")
            done += 1
            if progress:
                progress(done, len(beats), beat.beat_id,
                         ",".join(sorted({c.provider for c in clips})))

    return counts
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_clips.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing tests for honest unavailability**

A run with no Pexels key completes and produces a video. That is correct, and
it is also exactly how a broken setup disguises itself as a working one. It
has to say so.

Add to `tests/test_clips.py`:

```python
from engine.media.clips import unavailable_reason


def test_no_pexels_key_is_reported_as_a_reason():
    settings = SimpleNamespace(pexels_api_key="")
    assert "PEXELS_API_KEY" in unavailable_reason(settings)


def test_the_template_placeholder_counts_as_no_key():
    settings = SimpleNamespace(pexels_api_key="your_pexels_api_key_here")
    assert "PEXELS_API_KEY" in unavailable_reason(settings)


def test_a_real_key_has_no_reason():
    settings = SimpleNamespace(pexels_api_key="x" * 40)
    assert unavailable_reason(settings) is None


def test_the_stage_skips_the_agent_entirely_without_a_key(tmp_path,
                                                          monkeypatch):
    agent = MagicMock()
    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=2,
                                 reason="PEXELS_API_KEY is not set")

    agent.match.assert_not_called()
    assert "pexels" not in counts
    assert all(sum(c.duration for c in b.clips) == pytest.approx(
        b.seconds(), abs=1e-9) for b in plan.script.beats)


def test_a_rate_limit_is_surfaced_not_retried(tmp_path, monkeypatch):
    """Pexels free tier is 200 requests/hour. Hammering it turns a slow
    path into a banned one."""
    agent = MagicMock()
    agent.match.side_effect = RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(
        "engine.media.clips.generate_beat_image",
        lambda client, beat, out_path, **kw: (str(out_path), "placeholder"))
    plan = make_plan()
    for beat in plan.script.beats:
        beat.measured_seconds = 2.0

    counts = generate_plan_clips(plan, agent, client=None,
                                 work_dir=tmp_path, workers=1)

    assert agent.match.call_count == len(plan.script.beats), \
        "one attempt per beat, no retry storm"
    assert counts.get("placeholder") == len(plan.script.beats)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `python -m pytest tests/test_clips.py -q -k "unavailable or rate_limit or skips_the_agent"`
Expected: FAIL with `ImportError: cannot import name 'unavailable_reason'`

- [ ] **Step 7: Implement the reason check and the failure path**

Add to `engine/media/clips.py`:

```python
# What .env.example ships with. Treating it as a key produces a 401 on
# every beat and a confusing run; it is the same thing as no key.
KEY_PLACEHOLDER = "your_pexels_api_key_here"


def unavailable_reason(settings) -> str | None:
    """Why stock footage cannot be fetched, or None if it can.

    Returned rather than raised: a run without a key should still produce a
    video from the fallback chain. It just must not look like a run that
    got footage.
    """
    key = (getattr(settings, "pexels_api_key", "") or "").strip()
    if not key or key == KEY_PLACEHOLDER:
        return ("PEXELS_API_KEY is not set, so every scene falls back to "
                "the image chain. Add it to .env — registration is free.")
    return None
```

Change the `generate_plan_clips` signature to accept the reason, and make
`make` tolerate a failing agent:

```python
def generate_plan_clips(plan: ReelPlan, agent, client, work_dir: str | Path,
                        store=None, *, model: str | None = None,
                        use_keyless: bool = True,
                        browser_image_api: str = "",
                        workers: int = 4,
                        reason: str | None = None,
                        progress=None) -> dict[str, int]:
```

and inside `make`, replace the `clips = beat_clips(agent, beat, target_dir)`
line with:

```python
        if reason:
            clips = [Clip(path="", query="", provider="unfilled",
                          duration=d)
                     for d in slot_durations(beat.seconds(),
                                             clip_count(beat.seconds()))]
        else:
            try:
                clips = beat_clips(agent, beat, target_dir)
            except Exception as exc:
                # One attempt per beat. A rate limit answered with retries
                # turns a slow path into a banned one.
                print(f"[clips] {beat.beat_id} fell back: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                clips = [Clip(path="", query="", provider="unfilled",
                              duration=d)
                         for d in slot_durations(beat.seconds(),
                                                 clip_count(beat.seconds()))]
```

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_clips.py -q`
Expected: PASS

- [ ] **Step 9: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass. Nothing calls this yet.

- [ ] **Step 10: Commit**

```bash
git add engine/media/clips.py tests/test_clips.py
git commit -m "feat: the clip stage, with the image chain as its fallback"
```

---

### Task 6: Render clips instead of images

The filter graph emits one segment per clip rather than one per beat. Beats stay the unit that owns the timeline.

**Files:**
- Modify: `engine/assembly/render.py:127-211` (`build_filter_graph`), `engine/assembly/render.py:212-259` (`build_command`)
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `Beat.clips` from Task 2; `segment_lengths(durations, transition_duration) -> (lengths, offsets, overlaps)` unchanged
- Produces: `build_filter_graph` and `build_command` handling clips; `plan_inputs(plan) -> list[tuple[str, bool]]` returning `(path, is_video)` per input in order

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_render.py`:

```python
from engine.assembly.render import build_filter_graph, plan_inputs


def _clipped(plan, per_beat=2):
    """Give every beat `per_beat` video clips filling its measured span."""
    from engine.contract import Clip
    from engine.media.clips import slot_durations
    for beat in plan.script.beats:
        beat.measured_seconds = beat.measured_seconds or 4.0
        for i, d in enumerate(slot_durations(beat.seconds(), per_beat)):
            beat.clips.append(Clip(path=f"{beat.beat_id}-{i}.mp4", query="q",
                                   provider="pexels", duration=d))
    return plan


def test_one_input_per_clip_not_per_beat():
    plan = _clipped(make_plan(), per_beat=3)
    inputs = plan_inputs(plan)
    assert len(inputs) == 3 * len(plan.script.beats)
    assert all(is_video for _, is_video in inputs)


def test_a_still_fallback_input_is_flagged_as_not_video():
    plan = _clipped(make_plan(), per_beat=2)
    plan.script.beats[0].clips[0].path = "still.png"
    plan.script.beats[0].clips[0].provider = "placeholder"
    inputs = plan_inputs(plan)
    assert inputs[0] == ("still.png", False)
    assert inputs[1][1] is True


def test_video_inputs_drop_their_audio():
    plan = _clipped(make_plan(), per_beat=2)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    narration = [p for p in graph.split(";") if "concat=n=" in p]
    assert len(narration) == 1, "only the narration concat may build audio"
    assert "a=1" in narration[0]


def test_no_zoompan_on_a_video_clip():
    plan = _clipped(make_plan(), per_beat=2)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert "zoompan" not in graph


def test_zoompan_survives_on_a_still_fallback():
    plan = _clipped(make_plan(), per_beat=2)
    plan.script.beats[0].clips[0].path = "still.png"
    plan.script.beats[0].clips[0].provider = "placeholder"
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert graph.count("zoompan") == 1


def test_xfade_count_matches_beat_joins_not_clip_joins():
    """Hard cuts inside a beat; crossfade only where beats meet."""
    plan = _clipped(make_plan(), per_beat=3)
    graph, _, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert graph.count("xfade=") == len(plan.script.beats) - 1


def test_total_runtime_still_equals_the_narration():
    plan = _clipped(make_plan(), per_beat=4)
    _, total, _ = build_filter_graph(plan, audio_offset=len(plan_inputs(plan)))
    assert total == pytest.approx(
        sum(b.seconds() for b in plan.script.beats), abs=1e-6)


def test_a_beat_with_no_clips_still_renders_from_its_image():
    """Backwards compatibility: an older stored plan has image_path only."""
    plan = make_plan()
    for beat in plan.script.beats:
        beat.image_path = f"{beat.beat_id}.png"
    inputs = plan_inputs(plan)
    assert len(inputs) == len(plan.script.beats)
    assert all(is_video is False for _, is_video in inputs)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_render.py -q -k "clip or xfade_count or zoompan or plan_inputs"`
Expected: FAIL with `ImportError: cannot import name 'plan_inputs'`

- [ ] **Step 3: Add the input enumeration**

In `engine/assembly/render.py`, after `segment_lengths`:

```python
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def plan_inputs(plan: ReelPlan) -> list[tuple[str, bool]]:
    """Every visual input in render order, as ``(path, is_video)``.

    A beat with no clips falls back to its ``image_path``, so plans stored
    before clips existed still render.
    """
    inputs: list[tuple[str, bool]] = []
    for beat in plan.script.beats:
        if beat.clips:
            for clip in beat.clips:
                inputs.append((clip.path,
                               Path(clip.path).suffix.lower()
                               in VIDEO_SUFFIXES))
        else:
            inputs.append((beat.image_path or "", False))
    return inputs
```

Add `from pathlib import Path` to the imports if it is not already there.

- [ ] **Step 4: Rewrite the video half of `build_filter_graph`**

Replace the `--- per-beat video segments ---` and `--- chain them with xfade ---` blocks with:

```python
    # --- per-clip video segments, grouped by beat ------------------------
    #
    # Beats own the timeline; clips subdivide the span a beat already holds.
    # `lengths[i]` is the beat's segment *including* its half-overlap
    # padding, so the clip slots are scaled into it proportionally rather
    # than using their stored durations directly — the stored values are the
    # narration-timeline slots, which is what the invariant is asserted on.
    inputs = plan_inputs(plan)
    beat_labels: list[str] = []
    cursor = 0

    for beat_index, beat in enumerate(beats):
        slots = ([clip.duration for clip in beat.clips]
                 if beat.clips else [durations[beat_index]])
        scale_factor = lengths[beat_index] / sum(slots)
        clip_labels: list[str] = []

        for slot_index, slot in enumerate(slots):
            path, is_video = inputs[cursor]
            span = slot * scale_factor
            label = f"c{cursor}"
            common = (f"scale={width}:{height}:"
                      f"force_original_aspect_ratio=increase,"
                      f"crop={width}:{height},setsar=1,fps={fps}")
            if is_video:
                # Fit the source to its slot: trim if longer, loop if
                # shorter. The source's own length never moves the timeline.
                parts.append(
                    f"[{cursor}:v]{common},"
                    f"loop=loop=-1:size=32767:start=0,"
                    f"trim=duration={span:.3f},setpts=PTS-STARTPTS,"
                    f"format=yuv420p[{label}]")
            else:
                parts.append(
                    f"[{cursor}:v]{common},"
                    f"{zoompan_expr(beat.motion, span, fps, width, height)},"
                    f"format=yuv420p[{label}]")
            clip_labels.append(label)
            cursor += 1

        # Hard cuts inside the beat: a plain concat, no overlap to pay for.
        if len(clip_labels) == 1:
            beat_labels.append(clip_labels[0])
        else:
            joined = "".join(f"[{c}]" for c in clip_labels)
            parts.append(f"{joined}concat=n={len(clip_labels)}:v=1:a=0"
                         f"[b{beat_index}]")
            beat_labels.append(f"b{beat_index}")

    # --- crossfade between beats -----------------------------------------
    if len(beats) == 1:
        video_label = beat_labels[0]
    else:
        current = beat_labels[0]
        for index in range(1, len(beats)):
            parts.append(
                f"[{current}][{beat_labels[index]}]xfade="
                f"transition={_xfade_name(beats[index].transition)}:"
                f"duration={overlaps[index - 1]:.3f}:"
                f"offset={offsets[index - 1]:.3f}[x{index}]")
            current = f"x{index}"
        video_label = current
```

- [ ] **Step 5: Update `build_command` to feed clips**

In `build_command`, replace the image input loop:

```python
    for beat in beats:
        command += ["-i", str(Path(beat.image_path).resolve())]
```

with:

```python
    # Both kinds are plain inputs. A still is still supplied as exactly one
    # frame — no -loop — because zoompan's `d` counts output frames per
    # input frame, so a looped still would multiply its segment length.
    # Video is fitted to its slot inside the graph instead.
    inputs = plan_inputs(plan)
    for path, _is_video in inputs:
        command += ["-i", str(Path(path).resolve())]
```

and change the audio offset from `len(beats)` to `len(inputs)` in both the `music_index` calculation and the `build_filter_graph` call:

```python
    music_index = None
    if music_path and Path(music_path).exists():
        music_index = len(inputs) + len(beats)
        command += ["-stream_loop", "-1", "-i",
                    str(Path(music_path).resolve())]
    ...
    graph, total, video_label = build_filter_graph(
        plan, fps=settings.fps, width=settings.width, height=settings.height,
        transition_duration=settings.transition_duration, ass_path=ass_name,
        audio_offset=len(inputs), music_index=music_index)
```

- [ ] **Step 6: Run the render tests**

Run: `python -m pytest tests/test_render.py -q`
Expected: PASS, including the pre-existing A/V sync tests.

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add engine/assembly/render.py tests/test_render.py
git commit -m "feat: render one segment per clip, cutting hard inside a beat"
```

---

### Task 7: Wire the stage in, after voice

**Files:**
- Modify: `engine/pipeline.py:40` (`Stage.IMAGES` → `Stage.CLIPS`), `engine/pipeline.py:46-49` (stage order), `engine/pipeline.py:230-250` (the produce body), `engine/config.py` (add `pexels_api_key`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `generate_plan_clips` from Task 5
- Produces: `Stage.CLIPS = "clips"`; `Settings.pexels_api_key: str`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline.py`:

```python
from engine.pipeline import Stage


def test_clips_run_after_voice_because_they_need_measured_duration():
    """Clip count is a function of beat length, and beat length only exists
    once synthesis has written measured_seconds."""
    order = list(Stage.ORDER)
    assert order.index(Stage.VOICE) < order.index(Stage.CLIPS)


def test_the_images_stage_is_gone():
    assert not hasattr(Stage, "IMAGES")
    assert "images" not in Stage.ORDER


def test_produce_order_starts_with_voice():
    assert Stage.PRODUCE[0] == Stage.VOICE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_pipeline.py -q -k "clips or images_stage or produce_order"`
Expected: FAIL with `AttributeError: type object 'Stage' has no attribute 'CLIPS'`

- [ ] **Step 3: Rename the stage and reorder**

In `engine/pipeline.py`, replace `IMAGES = "images"` with:

```python
    CLIPS = "clips"
```

and the two tuples with:

```python
    ORDER = (RESEARCH, HOOKS, SCRIPT, METADATA, MODERATION, DEDUP,
             VOICE, CLIPS, CAPTIONS, RENDER, QC)

    PRODUCE = (VOICE, CLIPS, CAPTIONS, RENDER, QC)
```

- [ ] **Step 4: Swap the produce body**

In `produce_stage`, move the voice block above the visuals block, and replace the images block with:

```python
    reason = unavailable_reason(settings)
    emit(PipelineEvent(Stage.CLIPS, "started",
                       f"{len(plan.script.beats)} scenes"))
    if reason:
        # Surfaced as its own event, not buried in the provider counts. A
        # run with no key still produces a video, which is exactly how a
        # broken setup passes for a working one.
        emit(PipelineEvent(Stage.CLIPS, "info", reason))
    agent = StockVideoMatcherAgent(
        omniroute_base_url=settings.omniroute_base,
        omniroute_api_key=settings.omniroute_key,
        omniroute_model=settings.model_cheap or None,
        pexels_api_key=settings.pexels_api_key)
    counts = generate_plan_clips(
        plan, agent, client, settings.work_dir, store,
        model=settings.model_image or None,
        use_keyless=settings.keyless_images,
        browser_image_api=settings.browser_image_api,
        workers=settings.image_workers,
        reason=reason,
        progress=lambda i, n, beat, provider: emit(PipelineEvent(
            Stage.CLIPS, "info", f"{i}/{n} {beat} via {provider}")))
    emit(PipelineEvent(Stage.CLIPS, "done", ", ".join(
        f"{k}:{v}" for k, v in counts.items()), {"providers": counts}))
```

Change the import at `engine/pipeline.py:29` from:

```python
from engine.media.images import generate_plan_images
```

to:

```python
from engine.media.clips import generate_plan_clips, unavailable_reason
from stock_agent import StockVideoMatcherAgent
```

- [ ] **Step 5: Add the setting**

In `engine/config.py`, beside the other keys:

```python
    pexels_api_key: str = field(
        default_factory=lambda: os.getenv("PEXELS_API_KEY", "").strip())
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest -q`
Expected: all pass. Any test referring to `Stage.IMAGES` or `image_providers` must be updated in the same commit, not deleted.

- [ ] **Step 7: Commit**

```bash
git add engine/pipeline.py engine/config.py tests/test_pipeline.py
git commit -m "feat: clips replace images as the visual stage, after voice"
```

---

### Task 8: Show clips in the panel

**Files:**
- Modify: `engine/ui/index.html:505-540`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Beat.clips` from Task 2; the `providers` payload from Task 7
- Produces: no new Python interface

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
def test_the_panel_reports_clip_providers(client):
    """A run that fell back to stills must not look like a run that got
    footage. The old strip only knew about image_provider."""
    page = client.get("/").text
    assert "clips" in page
    assert "image_provider" not in page
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_app.py -q -k clip_providers`
Expected: FAIL — `image_provider` is still in the page.

- [ ] **Step 3: Update the scene strip**

In `engine/ui/index.html`, replace the beat filter and the placeholder count:

```javascript
  const beats = plan.script.beats.filter((b) => b.clips && b.clips.length);
  const fellBack = beats.reduce((n, b) => n + b.clips.filter(
    (c) => c.provider !== "pexels").length, 0);
```

and the figure caption:

```javascript
      <figcaption class="${b.clips.some((c) => c.provider !== 'pexels') ? 'ph' : ''}">
        ${b.beat_id} ${b.clips.length} clip${b.clips.length === 1 ? '' : 's'}
        ${b.clips.every((c) => c.provider === 'pexels') ? '' : '· fallback'}
      </figcaption>
```

and the scene summary row:

```javascript
    <dt>Scenes</dt><dd>${Object.entries(result.providers || {})
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_app.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/ui/index.html tests/test_app.py
git commit -m "feat: the scene strip reports clips and fallbacks"
```

---

### Task 9: Prove it end to end

Everything so far is unit-level. This task renders a real video and measures it.

**Files:**
- Modify: `scripts/verify_e2e.py`
- Test: manual, plus the existing scorecard

**Interfaces:**
- Consumes: everything above
- Produces: a verified MP4

- [ ] **Step 1: Update the verify script's summary**

In `scripts/verify_e2e.py`, change the providers line from:

```python
    print(f"providers  : {result['image_providers']}")
```

to:

```python
    print(f"providers  : {result.get('providers', {})}")
```

- [ ] **Step 2: Run the full pipeline**

Run: `python scripts/verify_e2e.py --topic "Barabar caves ke andar ki polish"`
Expected: exit code 0, an MP4 in `outputs/`, QC pass.

- [ ] **Step 3: Confirm the timeline did not drift**

```bash
python -c "
import subprocess, re, json, sys
from engine.config import Settings
from engine.store import Store
s = Settings(); st = Store(s.db_path)
import glob, os
mp4 = max(glob.glob('outputs/*.mp4'), key=os.path.getmtime)
r = subprocess.run([s.ffmpeg, '-hide_banner', '-i', mp4],
                   capture_output=True, text=True)
m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', r.stderr)
video = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
print(f'video    : {video:.2f}s')
print(f'file     : {mp4}')
"
```
Expected: the printed duration matches the narration total the run reported, within 0.05s.

- [ ] **Step 4: Watch it**

Open the MP4. Confirm by eye: clips move, cuts land on the beat, no frozen frames, no Pexels audio bleeding through, captions still aligned.

This step has no automated equivalent. The spec flags visual coherence as the risk that only eyes can judge.

- [ ] **Step 5: Run the whole suite one more time**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_e2e.py
git commit -m "chore: verify script reports clip providers"
```

---

### Task 10: Update the docs that now lie

**Files:**
- Modify: `README.md`, `SETUP.md`, `docs/omniroute-setup.md`
- Test: none

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1: Fix the README diagram**

In `README.md`, in the ASCII diagram, replace:

```
      image chain ─────────────────┤  gateway -> keyless -> placeholder
```

with:

```
      clip chain ──────────────────┤  pexels -> image -> placeholder
```

and replace `local edge-tts` on the line above with `local Piper (hi-IN)`,
which has been wrong since the voice engine changed.

- [ ] **Step 2: Replace the README's images section**

Replace the whole paragraph beginning **"Images walk a three-tier chain"**
with:

```markdown
**Visuals are stock footage, in a three-tier chain**, in
`engine/media/clips.py`: Pexels first, then the still-image chain in
`images.py`, then a generated placeholder. One clip per 2.5 seconds of
narration, matched per beat so a clip never straddles a beat boundary —
that constraint is what keeps the A/V sync work intact, because the clip
layer only subdivides a span the beat already owns. Clips hard-cut inside a
beat and crossfade only where beats meet, which is what fast-cut pacing
actually looks like. A slot Pexels cannot fill becomes a still with
zoompan, keeping its slot duration: a fallback changes what is on screen,
never when. `PEXELS_API_KEY` in `.env` is what switches tier 1 on.
```

- [ ] **Step 3: Correct the README's stale facts**

In the "What is verified" table, change `175 tests` to the count
`python -m pytest -q` actually reports, and change the Hindi TTS row from
`hi-IN-MadhurNeural at rate=-8% pitch=-6Hz` to
`Piper pratham at length_scale 1.12`. Change the "Scene images" row to
describe the clip run this plan's Task 9 produced. Delete the
`edge-tts commercial terms` row from the "Not verified" table — Piper is
MIT-licensed and that question is retired — and replace it with a row for
the Pexels licence terms.

- [ ] **Step 4: Add the key to SETUP.md**

In the configuration table in `SETUP.md`, add a row:

```markdown
| `PEXELS_API_KEY` | _(none)_ | stock footage for scenes; free to register, and without it every scene falls back to a still |
```

- [ ] **Step 5: Rewrite the SETUP.md known gap**

Replace the known-gap entry beginning **"Images are the weakest part"**
with:

```markdown
**Visuals depend on a free Pexels key.** The chain is Pexels → the
still-image chain → a placeholder frame. Without `PEXELS_API_KEY` the
pipeline still produces a video, but every scene is a still, and the
panel's scene strip will say `fallback` on each one. The old image path is
still there underneath and still carries its own limits: the gateway tier
exhausted its quota on 2026-09-18 with a 163-hour reset, and the free
keyless endpoint watermarks and upscales.
```

- [ ] **Step 3: Run the suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add README.md SETUP.md docs/omniroute-setup.md
git commit -m "docs: visuals are stock footage now, images are the fallback"
```

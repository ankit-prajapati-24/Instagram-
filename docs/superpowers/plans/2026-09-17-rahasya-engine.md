# Rahasya Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one Hinglish mystery topic into a QC-passed 45-second vertical MP4 with a local web UI to run and inspect every stage.

**Architecture:** A linear pipeline behind a FastAPI app. OmniRoute (`localhost:20128/v1`) is the only LLM/image/embedding/moderation dependency. Voice is local `edge-tts`. Video assembly and caption burn-in are direct `ffmpeg` filter graphs — no moviepy. SQLite stores plans, assets, costs and dedup vectors. A human gate sits between script generation and render; nothing publishes automatically.

**Tech Stack:** Python 3.14, FastAPI + uvicorn, httpx, pydantic v2, edge-tts, ffmpeg v7.1 (bundled via imageio-ffmpeg; has libass/libharfbuzz/fontconfig), SQLite (stdlib), vanilla HTML/CSS/JS for the UI.

**Spec:** `docs/superpowers/specs/2026-09-17-rahasya-engine-design.md`

## Global Constraints

- OmniRoute base URL: `http://localhost:20128/v1`. Every LLM, image, embedding and moderation call goes through it. No direct provider SDKs.
- Voice: local `edge-tts`, voice `hi-IN-MadhurNeural`, `rate=-8%`, `pitch=-6Hz`. Never OmniRoute `/v1/audio/speech` for Hindi.
- Captions burn from `caption_text` (Roman Hinglish) by default; `--captions=devanagari` switches to `voice_text`.
- Frame size is always `1080x1920`. Target duration 45s, QC-valid range 38–52s.
- Every beat carries both `voice_text` (Devanagari) and `caption_text` (Roman Hinglish).
- Beat durations come from **measured TTS audio**, never from the model's `target_seconds`.
- Dedup thresholds: trigram `0.6`, embedding cosine `0.88`, entity cooldown `45` days.
- No function in this codebase may be called by a scheduler to publish. Publishing builders return payloads only.
- Cost is recorded from `X-OmniRoute-Response-Cost`, `X-OmniRoute-Provider`, `X-OmniRoute-Fallback-Attempts` response headers on every call.
- ffmpeg binary is resolved via `imageio_ffmpeg.get_ffmpeg_exe()`, never a bare `ffmpeg` on PATH.
- Banned phrase list (QC hard-fail): `aaj hum baat karenge`, `kya aap jaante hain`, `chaliye shuru karte hain`, `doston`, `aap ko jaan kar hairani hogi`, `iske baare mein aapka kya khayal hai`.

---

## File Structure

| File | Responsibility |
|---|---|
| `engine/config.py` | Settings from env with defaults; ffmpeg path resolution |
| `engine/contract.py` | Pydantic models for ReelPlan and every nested object |
| `engine/omniroute.py` | HTTP client: chat/images/embeddings/moderations + cost telemetry capture |
| `engine/store.py` | SQLite schema creation and all reads/writes |
| `engine/agents/research.py` | Agent 1 — claims with source URLs |
| `engine/agents/hooks.py` | Agent 2 — 5 hook variants |
| `engine/agents/script.py` | Agent 3 — beats with dual text + visual prompts |
| `engine/agents/metadata.py` | Agent 4 — titles, captions, pinned comment, hashtags |
| `engine/prompts/*.txt` | One prompt file per agent |
| `engine/gates/dedup.py` | Four-layer dedup |
| `engine/gates/qc.py` | Scorecard |
| `engine/media/voice.py` | edge-tts synthesis + word boundary capture |
| `engine/media/images.py` | OmniRoute image generation + local placeholder fallback |
| `engine/assembly/captions.py` | ReelPlan + word timings → `.ass` with karaoke tags |
| `engine/assembly/render.py` | ffmpeg filter graph: zoompan + xfade + subtitles + loudnorm |
| `engine/assembly/compile.py` | ReelPlan → body for the existing `POST /generate-video` |
| `engine/publish/payloads.py` | YouTube + Instagram payload builders (never invoked automatically) |
| `engine/pipeline.py` | Stage orchestration with progress events |
| `engine/app.py` | FastAPI app, REST endpoints, SSE progress stream |
| `engine/ui/index.html` | Single-page dark UI |
| `tests/*` | One test module per unit above |

---

## Task 1: Config and OmniRoute client

**Files:**
- Create: `engine/config.py`, `engine/omniroute.py`
- Test: `tests/test_omniroute.py`

**Interfaces:**
- Produces: `Settings` dataclass with `omniroute_base`, `omniroute_key`, `ffmpeg`, `voice`, `rate`, `pitch`, `work_dir`, `out_dir`, `db_path`, `daily_usd_ceiling`. `OmniRouteClient` with `chat(messages, model=None, json_schema=None) -> ChatResult`, `image(prompt, size='1024x1792') -> bytes`, `embed(texts) -> list[list[float]]`, `moderate(text) -> ModerationResult`, and `.total_usd` / `.calls` accumulators. `CostRecord` dataclass carrying `usd`, `provider`, `model`, `fallback_attempts`, `latency_ms`, `cache_hit`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_omniroute.py
import json
import httpx
import pytest
from engine.omniroute import OmniRouteClient, CostRecord


def _client(handler):
    transport = httpx.MockTransport(handler)
    return OmniRouteClient(base="http://localhost:20128/v1", key="k",
                           transport=transport)


def test_chat_returns_text_and_records_cost():
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
        }, headers={
            "X-OmniRoute-Response-Cost": "0.0000123400",
            "X-OmniRoute-Provider": "groq",
            "X-OmniRoute-Model": "llama-3.3-70b",
            "X-OmniRoute-Fallback-Attempts": "2",
            "X-OmniRoute-Latency-Ms": "812",
        })

    c = _client(handler)
    result = c.chat([{"role": "user", "content": "hi"}])
    assert result.text == "hello"
    assert result.cost.usd == pytest.approx(0.00001234)
    assert result.cost.provider == "groq"
    assert result.cost.fallback_attempts == 2
    assert c.total_usd == pytest.approx(0.00001234)


def test_chat_extracts_json_from_fenced_block():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "```json\n{\"a\": 1}\n```"}}]})

    c = _client(handler)
    assert c.chat([], want_json=True).data == {"a": 1}


def test_missing_cost_headers_default_to_zero():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}}]})

    c = _client(handler)
    r = c.chat([])
    assert r.cost.usd == 0.0
    assert r.cost.fallback_attempts == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_omniroute.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.omniroute'`

- [ ] **Step 3: Write minimal implementation**

`engine/config.py` reads env vars with the Global Constraints values as defaults and resolves ffmpeg through `imageio_ffmpeg.get_ffmpeg_exe()`.

`engine/omniroute.py` implements `CostRecord.from_headers(headers)` parsing the `X-OmniRoute-*` set with `0` defaults, `ChatResult(text, data, cost, raw)`, and `OmniRouteClient` holding an `httpx.Client` (accepting an injected `transport` for tests). `chat()` posts to `/chat/completions`; when `want_json=True` it strips ``` fences and slices from the first `{` to the last `}` before `json.loads`. `image()` posts to `/images/generations` and returns decoded `b64_json` bytes, falling back to fetching `data[0].url`. `embed()` posts to `/embeddings`. `moderate()` posts to `/moderations`. Every method appends its `CostRecord` to `self.calls` and adds to `self.total_usd`. Retries: 5 attempts with exponential backoff and jitter on 429/5xx/timeout.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_omniroute.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/config.py engine/omniroute.py tests/test_omniroute.py
git commit -m "feat: OmniRoute client with cost telemetry capture"
```

---

## Task 2: ReelPlan contract

**Files:**
- Create: `engine/contract.py`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Topic`, `Hook`, `Beat`, `Script`, `Metadata`, `Claim`, `Provenance`, `Safety`, `Cost`, `ReelPlan`. `Beat` fields: `beat_id: str`, `role: Literal[...]`, `voice_text: str`, `caption_text: str`, `on_screen_text: str | None`, `target_seconds: float`, `visual_prompt: str`, `motion: Literal['zoom_in','zoom_out','move_left','move_right']`, `transition: Literal['fade','slide_left','slide_right','zoom','blur']`, `image_path: str | None = None`, `audio_path: str | None = None`, `measured_seconds: float | None = None`, `words: list[WordTiming] = []`. `WordTiming` fields: `word: str`, `start: float`, `end: float`. `ReelPlan.duration()` returns the sum of `measured_seconds` when all are set, else the sum of `target_seconds`. `Topic.make(raw)` classmethod computes `slug` and `dedupe_hash`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract.py
import pytest
from pydantic import ValidationError
from engine.contract import Topic, Beat, ReelPlan, Script, Hook


def test_topic_make_normalises_slug_and_hashes():
    t = Topic.make("  Roopkund Lake ke 800 SAAL purane Kankaal!! ")
    assert t.slug == "roopkund-lake-ke-800-saal-purane-kankaal"
    assert len(t.dedupe_hash) == 64
    assert Topic.make("roopkund lake ke 800 saal purane kankaal").dedupe_hash \
        == t.dedupe_hash


def test_beat_rejects_unknown_motion():
    with pytest.raises(ValidationError):
        Beat(beat_id="b1", role="hook", voice_text="a", caption_text="a",
             on_screen_text=None, target_seconds=3.0, visual_prompt="p",
             motion="spin", transition="fade")


def test_duration_prefers_measured_over_target():
    def beat(i, target, measured=None):
        return Beat(beat_id=f"b{i}", role="setup", voice_text="v",
                    caption_text="c", on_screen_text=None,
                    target_seconds=target, visual_prompt="p",
                    motion="zoom_in", transition="fade",
                    measured_seconds=measured)

    hook = Hook(variant_id="h1", voice_text="v", caption_text="c",
                style="question", seconds=3.0)
    plan = ReelPlan(plan_id="p1", topic=Topic.make("x"), hooks=[hook],
                    script=Script(total_seconds=45.0, chosen_hook="h1",
                                  beats=[beat(1, 5.0, 4.0), beat(2, 5.0, 6.5)]))
    assert plan.duration() == pytest.approx(10.5)

    plan.script.beats[1].measured_seconds = None
    assert plan.duration() == pytest.approx(10.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.contract'`

- [ ] **Step 3: Write minimal implementation**

Pydantic v2 `BaseModel` classes exactly as listed in Interfaces. `Topic.make` lowercases, strips non-alphanumerics to single hyphens, trims hyphens, then `sha256` of the slug. `ReelPlan` has `schema_version: str = "1.0"`, `metadata: Metadata | None = None`, `provenance: Provenance = Provenance()`, `safety: Safety = Safety()`, `cost: Cost = Cost()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contract.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/contract.py tests/test_contract.py
git commit -m "feat: ReelPlan contract models"
```

---

## Task 3: SQLite store

**Files:**
- Create: `engine/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `ReelPlan`, `CostRecord` from Tasks 1–2.
- Produces: `Store(db_path)` with `init()`, `save_plan(plan, status)`, `get_plan(plan_id) -> ReelPlan | None`, `list_plans(limit) -> list[dict]`, `set_status(plan_id, status)`, `record_cost(plan_id, stage, cost_record)`, `plan_cost(plan_id) -> float`, `save_asset(plan_id, beat_id, kind, provider, path, source_url, checksum)`, `save_embedding(plan_id, vector)`, `recent_embeddings(limit) -> list[tuple[str, list[float]]]`, `published_slugs(limit) -> list[str]`, `entity_last_seen(entity) -> datetime | None`, `record_entities(plan_id, entities)`, `today_usd() -> float`. Tables: `topics`, `plans`, `claims`, `assets`, `renders`, `publications`, `metrics_daily`, `costs`, `jobs`, `embeddings`, `entities`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import pytest
from engine.store import Store
from engine.contract import Topic, Hook, Beat, Script, ReelPlan
from engine.omniroute import CostRecord


def _plan(pid="p1", raw="roopkund skeletons"):
    hook = Hook(variant_id="h1", voice_text="v", caption_text="c",
                style="question", seconds=3.0)
    beat = Beat(beat_id="b1", role="hook", voice_text="v", caption_text="c",
                on_screen_text=None, target_seconds=3.0, visual_prompt="p",
                motion="zoom_in", transition="fade")
    return ReelPlan(plan_id=pid, topic=Topic.make(raw), hooks=[hook],
                    script=Script(total_seconds=45.0, chosen_hook="h1",
                                  beats=[beat]))


def test_roundtrip_plan(tmp_path):
    s = Store(tmp_path / "t.db"); s.init()
    s.save_plan(_plan(), status="draft")
    got = s.get_plan("p1")
    assert got.topic.slug == "roopkund-skeletons"
    assert got.script.beats[0].beat_id == "b1"


def test_costs_accumulate_per_plan(tmp_path):
    s = Store(tmp_path / "t.db"); s.init()
    s.save_plan(_plan(), status="draft")
    s.record_cost("p1", "script", CostRecord(usd=0.01, provider="groq",
                  model="m", fallback_attempts=0, latency_ms=1, cache_hit=False))
    s.record_cost("p1", "images", CostRecord(usd=0.02, provider="together",
                  model="flux", fallback_attempts=1, latency_ms=1, cache_hit=False))
    assert s.plan_cost("p1") == pytest.approx(0.03)
    assert s.today_usd() == pytest.approx(0.03)


def test_entity_cooldown_lookup(tmp_path):
    s = Store(tmp_path / "t.db"); s.init()
    s.save_plan(_plan(), status="draft")
    assert s.entity_last_seen("Roopkund") is None
    s.record_entities("p1", ["Roopkund", "Uttarakhand"])
    assert s.entity_last_seen("Roopkund") is not None
    assert s.entity_last_seen("roopkund") is not None  # case-insensitive
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.store'`

- [ ] **Step 3: Write minimal implementation**

`sqlite3` with `row_factory = sqlite3.Row`. `init()` runs `CREATE TABLE IF NOT EXISTS` for all eleven tables. `plans.plan_json` stores `plan.model_dump_json()`; `get_plan` revives with `ReelPlan.model_validate_json`. `embeddings.vector` stores JSON floats. `entities.entity` is stored lowercased so lookups are case-insensitive. `today_usd()` sums `costs.usd` where `date(at) = date('now')`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/store.py tests/test_store.py
git commit -m "feat: SQLite store with cost and entity tracking"
```

---

## Task 4: Dedup gate

**Files:**
- Create: `engine/gates/dedup.py`
- Test: `tests/test_dedup.py`

**Interfaces:**
- Consumes: `Store`, `OmniRouteClient`, `Topic`.
- Produces: `DedupResult(passed: bool, layer: str | None, detail: str)`, `trigram_similarity(a, b) -> float`, `cosine(a, b) -> float`, `check(topic, store, client, embed_text) -> DedupResult`. Layer names: `"exact"`, `"trigram"`, `"semantic"`, `"cooldown"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dedup.py
import pytest
from engine.gates.dedup import trigram_similarity, cosine


def test_trigram_identical_is_one():
    assert trigram_similarity("roopkund skeletons",
                              "roopkund skeletons") == pytest.approx(1.0)


def test_trigram_reworded_is_high():
    assert trigram_similarity("roopkund lake skeletons",
                              "skeletons of roopkund lake") > 0.6


def test_trigram_unrelated_is_low():
    assert trigram_similarity("roopkund skeletons",
                              "bhangarh fort curse") < 0.3


def test_cosine_orthogonal_and_identical():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dedup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.gates.dedup'`

- [ ] **Step 3: Write minimal implementation**

`trigram_similarity` builds padded 3-gram sets from lowercased alphanumeric-normalised strings and returns Jaccard overlap. `cosine` is the dot product over the norm product, returning `0.0` when either norm is zero. `check()` runs the four layers in order and returns the first failure: exact `dedupe_hash` match in `plans`; any `trigram_similarity > 0.6` against `store.published_slugs(400)`; any `cosine > 0.88` against `store.recent_embeddings(400)` using `client.embed([embed_text])`; any entity whose `entity_last_seen` is within 45 days.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dedup.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add engine/gates/dedup.py tests/test_dedup.py
git commit -m "feat: four-layer dedup gate"
```

---

## Task 5: Agents and prompts

**Files:**
- Create: `engine/agents/research.py`, `engine/agents/hooks.py`, `engine/agents/script.py`, `engine/agents/metadata.py`, `engine/prompts/research.txt`, `engine/prompts/hooks.txt`, `engine/prompts/script.txt`, `engine/prompts/metadata.txt`
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: `OmniRouteClient`, contract models.
- Produces: `run_research(client, topic) -> tuple[Provenance, CostRecord]`, `run_hooks(client, topic, provenance) -> tuple[list[Hook], CostRecord]`, `run_script(client, topic, provenance, hook) -> tuple[Script, CostRecord]`, `run_metadata(client, topic, script) -> tuple[Metadata, CostRecord]`. Each parses `client.chat(..., want_json=True).data` into the model and raises `AgentError(stage, raw)` on a schema mismatch.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agents.py
import httpx
import pytest
from engine.omniroute import OmniRouteClient
from engine.contract import Topic, Provenance
from engine.agents.hooks import run_hooks
from engine.agents.script import run_script
from engine.agents import AgentError


def _client(payload):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": payload}}]})
    return OmniRouteClient(base="http://x/v1", key="k",
                           transport=httpx.MockTransport(handler))


def test_run_hooks_parses_five_variants():
    payload = '{"hooks": [' + ",".join(
        '{"variant_id": "h%d", "voice_text": "v", "caption_text": "c",'
        ' "style": "question", "seconds": 3.0}' % i for i in range(1, 6)) + ']}'
    hooks, cost = run_hooks(_client(payload), Topic.make("x"), Provenance())
    assert len(hooks) == 5
    assert hooks[0].variant_id == "h1"


def test_run_script_rejects_bad_schema():
    with pytest.raises(AgentError) as e:
        run_script(_client('{"script": {"beats": "nope"}}'),
                   Topic.make("x"), Provenance(), None)
    assert e.value.stage == "script"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.agents.hooks'`

- [ ] **Step 3: Write minimal implementation**

`engine/agents/__init__.py` defines `AgentError(Exception)` with `.stage` and `.raw`, plus `load_prompt(name)` reading from `engine/prompts/`. Each agent module formats its prompt, calls `client.chat(messages, want_json=True)`, validates into the contract model inside a `try/except ValidationError` that re-raises `AgentError`.

`engine/prompts/script.txt` encodes every rule from spec §7.1 as numbered constraints, requires both `voice_text` (Devanagari) and `caption_text` (Roman Hinglish) per beat, demands 9–13 beats, and lists the banned phrases from Global Constraints. `engine/prompts/metadata.txt` requires the pinned comment to present two defensible sides per spec §7.2.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agents.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add engine/agents engine/prompts tests/test_agents.py
git commit -m "feat: four-agent chain with retention rules in prompts"
```

---

## Task 6: Voice synthesis with word timings

**Files:**
- Create: `engine/media/voice.py`
- Test: `tests/test_voice.py`

**Interfaces:**
- Consumes: `Beat`, `Settings`.
- Produces: `async synth_beat(text, out_path, voice, rate, pitch) -> list[WordTiming]`, `synth_plan(plan, work_dir, settings) -> None` which fills `beat.audio_path`, `beat.measured_seconds` and `beat.words` for every beat, and `probe_duration(path, ffmpeg) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice.py
import pytest
from engine.media.voice import offsets_to_timings


def test_offsets_convert_100ns_ticks_to_seconds():
    raw = [{"type": "WordBoundary", "offset": 0, "duration": 5_000_000,
            "text": "Roopkund"},
           {"type": "WordBoundary", "offset": 5_000_000,
            "duration": 3_000_000, "text": "jheel"}]
    timings = offsets_to_timings(raw)
    assert timings[0].word == "Roopkund"
    assert timings[0].start == pytest.approx(0.0)
    assert timings[0].end == pytest.approx(0.5)
    assert timings[1].start == pytest.approx(0.5)
    assert timings[1].end == pytest.approx(0.8)


def test_non_word_events_are_dropped():
    raw = [{"type": "SessionEnd"},
           {"type": "WordBoundary", "offset": 0, "duration": 1_000_000,
            "text": "x"}]
    assert len(offsets_to_timings(raw)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.media.voice'`

- [ ] **Step 3: Write minimal implementation**

`offsets_to_timings` filters `type == "WordBoundary"` and divides `offset` and `offset + duration` by `10_000_000` (edge-tts reports 100-nanosecond ticks). `synth_beat` uses `edge_tts.Communicate(text, voice, rate=..., pitch=...)`, writes `audio` chunks to `out_path` and collects `WordBoundary` chunks. `synth_plan` runs beats through `asyncio.run`, then sets `measured_seconds` from `probe_duration`, which shells out to ffmpeg and parses the `Duration:` line from stderr.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_voice.py -v`
Expected: 2 passed

- [ ] **Step 6: Verify against the real service**

Run: `python -m engine.media.voice --demo "रूपकुंड झील में आठ सौ साल पुराने कंकाल मिले"`
Expected: an mp3 in `work/`, non-zero duration printed, word count > 5.

- [ ] **Step 7: Commit**

```bash
git add engine/media/voice.py tests/test_voice.py
git commit -m "feat: edge-tts Hindi voice with word-level timings"
```

---

## Task 7: Image generation with placeholder fallback

**Files:**
- Create: `engine/media/images.py`
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: `OmniRouteClient`, `Beat`, `Store`.
- Produces: `generate_beat_image(client, beat, out_path, style_suffix) -> tuple[str, str]` returning `(path, provider)`, `placeholder_image(out_path, text, seed) -> str`, `generate_plan_images(client, plan, work_dir, store) -> None` filling `beat.image_path` and calling `store.save_asset` per image. On any OmniRoute image failure it falls back to `placeholder_image` and records provider `"placeholder"` so the pipeline still yields a watchable MP4.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_images.py
from pathlib import Path
from PIL import Image
from engine.media.images import placeholder_image, STYLE_SUFFIX


def test_placeholder_is_vertical_1080x1920(tmp_path):
    p = placeholder_image(tmp_path / "a.png", "Roopkund", seed=1)
    assert Image.open(p).size == (1080, 1920)


def test_placeholder_is_deterministic_per_seed(tmp_path):
    a = Path(placeholder_image(tmp_path / "a.png", "x", seed=7)).read_bytes()
    b = Path(placeholder_image(tmp_path / "b.png", "x", seed=7)).read_bytes()
    assert a == b


def test_style_suffix_pins_a_consistent_look():
    assert "cinematic" in STYLE_SUFFIX.lower()
    assert "9:16" in STYLE_SUFFIX or "vertical" in STYLE_SUFFIX.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_images.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.media.images'`

- [ ] **Step 3: Write minimal implementation**

`STYLE_SUFFIX` is a constant appended to every visual prompt so scenes share a look — dark cinematic, volumetric fog, desaturated, vertical 9:16, no text in image. `placeholder_image` draws a seeded dark gradient with the beat label using Pillow at 1080×1920. `generate_beat_image` calls `client.image(prompt + STYLE_SUFFIX, size="1024x1792")`, writes the bytes, and on exception falls back to the placeholder.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_images.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/media/images.py tests/test_images.py
git commit -m "feat: image generation with deterministic placeholder fallback"
```

---

## Task 8: ASS caption generation

**Files:**
- Create: `engine/assembly/captions.py`
- Test: `tests/test_captions.py`

**Interfaces:**
- Consumes: `ReelPlan` with `beat.words` and `beat.measured_seconds` filled.
- Produces: `ass_time(seconds) -> str`, `build_ass(plan, source='caption_text', font='Arial', font_size=96) -> str`, `write_ass(plan, path, **kw) -> str`. Each beat becomes one `Dialogue` line whose words carry `{\k<centiseconds>}` karaoke tags. Beat start times accumulate from `measured_seconds`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_captions.py
from engine.contract import Topic, Hook, Beat, Script, ReelPlan, WordTiming
from engine.assembly.captions import ass_time, build_ass


def test_ass_time_format():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3.5) == "0:00:03.50"
    assert ass_time(65.25) == "0:01:05.25"


def _plan():
    words = [WordTiming(word="Aath", start=0.0, end=0.5),
             WordTiming(word="sau", start=0.5, end=0.9)]
    b1 = Beat(beat_id="b1", role="hook", voice_text="आठ सौ",
              caption_text="Aath sau", on_screen_text=None, target_seconds=1.0,
              visual_prompt="p", motion="zoom_in", transition="fade",
              measured_seconds=1.0, words=words)
    b2 = Beat(beat_id="b2", role="setup", voice_text="फिर", caption_text="Phir",
              on_screen_text=None, target_seconds=1.0, visual_prompt="p",
              motion="zoom_out", transition="fade", measured_seconds=2.0,
              words=[WordTiming(word="Phir", start=0.0, end=0.8)])
    return ReelPlan(plan_id="p", topic=Topic.make("x"),
                    hooks=[Hook(variant_id="h1", voice_text="v",
                                caption_text="c", style="question",
                                seconds=1.0)],
                    script=Script(total_seconds=3.0, chosen_hook="h1",
                                  beats=[b1, b2]))


def test_build_ass_has_header_and_one_line_per_beat():
    out = build_ass(_plan())
    assert "[Script Info]" in out
    assert "PlayResX: 1080" in out
    assert "PlayResY: 1920" in out
    assert out.count("\nDialogue:") == 2


def test_karaoke_tags_and_cumulative_beat_offset():
    out = build_ass(_plan())
    assert r"{\k50}Aath" in out
    assert r"{\k40}sau" in out
    # second beat starts after the first beat's measured 1.0s
    assert "Dialogue: 0,0:00:01.00,0:00:03.00" in out


def test_devanagari_source_switch():
    out = build_ass(_plan(), source="voice_text")
    assert "आठ सौ" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_captions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.assembly.captions'`

- [ ] **Step 3: Write minimal implementation**

`ass_time` formats `H:MM:SS.cc`. `build_ass` emits `[Script Info]` with `PlayResX: 1080` / `PlayResY: 1920`, a `[V4+ Styles]` block with a bold outlined bottom-centred style, and a `[Events]` block. For each beat it accumulates `offset += measured_seconds`, emits `Dialogue: 0,<start>,<end>,Default,,0,0,0,,` followed by the word tokens. When the beat has `words`, each token is prefixed with `{\k<round(duration*100)>}`; when it does not, the whole `source` text is emitted untagged. `source="voice_text"` swaps the text field.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_captions.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/captions.py tests/test_captions.py
git commit -m "feat: ASS caption builder with karaoke word timing"
```

---

## Task 9: ffmpeg render

**Files:**
- Create: `engine/assembly/render.py`, `engine/assembly/compile.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `ReelPlan` with `image_path`, `audio_path`, `measured_seconds` filled; `Settings`.
- Produces: `zoompan_expr(motion, duration, fps) -> str`, `build_filter_graph(plan, fps) -> tuple[str, list[str]]` returning `(filter_complex, input_args)`, `render(plan, settings, out_path, ass_path=None, music_path=None, progress=None) -> str`, and in `compile.py`: `to_generate_video_body(plan, output_name) -> dict` producing the exact body the existing `POST /generate-video` accepts.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
import pytest
from engine.assembly.render import zoompan_expr
from engine.assembly.compile import to_generate_video_body
from tests.test_captions import _plan


def test_zoompan_zoom_in_grows_and_zoom_out_shrinks():
    assert "zoom+" in zoompan_expr("zoom_in", 3.0, 30)
    assert "1.25-" in zoompan_expr("zoom_out", 3.0, 30)


def test_zoompan_pans_horizontally():
    assert "x=" in zoompan_expr("move_left", 3.0, 30)
    assert zoompan_expr("move_left", 3.0, 30) != \
           zoompan_expr("move_right", 3.0, 30)


def test_zoompan_frame_count_matches_duration():
    assert ":d=90" in zoompan_expr("zoom_in", 3.0, 30)


def test_compile_body_matches_existing_endpoint_shape():
    plan = _plan()
    for i, b in enumerate(plan.script.beats):
        b.image_path = f"img{i}.png"
    body = to_generate_video_body(plan, "out.mp4")
    assert body["settings"]["frame_size"] == [1080, 1920]
    assert body["settings"]["layout_mode"] == "blur_bg"
    assert body["images"][0]["path"] == "img0.png"
    assert body["images"][0]["duration"] == pytest.approx(1.0)
    assert body["images"][0]["motion"] == "zoom_in"
    assert body["output_name"] == "out.mp4"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.assembly.render'`

- [ ] **Step 3: Write minimal implementation**

`zoompan_expr` returns a `zoompan` filter string with `d=round(duration*fps)`, `s=1080x1920`, `fps=<fps>`; `zoom_in` uses `z='zoom+0.0015'`, `zoom_out` uses `z='1.25-0.0015*on'`, and the pan motions hold zoom at `1.15` while driving `x` left-to-right or right-to-left. `build_filter_graph` scales and pads each still to 1080×1920, applies its `zoompan`, chains consecutive segments with `xfade` at `transition_duration=0.5`, concatenates the per-beat narration with `concat`, applies `loudnorm=I=-14`, and appends `subtitles=<ass>` when `ass_path` is given. `render` shells out to `settings.ffmpeg` with `-y`, parses `time=` from stderr for progress callbacks, and returns `out_path`.

`to_generate_video_body` maps the ReelPlan onto the existing endpoint body per spec §6.2, using `measured_seconds or target_seconds` for `duration`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_render.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/render.py engine/assembly/compile.py tests/test_render.py
git commit -m "feat: ffmpeg render graph and legacy endpoint compiler"
```

---

## Task 10: QC scorecard

**Files:**
- Create: `engine/gates/qc.py`
- Test: `tests/test_qc.py`

**Interfaces:**
- Consumes: `ReelPlan`.
- Produces: `Check(name, ok, kind, detail)` where `kind` is `"hard"` or `"warn"`, `Scorecard(checks)` with `.passed` (no hard failure) and `.hard_failures`, `BANNED_PHRASES: tuple[str, ...]`, `run_qc(plan, similarity=None) -> Scorecard`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qc.py
from engine.gates.qc import run_qc, BANNED_PHRASES
from engine.contract import Claim, Provenance, Safety
from tests.test_captions import _plan


def _long_plan():
    plan = _plan()
    base = plan.script.beats[0]
    plan.script.beats = []
    for i in range(10):
        b = base.model_copy(update={"beat_id": f"b{i}", "measured_seconds": 4.2,
                                    "words": []})
        plan.script.beats.append(b)
    plan.safety = Safety(moderation_passed=True, flags=[])
    return plan


def test_duration_out_of_range_is_hard_failure():
    plan = _plan()  # only 3s total
    sc = run_qc(plan)
    assert not sc.passed
    assert any(c.name == "duration" for c in sc.hard_failures)


def test_good_plan_passes():
    assert run_qc(_long_plan()).passed


def test_banned_phrase_is_hard_failure():
    plan = _long_plan()
    plan.script.beats[0].caption_text = "Doston aaj hum baat karenge"
    sc = run_qc(plan)
    assert not sc.passed
    assert any(c.name == "banned_phrase" for c in sc.hard_failures)


def test_claim_without_source_is_hard_failure():
    plan = _long_plan()
    plan.provenance = Provenance(claims=[
        Claim(beat_id="b0", text="800 saal purane", source_url=None,
              confidence="high")])
    sc = run_qc(plan)
    assert any(c.name == "claim_provenance" for c in sc.hard_failures)


def test_high_similarity_is_hard_failure():
    sc = run_qc(_long_plan(), similarity=0.91)
    assert any(c.name == "semantic_similarity" for c in sc.hard_failures)


def test_banned_list_is_populated():
    assert "aaj hum baat karenge" in BANNED_PHRASES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_qc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.gates.qc'`

- [ ] **Step 3: Write minimal implementation**

`BANNED_PHRASES` is the tuple from Global Constraints. `run_qc` builds the checks from spec §7.4: duration in 38–52s (hard), every claim has a non-empty `source_url` (hard), `similarity < 0.88` when provided (hard), `safety.moderation_passed` (hard), banned-phrase scan over `caption_text` and `voice_text` case-insensitively (hard), visual change rate `duration / len(beats) <= 4.0` (warn), sentence-length stdev `> 5.5` (warn). Checks whose input is absent are skipped rather than failed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_qc.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add engine/gates/qc.py tests/test_qc.py
git commit -m "feat: QC scorecard with hard and warn checks"
```

---

## Task 11: Publish payload builders

**Files:**
- Create: `engine/publish/payloads.py`
- Test: `tests/test_payloads.py`

**Interfaces:**
- Consumes: `ReelPlan`, a rendered video path.
- Produces: `youtube_payload(plan, video_path) -> dict` and `instagram_payload(plan, video_url) -> dict`. Both set the synthetic-content disclosure. Neither performs any network call — this module imports no HTTP library.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payloads.py
import inspect
import engine.publish.payloads as p
from tests.test_captions import _plan
from engine.contract import Metadata


def _with_meta():
    plan = _plan()
    plan.metadata = Metadata(yt_title="T", yt_description="D", ig_caption="C",
                             pinned_comment="P", hashtags=["#a", "#b"],
                             thumbnail_prompt="")
    return plan


def test_youtube_payload_sets_synthetic_disclosure():
    body = p.youtube_payload(_with_meta(), "out.mp4")
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["title"] == "T"


def test_instagram_payload_is_reels_and_carries_caption():
    body = p.instagram_payload(_with_meta(), "https://x/v.mp4")
    assert body["media_type"] == "REELS"
    assert "C" in body["caption"]
    assert body["video_url"] == "https://x/v.mp4"


def test_module_makes_no_network_calls():
    src = inspect.getsource(p)
    for forbidden in ("httpx", "requests", "urllib", "socket"):
        assert forbidden not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_payloads.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.publish.payloads'`

- [ ] **Step 3: Write minimal implementation**

Two pure functions returning dicts. `youtube_payload` builds `snippet` (title, description with hashtags appended, `categoryId` `"27"`) and `status` (`privacyStatus: "private"`, `selfDeclaredMadeForKids: False`, `containsSyntheticMedia: True`). `instagram_payload` returns `{"media_type": "REELS", "video_url": ..., "caption": ig_caption + hashtags, "share_to_feed": True}`. A module docstring states that a human runs publishing and no scheduler may import these.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_payloads.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/publish/payloads.py tests/test_payloads.py
git commit -m "feat: publish payload builders, no network side effects"
```

---

## Task 12: Pipeline orchestration

**Files:**
- Create: `engine/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: every module above.
- Produces: `Stage` enum-like string constants, `PipelineEvent(stage, status, detail, payload)`, `plan_stage(topic_raw, client, store, emit) -> ReelPlan` (research → hooks → script → metadata → moderation → dedup, stopping before media), and `produce_stage(plan, client, store, settings, emit, captions_source) -> dict` (images → voice → captions → render → QC). `emit` is a `Callable[[PipelineEvent], None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
from engine.pipeline import PipelineEvent, Stage


def test_events_carry_stage_and_status():
    e = PipelineEvent(stage=Stage.SCRIPT, status="done", detail="11 beats")
    assert e.stage == "script"
    assert e.status == "done"
    assert e.payload == {}


def test_stage_order_is_declared():
    assert Stage.ORDER.index(Stage.RESEARCH) < Stage.ORDER.index(Stage.SCRIPT)
    assert Stage.ORDER.index(Stage.VOICE) < Stage.ORDER.index(Stage.RENDER)
    assert Stage.ORDER[-1] == Stage.QC
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.pipeline'`

- [ ] **Step 3: Write minimal implementation**

`Stage` holds the string constants and an `ORDER` tuple ending in `QC`. `plan_stage` emits a `started`/`done` event around each agent, records each `CostRecord` via `store.record_cost`, runs `client.moderate` on the concatenated script, then `dedup.check`, raising `GateError` on failure. `produce_stage` generates images, synthesises voice, writes the `.ass`, renders, runs `run_qc`, and returns `{"video": path, "scorecard": ..., "cost_usd": ...}`. Before any paid call it checks `store.today_usd() < settings.daily_usd_ceiling`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pipeline.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add engine/pipeline.py tests/test_pipeline.py
git commit -m "feat: pipeline orchestration with progress events"
```

---

## Task 13: FastAPI app and web UI

**Files:**
- Create: `engine/app.py`, `engine/ui/index.html`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `pipeline`, `store`, `settings`.
- Produces: routes `GET /` (the UI), `GET /api/health` (reports whether OmniRoute answered), `POST /api/plan` (body `{"topic": str}` → ReelPlan JSON), `GET /api/plan/{id}`, `POST /api/plan/{id}/approve` (body `{"chosen_hook": str, "beats": [...]}` → persists human edits), `POST /api/plan/{id}/produce` → render result, `GET /api/plans`, `GET /api/events/{id}` (SSE progress), `GET /media/{path}` (serves `outputs/`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py
from fastapi.testclient import TestClient
from engine.app import create_app


def test_health_reports_omniroute_state(tmp_path):
    c = TestClient(create_app(db_path=tmp_path / "t.db"))
    body = c.get("/api/health").json()
    assert "omniroute" in body
    assert body["ffmpeg"] is True


def test_root_serves_the_ui(tmp_path):
    c = TestClient(create_app(db_path=tmp_path / "t.db"))
    r = c.get("/")
    assert r.status_code == 200
    assert "Rahasya" in r.text


def test_plans_list_is_empty_initially(tmp_path):
    c = TestClient(create_app(db_path=tmp_path / "t.db"))
    assert c.get("/api/plans").json() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.app'`

- [ ] **Step 3: Write minimal implementation**

`create_app(db_path=None)` builds the FastAPI instance, calls `Store.init()`, and registers the routes. `/api/health` does a 2-second `GET` on `{base}/models` and reports `omniroute: bool` plus `ffmpeg: Path(settings.ffmpeg).exists()`. Long-running work runs in a thread and pushes `PipelineEvent`s onto a per-plan `queue.Queue` that `/api/events/{id}` drains as SSE.

`engine/ui/index.html` is a single dark-themed page: a topic input, a stage-by-stage progress rail driven by SSE, a hook picker showing all five variants, an editable beat table (`caption_text`, `voice_text`, `visual_prompt`), an image strip, a `<video>` preview, a QC scorecard table with pass/warn/fail rows, a cost readout, and copy buttons for the title, IG caption and pinned comment. No external CDN — all CSS and JS inline.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add engine/app.py engine/ui/index.html tests/test_app.py
git commit -m "feat: FastAPI app with local dark-theme control UI"
```

---

## Task 14: End-to-end verification and repo fixes

**Files:**
- Create: `scripts/verify_e2e.py`, `scripts/fix_editor_backend.md`, `.env.example`, `README.md`
- Test: `tests/test_e2e_offline.py`

**Interfaces:**
- Consumes: everything.
- Produces: `scripts/verify_e2e.py` which runs the full pipeline with a fake OmniRoute client and real edge-tts, placeholder images and real ffmpeg, then asserts a playable MP4 exists with the expected duration.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_e2e_offline.py
import subprocess
import sys
from pathlib import Path


def test_offline_e2e_produces_playable_mp4(tmp_path):
    r = subprocess.run([sys.executable, "scripts/verify_e2e.py",
                        "--out", str(tmp_path)],
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    mp4s = list(Path(tmp_path).glob("*.mp4"))
    assert mp4s and mp4s[0].stat().st_size > 100_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_e2e_offline.py -v`
Expected: FAIL — `scripts/verify_e2e.py` does not exist

- [ ] **Step 3: Write minimal implementation**

`scripts/verify_e2e.py` builds a `FakeOmniRoute` returning a fixed 10-beat Hinglish ReelPlan, uses real `edge-tts` for voice, `placeholder_image` for visuals, real `build_ass`, and real `render`. It prints measured duration, QC scorecard and cost, then exits non-zero if the MP4 is missing or QC hard-fails.

`scripts/fix_editor_backend.md` documents the three fixes for the existing repo: replace the hardcoded `BASE` in `final/generate_video_final.py:32` with an env var, move `agent.py:11`'s `API_KEY` into `.env` and point its client at OmniRoute, and add `imageio-ffmpeg` so moviepy finds a binary.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_e2e_offline.py -v`
Expected: 1 passed

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts .env.example README.md tests/test_e2e_offline.py
git commit -m "feat: offline end-to-end verification and legacy repo fixes"
```

---

## Task 15: Retention and monetization playbook

**Files:**
- Create: `docs/playbook-retention-monetization.md`

- [ ] **Step 1: Write the document**

Covers, for the Hindi/India Instagram-primary track: the 0–3s hook taxonomy with Hinglish examples; the 45-second beat map with second-by-second targets; the pinned-comment debate formula; hashtag strategy (small/medium/large mix, no banned tags); posting cadence that stays under the inauthentic-content radar; the brand-deal readiness checklist with the metrics that actually get priced (saves, shares, comments, follower growth rate); affiliate categories that convert in this niche; and the YouTube-side reality that Shorts AdSense is a rounding error until 10M views/90d.

- [ ] **Step 2: Commit**

```bash
git add docs/playbook-retention-monetization.md
git commit -m "docs: retention and monetization playbook"
```

---

## Self-Review Notes

**Spec coverage:** §4 component map → Tasks 1,5,6,7,9,12. §5 provider table → Tasks 1,6,7,9. §6 contract → Task 2. §6.2 compile → Task 9. §7 agents → Task 5. §7.1 retention rules → Task 5 prompt + Task 10 checks. §7.2 pinned comment → Task 5 metadata prompt + Task 15. §7.3 dedup → Task 4. §7.4 QC → Task 10. §8 store → Task 3. §9 failure policy → Tasks 1 (retries) and 12 (spend ceiling). §10 out-of-scope respected — Task 11 builds payloads with a no-network test. §11 verification → Task 14. UI (added after the spec, at user request) → Task 13.

**Deviation from spec §5:** assembly uses direct ffmpeg filter graphs rather than moviepy's `create_video_from_image_and_audio`. Reason: moviepy is not installed and is unverified on Python 3.14, while ffmpeg v7.1 with `zoompan`/`xfade`/`subtitles`/`loudnorm` is verified present on this machine. Task 9 still ships `to_generate_video_body` so the existing EDITOR-_BACKEND endpoint remains a usable alternative renderer.

**Type consistency:** `CostRecord` fields are identical across Tasks 1, 3 and 12. `Beat.measured_seconds` is the single duration source in Tasks 6, 8, 9 and 10. `Stage` constants in Task 12 match the SSE stage names the Task 13 UI renders.

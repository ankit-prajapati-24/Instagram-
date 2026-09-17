# Rahasya Engine — Design Spec

**Date:** 2026-09-17
**Status:** Approach + track approved by user. Implementation pending morning review.
**Scope:** Sub-project A (Brain) + the bridge into Sub-project C (Assembly). B and D get their own specs.

---

## 1. What this is

An automated content engine that turns one mystery / dark-secret topic into a
45-second vertical video, publishable to Instagram Reels and YouTube Shorts.

- **Track (decided):** Hinglish/Hindi-first, India audience.
- **Approach (decided):** "Hybrid Assembly" — AI-generated stills with Ken Burns
  motion and timed text reveals; optionally one AI video clip for the 0–3s hook.

## 2. The revenue inversion (why this shapes the architecture)

The Hindi/India track makes YouTube AdSense economically irrelevant:

- Shorts India RPM: ₹5–30 (~$0.15)
- Shorts Creator Pool requires **10M qualified views / 90 days** before any payout
- From 1 Feb 2027, new YPP Shorts applicants need **20M views / 90 days**

Therefore:

| Platform | Role | Revenue |
|---|---|---|
| **Instagram Reels** | **Primary** | Brand deals, affiliate |
| YouTube Shorts | Secondary | Reach, social proof, search longevity |

The engine optimises for **saves + shares + comments** — the metrics brand deals
are priced on — not for watch-hours.

## 3. Non-negotiable constraints

Requirements, not features. Each traces to a documented policy risk.

1. **One human gate.** After script generation, before render.
2. **Semantic dedup before production.** Four layers (§7.3). Primary defence
   against channel-level "inauthentic content" enforcement.
3. **No auto-publish.** Publishing code is written and callable, but the
   scheduler never invokes it. A human runs it.
4. **Synthetic-content disclosure** set on every upload payload. QC hard-fail.
5. **Provenance for every factual claim.** Source URL stored, or the claim is cut.

## 4. Component map

```
        topic (human input, one line)
                  |
    +-------------v--------------------------------------+
    |  BRAIN  ->  OmniRoute  http://localhost:20128/v1   |
    |  /v1/chat/completions   hooks, script, prompts, meta|
    |  /v1/search /web/fetch  research + citations        |
    |  /v1/embeddings         dedup vectors               |
    |  /v1/moderations        safety gate (cost $0)       |
    +-------------+--------------------------------------+
                  | ReelPlan JSON (contract, §6)
                  v
         [ HUMAN GATE ]  approve / edit / reject
                  |
        +---------+----------+
        v                    v
   VOICE (local)        IMAGES (OmniRoute)
   edge-tts             /v1/images/generations
   hi-IN-MadhurNeural   FLUX / Grok / ComfyUI-local
        |                    |
        +---------+----------+
                  v
          TIMING   edge-tts WordBoundary events
                  v
          ASSEMBLE  create_video_from_image_and_audio()
                    (existing EDITOR-_BACKEND function)
                  v
          CAPTIONS  ffmpeg libass (.ass, karaoke \k tags)
                  v
          QC SCORECARD  ->  pass / human queue
                  v
          outputs/*.mp4 + metadata.json   (publish = manual)
```

## 5. Why each provider choice

| Layer | Choice | Why not the obvious alternative |
|---|---|---|
| Text / reasoning | OmniRoute `/v1/chat/completions` | Direct provider SDKs mean rate limits and key sprawl. The gateway gives fallback, free-tier stacking, and cost telemetry headers |
| Images | OmniRoute `/v1/images/generations` | The same endpoint later routes to **local ComfyUI** when free tiers throttle at ~40 img/day. Zero code change |
| **Voice** | **Local `edge-tts`** | OmniRoute's `/v1/audio/speech` documents only `openai/tts-1`; no `hi-IN` voice is documented. `openai/tts-1` speaks Hindi with a foreign accent — a retention killer for this audience. edge-tts gives true `hi-IN` neural voices, free and unmetered |
| Word timing | edge-tts `WordBoundary` events | A Whisper round-trip is slower and lossy when we already know the text we synthesised |
| Captions | ffmpeg `subtitles` filter (libass) | Verified: bundled ffmpeg v7.1 has `libass` + `libharfbuzz` + `fontconfig`. Pillow on this machine reports `RAQM=False`, so it cannot shape Devanagari. libass also gives karaoke word-highlight free |
| Assembly | existing `create_video_from_image_and_audio()` | Already vertical (1080×1920), already has per-image motion/transition and `blur_bg`. Rewriting it would be waste |
| Store | SQLite | Single operator, single machine. Schema matches the Postgres design so migration is a driver swap |

### 5.1 Voice tone problem (open, mitigated)

Both `hi-IN` edge-tts voices are tagged *"Friendly, Positive"* — the wrong
register for dark mystery. Mitigation: `rate=-8%`, `pitch=-6Hz` on
`hi-IN-MadhurNeural`, plus a low music bed.

**This needs an ear test, not a unit test.** If it still sounds cheerful, the
fallback is a paid ElevenLabs Hindi voice (~₹1.5/video at 45s) called directly,
not through the gateway.

## 6. The ReelPlan contract

One JSON object is the only thing the brain emits; everything downstream
consumes it. `engine/contract.py` holds the Pydantic models. This is the shape:

```json
{
  "schema_version": "1.0",
  "plan_id": "uuid",
  "topic": {
    "raw": "Roopkund lake ke 800 saal purane kankaal",
    "slug": "roopkund-skeletons",
    "entities": ["Roopkund", "Uttarakhand"],
    "dedupe_hash": "sha256..."
  },
  "hooks": [
    {
      "variant_id": "h1",
      "voice_text": "<Devanagari, 0-3s>",
      "caption_text": "<Roman Hinglish, big text>",
      "style": "question|claim|number|contradiction|threat",
      "seconds": 3.0
    }
  ],
  "script": {
    "total_seconds": 45.0,
    "chosen_hook": "h1",
    "beats": [
      {
        "beat_id": "b1",
        "role": "hook|setup|escalation|reveal|twist|cliffhanger|cta",
        "voice_text": "<Devanagari — fed to TTS>",
        "caption_text": "<Roman Hinglish — burned on screen>",
        "on_screen_text": "<optional 3-5 word punch, or null>",
        "target_seconds": 4.5,
        "visual_prompt": "<English, for the image model>",
        "motion": "zoom_in|zoom_out|move_left|move_right",
        "transition": "fade|slide_left|slide_right|zoom|blur"
      }
    ]
  },
  "metadata": {
    "yt_title": "<=100 chars",
    "yt_description": "",
    "ig_caption": "",
    "pinned_comment": "<debate-starter question>",
    "hashtags": ["..."],
    "thumbnail_prompt": ""
  },
  "provenance": {
    "claims": [
      {"beat_id": "b3", "text": "...", "source_url": "https://...", "confidence": "high|medium|low"}
    ]
  },
  "safety": {"moderation_passed": true, "flags": []},
  "cost": {"usd": 0.0, "by_provider": {}, "fallback_attempts": 0}
}
```

### 6.1 The dual-text rule (key design decision)

Every beat carries `voice_text` in **Devanagari** and `caption_text` in **Roman
Hinglish**.

- Devanagari is required for correct TTS pronunciation.
- Roman is what gets burned on screen: it is the actual convention in Indian
  Reels, and it carries zero text-shaping risk.

A `--captions=devanagari` flag switches the burn source, since libass can shape
it correctly.

### 6.2 Compiling to the assembly call

`engine/assembly/compile.py` turns a ReelPlan plus rendered assets into the exact
body that `POST /generate-video` already accepts:

| ReelPlan field | Assembly field |
|---|---|
| `beats[].image_path` (filled by media stage) | `images[].path` |
| `beats[].measured_seconds` (from TTS) | `images[].duration` |
| `beats[].transition` | `images[].transition` |
| `beats[].motion` | `images[].motion` |
| stitched narration wav | `audio_path` |
| fixed | `settings.frame_size = [1080, 1920]`, `layout_mode = "blur_bg"` |

Durations come from **measured TTS audio**, never from the model's
`target_seconds` guess. The estimate only steers script length.

## 7. Agent chain

Four calls, each with its own prompt file in `engine/prompts/` and a strict JSON
schema. All go through OmniRoute.

| # | Agent | Input | Output | Model tier |
|---|---|---|---|---|
| 1 | `research` | topic | claims + source URLs | cheap + `/v1/search` |
| 2 | `hooks` | topic + claims | 5 hook variants | strong — this is the retention lever |
| 3 | `script` | topic + claims + chosen hook | beats with dual text + visual prompts | strong |
| 4 | `metadata` | script | titles, captions, pinned comment, hashtags | cheap |

### 7.1 Retention rules encoded in the script prompt

Not advice in a doc — constraints in the prompt, checked by QC:

- **0–1s:** a visual or claim that cannot be scrolled past. No channel intro, ever.
- **0–3s:** the hook states a *specific* unresolved thing. Banned openers:
  "aaj hum baat karenge", "kya aap jaante hain".
- **Every 2.5–4s:** a visual change (new image or on-screen text pop). Enforced
  by beat count.
- **~60% mark:** one pattern interrupt — a contradiction or a "lekin" reversal.
- **Last 2s:** loop-close. The final line makes re-watching the hook make sense.
- **No CTA asking for a like.** The pinned comment does the engagement work.
- **Sentence-length variance** must exceed a floor (AI-tell detector).

### 7.2 Pinned comment strategy

The single highest-leverage monetisation asset, because Instagram brand deals are
priced on comments and saves. The metadata agent must produce a **genuine open
question with two defensible sides**, not "what do you think?".

Example shape: *"Official report kehti hai X. Locals 50 saal se Y bolte hain.
Tum kispe bharosa karoge?"*

### 7.3 Dedup gate (four layers, cheapest first)

1. SHA-256 of the normalised topic → exact reject
2. Trigram similarity vs published slugs, threshold 0.6
3. Cosine similarity of `title + angle` embedding vs last 400 plans,
   **threshold 0.88** → hard reject
4. Entity cooldown: the same entity blocked for **45 days**

### 7.4 QC scorecard

Any hard-fail routes to the human queue. Nothing auto-retries a semantic failure.

| Check | Threshold | Type |
|---|---|---|
| Every numeric / factual claim has a source URL | 100% | hard fail |
| Semantic similarity to last 400 | < 0.88 | hard fail |
| `/v1/moderations` flagged | false | hard fail |
| Synthetic-content disclosure set | true | hard fail |
| Audio silence gap | none > 1.2s | hard fail |
| Caption / audio word alignment | ≥ 98% | hard fail |
| Total duration | 38–52s | hard fail |
| Banned-phrase hits | 0 | hard fail |
| Visual change rate | ≥ 1 per 4s | warn |
| Audio loudness | −14 LUFS ±1 | auto-fix |
| Sentence-length stdev | > 5.5 | warn |

## 8. Store schema (SQLite)

Mirrors the Postgres design in the feasibility report, so migration is a driver
swap rather than a rewrite:

`topics`, `plans`, `claims`, `assets`, `renders`, `publications`,
`metrics_daily`, `costs`, `jobs`

Two tables carry disproportionate weight:

- **`assets`** — provider, licence, source URL, checksum per asset. This table
  *is* the copyright defence.
- **`costs`** — fed directly from the `X-OmniRoute-Response-Cost`,
  `X-OmniRoute-Provider` and `X-OmniRoute-Fallback-Attempts` response headers.
  Per-video true cost with no separate accounting.

## 9. Failure policy

| Class | Examples | Policy |
|---|---|---|
| Transient | 429/5xx, timeout, render OOM | exponential backoff + jitter, 5 attempts |
| Deterministic | malformed JSON, missing asset | 1 repair-prompt retry, then dead-letter |
| Semantic | dedup fail, QC fail, moderation flag | **no retry** → human queue |

Every job carries an `idempotency_key` (`render:{plan_id}:v{n}`). A daily USD
spend ceiling pauses the pipeline and alerts when crossed.

## 10. Explicitly out of scope here

- Automatic publishing to any platform (code exists; the scheduler never calls it)
- Instagram Graph API wiring — needs a Business/Creator account linked to a
  Facebook Page, which is a human setup prerequisite
- Multi-tenant / agency mode
- English-language variant (Phase 2; the pipeline is language-parameterised for it)
- Thumbnail generation beyond a prompt string

## 11. Verification status

An honest split, because OmniRoute is not running on this machine yet — port
20128 refused connection at design time.

| Verified on this machine | Not yet verified |
|---|---|
| ffmpeg v7.1 with `libass` + `libharfbuzz` + `fontconfig` | any OmniRoute endpoint returning 200 |
| edge-tts `hi-IN-MadhurNeural` / `hi-IN-SwaraNeural` exist | Hindi voice *tone* suitability (ear test) |
| `uharfbuzz` 0.56.1 installed (fallback shaper) | image model quality / consistency for this niche |
| `Nirmala.ttc` Devanagari font present | end-to-end render on real assets |
| Pillow `RAQM=False` — why libass is required | actual per-video cost |

Everything OmniRoute-facing is built against a fake client and integration-tested
through recorded-response fixtures. The first real 200 is a morning task.

# Stock video clips as the visual layer

**Status:** approved 2026-09-21, not yet implemented.

Replaces the still-image visual stage with real stock footage from Pexels,
matched per beat by `stock_agent.py`. The images path stays, demoted to a
fallback tier.

---

## Why

Scene visuals have been the weakest part of the pipeline since it started
producing videos. The three-tier image chain (gateway → keyless endpoint →
placeholder) has produced runs that were more than half flat gradient frames,
and the gateway tier died on 2026-09-18 with a 429 and a 163-hour reset. The
free keyless tier watermarks, upscales from a smaller source, and its
commercial terms were never verified.

Stock footage fixes all three at once: real motion instead of Ken Burns on a
still, a documented licence, and a provider that does not run out after six
requests.

---

## Decisions already taken

Two forks were settled before this document, and everything below follows from
them:

**Fast-cut pacing.** One clip per 2.5 seconds, which is what
`VisualQueryGenerator.calculate_clip_count` already implements
(`ceil(duration / 2.5)`). A 50-second video becomes roughly 20 clips, not 12.

**Per-beat segmentation.** The agent is called once per beat with that beat's
`voice_text` and its *measured* duration, rather than once with the whole
script. A clip therefore never straddles a beat boundary. This is the decision
that preserves the existing A/V sync architecture — see below.

---

## The stage order has to change

Current order in `produce_stage`:

```
IMAGES → VOICE → CAPTIONS → RENDER
```

Images never needed a duration. Clips do: clip count is a function of beat
length, and beat length is only known after synthesis writes
`beat.measured_seconds`.

Using `target_seconds` (the model's guess) instead would be the wrong fix. The
whole timing model in this codebase is measured-not-guessed, and the 4.5s drift
bug came from exactly that class of assumption.

New order:

```
VOICE → CLIPS → CAPTIONS → RENDER
```

`Stage.IMAGES` becomes `Stage.CLIPS` in the stage list and in the panel's
progress strip. The images code still runs, but underneath the clips stage as
a fallback, not as its own stage.

---

## Components

### `engine/media/clips.py` (new)

Mirrors the shape of `engine/media/images.py` so the two are readable side by
side and the pipeline call site barely changes:

```python
def generate_plan_clips(plan, settings, work_dir, store, *,
                        workers=4, progress=None) -> dict[str, int]
```

Returns provider counts (`{"pexels": 18, "image-fallback": 2}`) the same way
`generate_plan_images` does, so the existing emit/telemetry code is unchanged.

Internally it wraps `StockVideoMatcherAgent`, one `match()` call per beat,
across a `ThreadPoolExecutor` exactly as image generation already does.

`stock_agent.py` is **not modified**. It is consumed as a library through its
existing public surface:

```python
agent.match(script_segment=beat.voice_text,
            duration_seconds=beat.measured_seconds,
            download=True, output_dir=...)
# -> StockMatcherResult.matches: list[ClipMatch]
```

Its `main()` CLI stays working and untouched.

### `engine/media/images.py` (unchanged)

Becomes tier 2 of the clip chain. No edits.

---

## Contract

`Beat` gains a list, and keeps its image fields for the fallback:

```python
class Clip(Coercing):
    path: str
    query: str                 # the physical-visual search phrase used
    provider: str              # "pexels" | "image-fallback" | "placeholder"
    duration: float            # the slot this clip fills, not the source length
    source_url: str | None = None
    pexels_id: int | None = None
    author: str | None = None
    licence: str | None = None

class Beat(Coercing):
    ...
    clips: list[Clip] = Field(default_factory=list)
```

`image_path` and `image_provider` stay. They are how a fallback clip slot
reports itself.

Each clip also gets a row in the existing `assets` table, with its real
`provider`, `source_url`, `licence` and `checksum` — the same bookkeeping
images already get, so the publish checklist can flag a render that leaned on
fallbacks.

---

## Render graph

`build_filter_graph` currently emits one video segment per beat. It will emit
one per clip, with beats as the grouping that owns the timeline.

Per clip:

```
trim → scale=1080:1920:force_original_aspect_ratio=increase
     → crop=1080:1920 → fps → setsar=1 → format=yuv420p
```

Source audio is dropped. Pexels clips carry their own audio and none of it
belongs in the render. A clip shorter than its slot is looped rather than
frozen.

`zoompan` disappears for video clips. The footage already moves; Ken Burns on
top of it reads as a mistake. `Beat.motion` stops applying to video clips and
continues to apply only to a fallback still.

### Transitions

**Hard cuts inside a beat, xfade only at beat boundaries.**

Fast-cut pacing is built out of hard cuts — that is the look. It also removes
the xfade padding problem entirely at the clip level: no overlap to pay for,
no per-join compression to correct. `Beat.transition` keeps its current
meaning and still drives the crossfade between beats.

### Timeline

Unchanged where it matters. A beat's total is still its measured narration
length, `segment_lengths` still pads beat segments and centres beat
transitions on their cuts, and the clip layer only subdivides the span a beat
already owns:

```
beat.seconds() == sum(clip.duration for clip in beat.clips)
```

That invariant is the whole reason for per-beat segmentation. It is asserted in
tests, and the existing `av_sync` QC check is untouched.

Slots divide the beat evenly: each clip's `duration` is
`beat.seconds() / len(beat.clips)`, with any floating-point remainder added to
the last slot so the sum is exact rather than approximately right. A beat
shorter than 2.5s yields a single slot spanning the whole beat, which
`ceil(duration / 2.5)` already gives.

Source footage is fitted to its slot, never the other way round: longer than
the slot is trimmed, shorter is looped. A clip's source length has no effect on
the timeline.

---

## Fallback

The same three-tier idea already in `images.py`, applied per clip slot:

```
1. Pexels clip
2. the existing image chain  (a still, with zoompan, filling that slot)
3. a placeholder frame
```

A missing clip therefore never fails a render, and the filter graph must
handle a mix of video and still inputs in one chain. Tier 2 and 3 slots are
the only place `zoompan` still appears.

---

## Error handling

- **No `PEXELS_API_KEY`** — the clips stage reports it once, clearly, and every
  slot falls to tier 2. A run still completes. It must not look like success:
  the provider counts in the panel will read `image-fallback` for every slot,
  the way `keyless` and `placeholder` already surface today.
- **A query returns nothing** — that slot falls through. Other slots are
  unaffected.
- **Download fails or truncates** — `ClipDownloader` already writes to a
  `.tmp` and renames; a failed slot falls through.
- **Pexels rate limit** — free tier is 200 requests/hour. At ~20 clips a video
  that is roughly 10 videos an hour. Treated as a real limit, surfaced, not
  retried into the ground.

---

## Testing

- `sum(clip.duration) == beat.seconds()` for every beat, on generated and
  hand-built plans.
- Clip count matches `ceil(measured / 2.5)` per beat.
- Filter graph shape: one segment per clip, hard cuts within a beat, xfade at
  beat boundaries only, audio dropped from every video input.
- Fallback: a beat whose Pexels lookup returns nothing still renders, with
  `provider == "image-fallback"` recorded.
- Mixed graph: a plan with both video slots and still slots produces a valid
  command.
- The existing labelled-frame A/V proof, which previously measured 0.000s
  drift, re-run against a clip-based render.

---

## Prerequisites

`PEXELS_API_KEY` in `.env`. The key is already templated in `.env.example`.
Registration is free. Without it the pipeline runs but produces only fallback
visuals.

The four dependencies that arrived with `stock_agent.py` — `python-dotenv`,
`openai`, `requests`, `rich` — are in `requirements.txt` but three are not
installed on the current machine, which is why `tests/test_stock_agent.py`
currently errors at setup. `pip install -r requirements.txt` resolves it, and
that should happen before implementation starts so the baseline is green.

---

## Risks worth stating

**Disk and time both go up.** Twelve images become roughly twenty HD portrait
clips, each several MB. This will be slower than image generation and will grow
`work/` considerably. Cleanup of downloaded clips after a successful render is
worth doing, but is not in this scope.

**Licence terms need reading, not assuming.** The Pexels licence is documented,
unlike the keyless image endpoint's, but the exact terms get recorded per asset
during implementation rather than asserted here.

**Visual coherence is unproven.** Twenty unrelated stock clips can read as a
generic template rather than one piece. The image path solved this with a
shared `STYLE_SUFFIX` on every prompt; stock footage has no equivalent lever.
Whether the result looks like a real channel or like filler is a judgement that
needs eyes on a finished render, not a test.

---

## Out of scope

- Modifying `stock_agent.py`.
- Removing the image chain.
- Cleaning up downloaded clips between runs.
- Any change to voice, captions, QC gates, dedup, or publishing.

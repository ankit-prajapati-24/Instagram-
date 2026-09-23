# Choosing sticker art from the panel

Let the person approving a reel swap the designed art on any sticker that
fired, from Lordicon's full catalogue, without touching a file or running a
script.

## Why

The sticker feature ships art for five triggers — `death`, `question`,
`science`, `time`, `witness` — chosen once, by eye, and committed. The other
ten fall back to a Segoe UI Emoji glyph, and `water` is the most-fired trigger
in the corpus.

Adding a sixth today means editing `engine/data/stickers.json`, running
`scripts/fetch_sticker_art.py`, running `scripts/bake_stickers.py`, and
knowing which Lordicon slug to name. That is a developer's workflow attached
to what is really a taste decision, and taste decisions belong where the
person is already looking at the reel.

Measured, the whole chain is cheap: **0.4s to download an icon, 16.2s to matte
and grade both styles, and 5.3s to search-and-preview five candidates**. The
cost is not the work; it is knowing the slug.

## Decisions already taken

- **The trigger still fires the way it does now.** Which word earns a sticker,
  when it lands, where it sits, the cap, the minimum gap — none of that
  changes. The picker replaces the *art*, nothing else. This is the smallest
  change that solves the problem and it keeps every timing test meaningful.
- **A choice belongs to one reel.** Picking a different skull for this reel
  does not change the next one. Matches how clips already work.
- **It lives in the clips review screen**, the existing third gate.

## The constraint that fixes the placement

**A sticker cue cannot exist before the voice stage.** `find_cues` reads
`beat.words`, which `engine.media.voice.caption_timings` produces; a beat
without word timings gets no sticker, and there is a test pinning that.
Measured on a three-beat plan: **0 cues before voice, 3 after.**

So the picker cannot live on the script-approve screen — there would be
nothing to show. The clips review screen is the first place the cues exist,
and it is already the screen for checking what the video will look like.

## What automatic matching cannot do

The catalogue holds **3,578 icons** and the sitemap that lists them is
public, so searching it is easy. Choosing from it is not.

Searching `earth` returns exactly one hit: **`1195-earthworm`**, a worm. The
planet is filed under `globe` and `planet`. Searching `circle` returns 92
hits, of which `1414-circle` — the one whose name matches best — is a
quarter-circle wedge, not a circle.

That is why this is a picker and not an automatic swap. The person sees the
candidates and rejects the worm.

## Components

### `engine/assembly/sticker_catalog.py` (new)

The catalogue, and nothing else. Pure functions plus one cached download.

- `refresh(cache_dir) -> Path` — fetches
  `https://lordicon.com/sitemap-icons-wired.xml` (5.4MB) once and caches it.
- `search(term, catalog, limit) -> list[str]` — slugs matching a term, best
  first. A slug is `1234-some-name`; rank an exact word match in the name
  above a substring, and a shorter name above a longer one, so `27-globe`
  outranks `1547-globe-honeymoon` for `globe`.
- `gif_url(slug, variant="flat") -> str` — the same URL
  `scripts/fetch_sticker_art.py` already builds.

No engine module imports this at render time. Same rule as `sticker_art.py`.

### `engine/assembly/sticker_choices.py` (new)

Resolution, kept out of `stickers.py` because that file is already large.

- `choice_for(plan_id, trigger, store) -> str | None`
- `bake_key(slug, style, size, fps) -> str` — the content-addressed cache name.
- `cached_sequence(slug, style, fps, size, root) -> tuple | None` — the same
  shape `baked_sequence` returns.

### `engine/store.py` (changed)

A `sticker_choices` table: `plan_id`, `trigger`, `slug`, `created_at`, unique
on `(plan_id, trigger)`. Written by the choose route, read by `prepare`.

**Deliberately not a field on `ReelPlan`.** A chosen slug is a production
decision about one render, not part of the plan's creative content — and
`engine/contract.py` is being actively edited by another session, so widening
it invites a conflict for no benefit.

### `engine/assembly/stickers.py` (changed)

`prepare()` gains an optional `choices: dict[str, str] | None`. For each cue
it resolves, in order:

1. a chosen slug for this plan and trigger, from the bake cache;
2. the committed art under `engine/data/stickers/<trigger>/<style>/`;
3. the emoji glyph.

Rung 1 is new; rungs 2 and 3 are exactly today's behaviour. `prepare(plan,
settings)` with no `choices` behaves identically to now.

### `engine/app.py` (changed — two routes, mirroring the clip picker)

```
GET  /api/plan/{plan_id}/stickers                  candidates
POST /api/plan/{plan_id}/sticker/{trigger}         choose one
```

The GET returns one row per fired cue: the trigger, the word that fired it,
its time, which art is in use, and up to eight candidates each with a
**preview** — one matted, graded frame, not a bake.

The POST takes a slug, downloads it if the cache lacks it, bakes both styles,
writes the choice, and returns the new preview. It is the only slow call
(~17s) and it is the one the person explicitly asked for.

### `engine/ui/index.html` (changed)

A section in the clips review screen, following the existing clip rows:
per fired sticker, the current art and a row of candidate thumbnails; click
one to choose; a spinner while it bakes.

## Preview versus bake

Browsing must be cheap or nobody browses. A preview is **one frame** —
download (0.4s, cached after), matte, grade, resize — about 0.2s per icon
after the download. A bake is 42 frames × 2 styles, 16.2s, and happens only
on the click that commits.

## Fallback

Unchanged in spirit. Every new failure returns to the rung below rather than
raising:

- catalogue unreachable → no candidates, the screen says so, the committed art
  still renders;
- download or bake fails → the choice is not written, the previous art stands;
- a chosen slug whose cache was cleaned → falls through to committed art, then
  emoji.

A missing decoration must never cost a render, and now a missing *choice* must
never cost one either.

## Testing

Following this suite's habit: real behaviour, not mocks, and every guard test
proven to fail when the thing it guards is disabled.

1. `search("earth")` ranks `27-globe` and `1875-planet` above `1195-earthworm`
   — the ranking exists precisely because the naive match is wrong.
2. `search("globe")` puts `27-globe` first, ahead of `1547-globe-honeymoon`.
3. The catalogue is fetched once and reused; a second call does no network.
4. A chosen slug resolves through `prepare()` to the cached bake, with
   `baked=True` — so the Lordicon credit still holds.
5. `prepare(plan, settings)` with no choices is byte-identical to today.
6. A choice whose cache is deleted falls back to committed art, then emoji,
   and never raises.
7. Two plans choosing different slugs for the same trigger do not collide.
8. The bake cache is content-addressed: choosing the same slug twice does no
   second bake.
9. The choose route rejects a slug that is not in the catalogue, rather than
   downloading an arbitrary URL.

## Risks worth stating

- **The catalogue is a live URL.** It is fetched at *choose* time, never at
  render time, so a render still needs nothing external. But the picker stops
  working if Lordicon moves the sitemap, and the failure must read as "no
  candidates", not as a crash.
- **A picked icon can be wrong in ways a thumbnail hides.** The interior-white
  check that refuses holed icons runs at bake time; if it refuses, the choose
  route must say so plainly and keep the previous art rather than failing
  silently.
- **`engine/app.py` and `engine/ui/index.html` are contended.** Another
  session is committing to both. This work should land its backend first and
  its UI when that file is free.

## Out of scope

- Free-text search for a word no trigger matched. The trigger still decides
  *whether* a sticker fires; this only changes *what* it looks like.
- Changing which triggers exist, the cap, the gap, placement or timing.
- Promoting a per-reel choice to a global default.
- Cache eviction. The bake cache grows; at ~250KB per icon-style it will not
  matter for a long time, and a `--prune` flag is a later, separate job.

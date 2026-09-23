# Pro-level animated stickers

Replace the emoji-glyph sticker with a designed, continuously animated one
that is graded to the beat it lands on.

## Why

The current sticker is a Segoe UI Emoji glyph drawn by Pillow, scaled up past
its resting size and back down over 0.20s, then held **perfectly still for
1.4s**, then faded out over 0.18s. Three separate things make it read as
clipart pasted on the frame rather than as part of the edit:

1. **The art is a system icon.** It is the same glyph the viewer sees in
   WhatsApp. Nothing about it belongs to this channel.
2. **It is frozen for 70% of its life.** A sticker is on screen for 1.40s —
   `sticker_chain` trims to `HOLD_SECONDS` and gates `enable` to the same
   span. The 7-frame pop ends at 0.233s and the fade begins at 1.22s, so
   0.987 of those 1.40 seconds are a single unchanging picture. This is the
   largest single contributor, and it is true no matter what art is used.
3. **It is not grounded.** No shadow, no stroke, no glow, so it sits *on* the
   frame instead of *in* it.

A fourth, smaller problem: placement is three fixed slots in the upper third,
chosen without reference to what is in the shot.

## What was proven before writing this

Every load-bearing assumption here was measured on 2026-09-22/23, not assumed.

- **Assets can be fetched without a login.** `lordicon.com/sitemap.xml` fans
  out to `sitemap-icons-{wired,system,doodle}.xml`; the wired map is 5.4MB and
  lists every icon as `/icons/wired/{variant}/{slug}`. The rendered animation
  is then public at
  `https://media.lordicon.com/icons/wired/{variant}/{slug}.gif` — verified 200
  for all five chosen icons. This is the same URL Lordicon puts in its own
  `og:image`, so it is served for public consumption.
- **`.li` is off limits.** `…/{slug}.li` also returns 200, but the payload is
  an obfuscated container (106KB, all-printable, not gzip/zip/JSON). Decoding
  it would circumvent Lordicon's access control. The GIF route makes it
  unnecessary, so nothing in this design reads `.li`.
- **The GIFs carry no alpha** — `transparent=False`, one alpha level. They are
  drawn on pure white (60.3% of pixels on the sampled frame).
- **Flood-filling white from the edges recovers a clean matte.** On
  `2841-crashed-skull`, 96,918 near-white pixels were removed and **0** white
  pixels survived inside the icon, so the fill never punches holes in the art.
  This must be re-checked per icon, not assumed (see Risks).
- **184px is a workable size.** All five icons stay legible at the real
  on-screen size over a dark frame.
- **Source clips are 400×400, 51–101 frames, 2.54–4.06s.**

## Decisions already taken

- **Lordicon over LottieFiles.** LottieFiles' Simple License needs no
  attribution but its pages return 403 to every fetch attempt, so assets can
  only arrive by hand. Lordicon can be automated end to end; its free licence
  requires the credit line. Alex accepted the attribution.
- **Giphy rejected.** API is paid since March 2026 and 401s without a key,
  attribution is heavier ("Powered By GIPHY" plus per-creator credit), its
  terms discourage commercial use of the free tier, GIF's 1-bit alpha cannot
  produce a soft edge, and the library is full of third-party trademarked
  material.
- **GIF, not Lottie JSON.** This removes `rlottie-python` — whose wheels stop
  at cp312 and will not install on this project's Python 3.14 — and with it
  the whole second-interpreter problem. Pillow already decodes GIF and is
  already in `requirements.txt`. **This design adds no new dependency.**
- **Style follows `Beat.role`.** `reveal` and `twist` get the dark cinematic
  treatment; `hook` and `cta` get punchy. The field already exists on every
  beat, so no contract change is needed to carry it.

## The five triggers, and why these five

Chosen from the real corpus, not from the weights table: the trigger matcher
was run over all 113 captioned beats in the 10 stored plans.

| Trigger  | Fires | Concentrated in | Icon |
|----------|-------|-----------------|------|
| question | 11    | cta (8)         | `424-chat-question` |
| death    | 10    | hook (4)        | `2130-skull-poison` |
| science  | 9     | twist (6)       | `440-dna` |
| time     | 9     | reveal (7)      | `196-clock-arrow-rotate-left` |
| witness  | 7     | spread          | `2813-creepy-eye-ball` |

`water` fires most (19) but is deliberately excluded: 🌊 is tonally weak over
a frozen corpse lake, and at weight 4 it loses to `death` (10) whenever both
fire, so it rarely wins a slot anyway.

`ghost`, `shock`, `secret` and `danger` **never fired once** across 113 beats
despite high weights. They stay in the trigger map but get no art in this
round; they will fall back to the emoji path.

Art selection is a taste judgement and was made by looking at renders at
184px. `2841-crashed-skull` was rejected — pastel skull with a pink brain and
a mouse on it, tonally wrong. `dead-day-skull` (festive sugar skull),
`ghost-scary` (cute), `bone-hand` (manicured) were rejected for the same
reason. `2130-skull-poison` is a plain bone-white skull and crossbones.

## Components

### `engine/data/stickers.json` (changed, data only)

Each trigger gains an optional `art` object. A trigger without one keeps
working exactly as today.

```json
{ "name": "death", "emoji": "💀", "weight": 10, "match": [...],
  "art": { "source": "lordicon", "family": "wired",
           "variant": "flat", "slug": "2130-skull-poison" } }
```

`test_a_new_trigger_needs_no_code_change` must keep passing: adding a trigger
*with* art must also need no code change.

### `scripts/fetch_sticker_art.py` (new)

Reads `stickers.json`, downloads each `art` entry to
`assets/lordicon/<trigger>.gif`, skips what is already present, and verifies
each file decodes as a multi-frame GIF. Run by hand when art changes; never
during a render.

### `scripts/bake_stickers.py` (new)

The only place that knows about GIFs. For each trigger × style:

1. Decode every GIF frame to RGBA.
2. **Matte**: flood-fill near-white (all channels > 238) inward from the
   border; those pixels get alpha 0. Fail loudly if any near-white pixel
   survives *inside* the silhouette, because that icon would render with
   holes and must be swapped rather than shipped broken.
3. **Retime** to the on-screen window (below), not truncate.
4. **Style treatment** (below).
5. **Ground**: composite a soft drop shadow beneath the art.
6. Write `engine/data/stickers/<trigger>/<style>/frame-%03d.png` plus a
   `meta.json` recording source slug, licence, frame count and canvas.

Baked frames are committed. A render never needs network or Pillow-heavy work.

### `engine/assembly/stickers.py` (changed)

`render_pop_frames` keeps its emoji path as the fallback and gains a sibling
that returns the baked sequence for `(trigger, style)` when one exists.
`prepare()` resolves the style from the cue's beat role and passes it down.
`Sticker` gains `style: str`. Everything downstream — `sticker_inputs`,
`sticker_chain`, the audio pop pairing in `plan_sfx` — keeps its shape.

## The on-screen window, and retiming

Source animations run 2.54–4.06s; the sticker lives ~1.6s. Truncating would
cut them mid-motion, which looks like a glitch rather than a beat.

The baked sequence is therefore **resampled across the whole window** so the
source animation plays its complete arc in the time available:

```
WINDOW_SECONDS = HOLD_SECONDS  # 1.40, today's full visible life
frames_out     = round(WINDOW_SECONDS * fps)          # 42 at 30fps
src_index      = round(i * (n_src - 1) / max(frames_out - 1, 1))
```

`WINDOW_SECONDS` is `HOLD_SECONDS`, not `POP_SECONDS + HOLD_SECONDS`.
`sticker_chain` trims the input to `HOLD_SECONDS` and gates `enable` to
`start .. start + HOLD_SECONDS`, so 1.40s is the whole of a sticker's
visible life and a longer bake would simply have its tail trimmed away —
the animation would never reach its final frame, which is the failure this
change exists to remove.

So the visible duration is byte-identical to today's: this alters *what is
on screen*, never *how long*. Every timing test that passes now keeps
passing. The static hold is deleted outright. `FADE_OUT_SECONDS` stays, applied by the existing `fade`
filter, so the exit is unchanged.

`sticker_chain` no longer needs
`loop=loop=-1:size=1:start={frames-1}` — the sequence now covers the whole
window. Everything else in that fragment stays byte-for-byte, including
`settb=1/fps` on every label, `enable=between(...)`, and the rule that the
overlay never touches the MAIN branch's PTS. The narration-length invariant
is therefore untouched.

## Style treatment

Both styles start from the same matted frames, so art is fetched once.

**punchy** (`hook`, `cta`, and the default for any unmapped role)
- saturation ×1.15
- 3px outline in near-white at 85% opacity, to hold the shape against busy
  footage
- drop shadow: 6px down, 12px blur, 45% black

**dark** (`reveal`, `twist`)
- **darken first**: brightness x0.72. This is what makes it dark; desaturating
  and tinting alone can only make art pale and yellow, never darker.
- desaturate to 65% — enough to calm it, not so much that the icons lose the
  colour separation that makes them readable at 184px
- tint 10% toward `#FFD700`, the gold `captions.py` already highlights the
  spoken word with (`COLOUR_SPOKEN`). The channel has exactly one accent
  colour and the sticker should use it rather than introduce a second. Ten
  percent is a warmth, not a coat of paint.
- outer glow in the same gold, low opacity, to separate it from dark footage
  instead of an outline
- drop shadow: 4px down, 16px blur, 60% black

These three numbers were chosen by rendering the five shipped icons under five
candidate combinations and looking at them, not by taste. The first attempt
(35% saturation, no brightness change, 45% tint) collapsed all five to the same
flat yellow and made the eye's pupil vanish.

The treatment is also what makes the set cohere: as fetched, the skull is
bone-grey, DNA and clock are blue/orange, the eye is red/blue. Unifying them
happens here, once, at bake time. Punchy keeps more of the source colour on
purpose — `hook` and `cta` are where loudness earns its place.

## Placement

Keep the existing safe band (`ANCHOR_Y_FRACTION = 0.22`) and the three cycled
x-slots. These already keep the sticker clear of the caption block and of the
centre Punch style, and `test_the_sticker_sits_clear_of_the_burned_in_captions`
pins that.

Content-aware placement is **out of scope** — see below.

## Fallback

Unchanged in spirit, and it now has one more rung:

1. Baked frames for `(trigger, style)` → use them.
2. Baked frames missing → the existing emoji pop path.
3. No colour emoji font → `StickerUnavailable`, stickers are dropped, the
   render proceeds.

A missing decoration must never cost a render. `prepare()` keeps returning
`[]` rather than raising.

## Attribution

Lordicon's free licence requires a visible credit. The metadata the panel
emits for copy-out gains a fixed line:

```
Animated icons by Lordicon.com
```

It is emitted whenever at least one baked sticker was used, so the credit and
the thing it credits cannot drift apart. Tracked by a test.

## Testing

Existing sticker tests must keep passing unchanged, except the three that
encode the static hold. Specifically:

- `test_the_pop_overshoots_and_then_settles` and
  `test_the_pop_overshoots_on_screen_not_just_in_the_expression` describe the
  emoji path. They keep testing that path and gain baked-path siblings.
- `test_no_stickers_means_a_graph_identical_to_the_old_one` must still hold.

New tests, following this suite's existing habit of running real ffmpeg
rather than asserting on graph strings:

1. Flood-fill matte leaves zero near-white pixels inside the silhouette, for
   every shipped icon.
2. A baked sequence has exactly `round(WINDOW_SECONDS * fps)` frames.
3. **The sticker is not static**: sample the rendered video at its start,
   middle and end; consecutive sampled frames must differ inside the sticker
   box. This is the regression that protects the headline fix.
4. Role → style resolution, including the default for unmapped roles.
5. A trigger with no `art` still renders via the emoji path.
6. Deleting a baked directory falls back to emoji rather than failing.
7. The attribution line appears in metadata exactly when a baked sticker was
   used.
8. Render time with baked stickers stays within the current measured envelope
   — the existing design bakes frames precisely because `scale=…:eval=frame`
   cost +113%, and that must not regress.

## Risks worth stating

- **The white matte is per-icon.** It worked cleanly on the skull, but an
  icon with white *inside* it would come out holed. The bake step fails loudly
  on that rather than shipping it, and the fix is to choose another icon.
- **Retiming can look wrong.** Compressing a 4.06s animation into ~1.6s makes
  it play ~2.5× fast. If it reads as frantic, the honest fixes are a longer
  window or a slower-moving icon, not a hidden speed fudge.
- **Lordicon can change or withdraw an asset.** Baked frames are committed, so
  a render never depends on their CDN; only re-baking does.
- **Tone is a judgement, not a measurement.** The five renders looked right at
  184px on a dark frame. They have not been seen over real footage under real
  captions. The first full render is the actual test.

## Out of scope

- Content-aware placement (face/subject avoidance). Real work, needs a
  detector, and orthogonal to everything here.
- Expanding beyond five triggers. The remaining ten keep the emoji path.
- Art for `ghost`, `shock`, `secret`, `danger` — they never fire.
- Any change to trigger matching, the cap, the minimum gap, or the
  one-per-beat rule. Those work.
- Sticker sound effects. `plan_sfx` already pairs a pop with each sticker's
  start and needs no change.

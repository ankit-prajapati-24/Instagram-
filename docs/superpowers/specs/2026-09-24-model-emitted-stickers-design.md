# Stickers the script asks for

Let the model that writes the script also say where a sticker belongs and
what it should show, so the reel is not limited to fifteen mystery words
someone typed into a JSON file once.

## Why

`engine/data/stickers.json` holds fifteen triggers: `death`, `shock`,
`money`, `ghost`, `secret`, `fire`, `danger`, `witness`, `question`,
`science`, `night`, `water`, `mountain`, `location`, `time`. That is a
mystery channel's vocabulary, and the channel is not only doing mystery any
more.

Measured across the seventeen stored plans, thirteen of which have word
timings:

| Plan | Candidates | Chosen |
|---|---|---|
| a8836680 | 15 | 3 |
| fd5b853a | 9 | 3 |
| 05f4456d, 1ef1af98, ef1abe79, 6ada8633 | 8 | 3 |
| a0f410b5, 8f33970c | 7 | 3 |
| e25f5ef3, da61d5ca | 6 | 3 |
| f5bfe71c, 2fe4e7b8, 13861fc1 | **2** | **2** |

Eleven of thirteen spend the whole `DEFAULT_CAP` of three. The three that
produce two are the same script — a personal story about a girl who moves
to Indore for college, works four years, loses the placement, and leaves.
It is forty-three seconds long and exactly two of its words match the
trigger map:

```
beat 1: usne me medical university se padhai ki aur chaar *saal mehnat ki
beat 4: usne din *raat mehnat ki code likha seekha aur khud ko sabit kiya
```

Both are idioms, not moments. `din raat` means "constantly" and has nothing
to do with night, so the moon sticker it fires is simply wrong. Meanwhile
the story's actual peaks — the dream, the placement, the code, the
farewell, the memories — have no trigger at all, because no one thought to
type them in.

The trigger map cannot be extended into this. Every new genre would need
another list, and the lists would never finish. The model already knows
what its own script means.

## What the catalogue can and cannot find

Lordicon's wired set holds 3,578 icons. Searching it with words drawn from
the story above:

| Found nothing | Found something |
|---|---|
| `dream`, `journey`, `memory`, `farewell`, `goodbye`, `job`, `interview`, `resume`, `effort`, `strength`, `college`, `friendship`, `study`, `path`, `climb`, `reject`, `failure`, `luggage`, `suitcase` | `cloud`, `star`, `sleep`, `road`, `train`, `travel`, `photo`, `camera`, `album`, `briefcase`, `office`, `book`, `graduation`, `student`, `code`, `laptop`, `keyboard`, `heart`, `hug`, `muscle`, `fail`, `sad`, `broken` |

Every miss is an abstract noun. Every hit is a thing you could photograph.
The catalogue is named for objects, not ideas.

This is the single most important constraint on the prompt, and it is also
why one search term is not enough: `dream` finds nothing but `cloud` finds
an icon, and both mean the same beat. Asked for three or four concrete
terms per sticker, all ten concepts tested landed at least one hit.

It is also why `search` takes one word. It ranks against hyphen-separated
name parts, so a multi-word query like `moon night` matches nothing. The
model emits a list of single words, exactly as `Trigger.search` already
carries one.

## Decisions already taken

- **The model names the word, not just the concept.** A sticker's clock
  comes from `beat.words`, which `engine.media.voice.caption_timings`
  produces. Without a word to sit on there is no timing, and the pop lands
  nowhere in particular. Naming the word keeps every existing timing test
  meaningful.
- **One sticker per beat, and the 2.5-second gap stays.** `HOLD_SECONDS` is
  1.40 and `MIN_GAP_SECONDS` is 2.5, which is what guarantees two stickers
  are never on screen together. A ten-beat script can now carry up to ten
  stickers instead of three; it cannot carry two in one breath.
- **The cap goes.** `DEFAULT_CAP` and `settings.sticker_max` stop applying
  to model-emitted cues. They still bound the fallback path.
- **The trigger map stays, as the rung below.** It costs nothing to keep,
  it keeps all seventeen stored plans rendering exactly as they do now, and
  it is what a plan written before this change falls back on.
- **A choice belongs to one reel**, unchanged.
- **Each pick is chosen by hand** from candidates, unchanged. The engine
  does not auto-select.

## Components

### `engine/contract.py`

```python
class StickerCue(Coercing):
    word: str            # the caption word this sits on
    terms: list[str]     # English search terms, best first


class Beat(Coercing):
    ...
    on_screen_text: str | None = None
    sticker: StickerCue | None = None
```

`None` by default, so a stored plan written before this field existed
loads and renders unchanged.

### `engine/prompts/script.txt`

A new optional per-beat field, described beside `on_screen_text`:

> `sticker` : OPTIONAL. `word` must be a word that appears in this beat's
> `caption_text`, spelled as it appears there. `terms` are 3-4 concrete,
> photographable objects in English, best first. The icon library is named
> for things, not ideas: `dream`, `journey`, `memory`, `farewell` and
> `effort` all find nothing, while `cloud`, `road`, `photo`, `suitcase` and
> `muscle` all find something. Never emit an abstract noun.

The JSON example at the end of the prompt gains the field.

### `engine/authoring.py`, `engine/pipeline.py`, `engine/fake_client.py`

The three places that already coerce `on_screen_text` from model JSON into
a beat dict (`authoring.py:279`, `pipeline.py:402`, `fake_client.py:134`)
each gain the same handling for `sticker`.

### `engine/assembly/stickers.py`

`_candidates` gains a rung above the trigger scan. Per beat:

1. the beat carries a `sticker` — find its `word` in `beat.words` by the
   same `normalise` the trigger path uses, and take that timing. If the
   word is not there, place the cue at the beat's midpoint.
2. no `sticker` — scan the trigger map, as today.

`Cue` gains two fields:

- `beat_id: str` — the stable identity, alongside the existing
  `beat_index: int`. The index is a position and would move if beats were
  ever reordered; the id is what a stored choice is keyed on, so it is the
  one that must be carried rather than recomputed.
- `terms: tuple[str, ...]` — what the panel searches. Filled from the
  beat's `sticker.terms` on the model rung and from `Trigger.search` on the
  fallback rung, so the route that builds candidates reads one field and
  never needs to know which rung produced the cue.

`find_cues` applies the cap only to cues that came from the trigger map. A
plan whose beats are mixed — some carrying `sticker`, some not — keeps
every model-emitted cue and spends the cap on the trigger-map ones. The
2.5-second gap and the one-per-beat rule apply across both rungs together,
because they are about what the viewer sees, not about where the cue came
from.

`prepare()` resolves `choices` by beat id rather than trigger name.

### `engine/store.py`

The `sticker_choices` table's `trigger` column becomes `beat_id`, and the
primary key becomes `(plan_id, beat_id)`. `choose_sticker` and
`sticker_choices` follow.

The five rows currently in the table were all written today against `test`,
`my story` and a junk-topic plan. They are dropped rather than migrated: a
trigger name cannot be mapped back to a beat reliably, and the bakes those
rows point at stay in the cache, so re-picking any of them is instant.

### `engine/assembly/sticker_choices.py`

`ensure_baked` gains a `style` argument and bakes that one style. Today it
bakes both, because a trigger-keyed choice could be used by beats of
different roles and `style_for_role` could not be resolved in advance. A
beat-keyed choice has exactly one role, so exactly one style is needed —
about eight seconds per pick instead of sixteen.

### `engine/app.py`

- `GET /api/plan/{plan_id}/stickers` — rows keyed by `beat_id`, candidates
  searched from the beat's own `terms` rather than the trigger's.
- `POST /api/plan/{plan_id}/sticker/{beat_id}` — was `{trigger}`. Passes
  the beat's style to `ensure_baked`.
- `GET /api/sticker-search?term=...` — candidates for a term the person
  typed. The same catalogue membership rules as the rest of the picker.

### `engine/ui/index.html`

The existing sticker grid keys its cards on `beat_id` instead of `trigger`,
and each card gains a text box that searches the catalogue directly.

## Free-text search, and why it is in scope now

The previous picker spec put free-text search out of scope, on the grounds
that the trigger decided whether a sticker fired and the picker only changed
its art. That reasoning no longer holds. The model's terms can all miss —
every abstract noun in the table above does — and when they do, the person
is left with an emoji and no way to reach the 3,578 icons that are sitting
right there. A text box is what makes the miss recoverable.

## Fallback

Every new failure returns to the rung below rather than raising:

- no `sticker` on the beat → the trigger map;
- a `word` that is not in the caption → the beat's midpoint;
- every term finds nothing → no candidates, the emoji renders, the panel
  says so;
- catalogue unreachable → unchanged: a note, and the committed art still
  renders;
- a chosen slug whose bake was cleaned → committed art, then emoji.

A missing decoration must never cost a render.

## Testing

Real behaviour, not mocks. Every test that guards a specific defect is
proven by disabling the thing it guards, witnessing the failure, and
restoring — the evidence goes in the report, not a claim that it was done.

1. A beat carrying a `sticker` produces a cue whose start is the timing of
   the named word, without consulting the trigger map.
2. A beat with no `sticker` produces exactly the cues it produces today.
   The thirteen stored plans with word timings are byte-identical.
3. A `word` that does not appear in the caption places the cue at the
   beat's midpoint and raises nothing.
4. Two beats in one reel can hold two different chosen icons — the defect
   the trigger-name key causes today.
5. The cap does not apply to model-emitted cues: a ten-beat script yields
   ten stickers. The 2.5-second gap still does.
6. `ensure_baked` writes one style, not two.
7. `search("dream")` returns nothing and `search("cloud")` returns icons —
   the asymmetry the prompt's rule exists for.
8. A term typed into the search route returns catalogue slugs, and a slug
   that is not in the catalogue is refused before anything is fetched.
9. A mixed plan — some beats carrying `sticker`, some not — keeps every
   model-emitted cue, spends the cap only on the trigger-map ones, and
   still honours the 2.5-second gap between a cue from one rung and a cue
   from the other.

## Risks worth stating

- **The model may name a word that is not in its own caption.** The
  midpoint fallback keeps the sticker, but it lands loosely. How often this
  happens is not known until real scripts are generated, and it is the
  first thing to measure after the prompt lands.
- **The model may emit abstract terms despite the instruction.** The result
  is an empty candidate list, which the free-text box recovers. Worth
  counting across a few generated scripts to see whether the prompt needs
  sharpening.
- **Ten stickers is ten picks.** At roughly eight seconds a bake, a fully
  stickered reel costs around eighty seconds of picking. That is the
  chosen trade for hand-picking every one.
- **`engine/app.py` and `engine/ui/index.html` are contended.** Another
  session is committing to both. The backend should land first.

## Out of scope

- Auto-selecting an icon from the model's terms.
- Changing the pop curve, the hold, the placement or the slot cycling.
- Promoting a per-reel choice to a global default.
- Removing the trigger map.
- Cache eviction.

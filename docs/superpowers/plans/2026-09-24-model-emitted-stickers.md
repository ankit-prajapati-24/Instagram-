# Model-Emitted Stickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the model that writes a script name the word each sticker sits on and the terms its icon is searched by, so a reel is no longer limited to fifteen hardcoded mystery words.

**Architecture:** A new optional `sticker` field on `Beat` carries `word` and `terms`. `_candidates` gains a rung above the trigger scan that reads it; the trigger map stays underneath as the fallback for every plan written before this field existed. Stored choices move from being keyed by trigger name to being keyed by `beat_id`, which both fixes a real defect (two cues of the same trigger shared one icon) and makes the beat's style knowable in advance, halving the bake.

**Tech Stack:** Python 3.14, pydantic v2, FastAPI, SQLite, Pillow, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-model-emitted-stickers-design.md`

## Global Constraints

- `HOLD_SECONDS = 1.40` and `MIN_GAP_SECONDS = 2.5` are unchanged. The gap is what guarantees two stickers are never on screen together, and it applies across both rungs.
- One sticker per beat, across both rungs together.
- The cap (`DEFAULT_CAP = 3`, `settings.sticker_max`) applies **only** to cues that came from the trigger map. Model-emitted cues are never capped.
- `Beat.sticker` defaults to `None`. The seventeen stored plans must load and render byte-identically.
- A missing decoration must never cost a render. Every new failure returns to the rung below rather than raising.
- Search terms are single words. `sticker_catalog.search` ranks against hyphen-separated slug name parts, so a multi-word query matches nothing.
- `engine/ui/index.html` is **out of scope for this plan** — a concurrent session is actively rewriting it (184 lines in its last commit). Do not touch it.
- Before every commit, run `git status --porcelain` and stage only the files named in that task. Another session commits to this branch.
- Every test that guards a specific defect must be proven: disable the thing it guards, witness the FAIL, restore, witness the PASS. Put the evidence in the report, not a claim that it was done.

## Review Focus

1. **A beat carries `sticker` but has no word timings at all** (every beat before the voice stage). The midpoint fallback must NOT fire — it would create a cue before voice exists, breaking the invariant the picker's placement depends on. Pinned in Task 2.
2. **`terms` is empty, or every term finds nothing.** No candidates, the emoji renders, nothing raises. Pinned in Task 2 and Task 6.
3. **`word` appears more than once in the caption.** The first occurrence wins, deterministically. Pinned in Task 2.
4. **`word` is empty or whitespace.** Treated as not found — midpoint if the beat has timings, no cue if it does not. Pinned in Task 2.
5. **The search route's `term` is caller-controlled.** It must only ever be matched against catalogue slugs and must never become a filesystem path. Pinned in Task 6.

---

### Task 1: `StickerCue` on the contract, and the three places model JSON becomes a beat

**Files:**
- Modify: `engine/contract.py` (add `StickerCue` after `WordTiming` at line 85; add the field to `Beat` after `on_screen_text` at line 159)
- Modify: `engine/authoring.py:279`
- Modify: `engine/pipeline.py:397-408`
- Modify: `engine/fake_client.py:128-140`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `engine.contract.StickerCue` with fields `word: str` and `terms: list[str]`; `Beat.sticker: StickerCue | None = None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contract.py`:

```python
from engine.contract import Beat, StickerCue


def _beat(**extra):
    base = {
        "beat_id": "b1", "role": "setup",
        "voice_text": "रात में कोड लिखा",
        "caption_text": "raat mein code likha",
        "visual_prompt": "a dark desk lit by one screen",
        "motion": "zoom_in", "transition": "fade",
        "target_seconds": 4.4,
    }
    base.update(extra)
    return base


def test_a_beat_carries_the_sticker_the_script_asked_for():
    beat = Beat.model_validate(_beat(
        sticker={"word": "code", "terms": ["code", "laptop", "keyboard"]}))
    assert beat.sticker is not None
    assert beat.sticker.word == "code"
    assert beat.sticker.terms == ["code", "laptop", "keyboard"]


def test_a_beat_without_a_sticker_is_still_a_beat():
    """Every stored plan predates this field. None must mean 'use the
    trigger map', not 'this plan is invalid'."""
    beat = Beat.model_validate(_beat())
    assert beat.sticker is None


def test_an_empty_terms_list_is_accepted():
    """The model can name a word and give no usable term. That is a beat
    with no candidates, not a malformed beat."""
    beat = Beat.model_validate(_beat(sticker={"word": "code", "terms": []}))
    assert beat.sticker.terms == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_contract.py -k sticker -v`
Expected: FAIL — `ImportError: cannot import name 'StickerCue'`.

- [ ] **Step 3: Add the model and the field**

In `engine/contract.py`, immediately after the `WordTiming` class (line 85):

```python
class StickerCue(Coercing):
    """What the script asked for at one beat: a word to sit on, and what
    the icon should show.

    ``word`` is a word from this beat's ``caption_text``, which is where the
    timings come from -- a sticker with no word has no clock. ``terms`` are
    English, and single words, because ``sticker_catalog.search`` ranks
    against hyphen-separated slug name parts and a multi-word query matches
    nothing. They are a list rather than one term because the catalogue is
    named for objects and not for ideas: "dream" finds nothing and "cloud"
    finds an icon, and both mean the same beat.
    """

    word: str
    terms: list[str] = Field(default_factory=list)
```

`Coercing` is defined above at line 56 and `Field` is already imported at line 20.

In `Beat`, directly after `on_screen_text` (line 159):

```python
    # What the script asked for at this beat, or None to fall back to the
    # trigger map in engine/data/stickers.json. None on every plan stored
    # before this field existed, which is why the fallback stays.
    sticker: StickerCue | None = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_contract.py -k sticker -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Carry the field through `engine/authoring.py`**

This is the path model JSON takes. At line 279 the beat dict is built; `_text` is the module's own coercion helper. Replace:

```python
        beat["on_screen_text"] = _text(item.get("on_screen_text")) or None
```

with:

```python
        beat["on_screen_text"] = _text(item.get("on_screen_text")) or None
        beat["sticker"] = _sticker(item.get("sticker"))
```

and add this helper beside the other module-level helpers in the same file:

```python
def _sticker(raw) -> dict | None:
    """The beat's sticker request, or None when it asked for none.

    Tolerant on purpose: a model that returns a bare string, drops
    ``terms``, or returns them as one comma-joined string is asking for a
    sticker and should get one. Only a request with no word at all is
    nothing, because a sticker with no word has no clock.
    """
    if not isinstance(raw, dict):
        return None
    word = _text(raw.get("word"))
    if not word:
        return None
    terms = raw.get("terms")
    if isinstance(terms, str):
        terms = terms.split(",")
    if not isinstance(terms, (list, tuple)):
        terms = []
    clean = [t for t in (_text(term).lower() for term in terms) if t]
    return {"word": word, "terms": clean}
```

- [ ] **Step 6: Carry the field through `engine/pipeline.py`**

This is the manual-rows path. In the `Beat.model_validate({...})` block at lines 397-408, add one key after `on_screen_text`:

```python
            "sticker": ({"word": str(row["sticker_word"]).strip(),
                         "terms": [t.strip().lower()
                                   for t in str(row.get("sticker_terms") or
                                                "").split(",") if t.strip()]}
                        if str(row.get("sticker_word") or "").strip()
                        else None),
```

- [ ] **Step 7: Make the fake client emit one, so the suite exercises it**

In `engine/fake_client.py`, the beats are built at lines 128-140. Add a module-level constant beside `SAMPLE_BEATS`:

```python
# One beat carries a sticker request, so every test that runs the fake
# through the pipeline exercises the model rung rather than only the
# trigger map. Concrete nouns, because the catalogue is named for objects.
SAMPLE_STICKERS = {2: {"word": "raat", "terms": ["moon", "star", "night"]}}
```

and one key inside the appended dict, after `"on_screen_text": punch,`:

```python
            "sticker": SAMPLE_STICKERS.get(index),
```

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. Nothing else reads `Beat.sticker` yet, so this step is proving the field broke nothing.

- [ ] **Step 9: Commit**

```bash
git status --porcelain
git add engine/contract.py engine/authoring.py engine/pipeline.py engine/fake_client.py tests/test_contract.py
git commit -m "feat: a beat can carry the sticker its script asked for"
```

---

### Task 2: The model rung in `_candidates`, and a cap that only binds the trigger map

**Files:**
- Modify: `engine/assembly/stickers.py` (`Cue` at lines 177-187, `_candidates` at lines 194-229, `find_cues` at lines 272-297)
- Test: `tests/test_stickers.py`

**Interfaces:**
- Consumes: `Beat.sticker: StickerCue | None` from Task 1.
- Produces: `Cue` with three new fields — `beat_id: str`, `terms: tuple[str, ...]`, `source: str` (`"model"` or `"trigger"`). `find_cues(plan, triggers=None, *, cap=DEFAULT_CAP, min_gap=MIN_GAP_SECONDS)` keeps its signature.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stickers.py`. The file already has `_timed(beats, measured, captions)`, which builds a plan whose beats carry caption-aligned word timings, and imports `stickers as stk`.

```python
# --- the script asks for its own stickers ---------------------------------

def test_the_script_can_ask_for_a_sticker_the_trigger_map_has_never_heard_of():
    plan = _timed(beats=4, captions=[
        "usne din raat mehnat ki code likha",
        "college ki placement sthiti kharab thi",
        "woh naukri nahi bas yaadein lekar ja rahi thi",
        "kabhi manzil nahi milti safar badal deta hai"])
    plan.script.beats[1].sticker = StickerCue(
        word="placement", terms=["briefcase", "office"])
    cue = next(c for c in stk.find_cues(plan) if c.beat_index == 1)
    assert cue.source == "model"
    assert cue.terms == ("briefcase", "office")
    assert cue.beat_id == plan.script.beats[1].beat_id
    timing = next(t for t in plan.script.beats[1].words
                  if t.word == "placement")
    assert cue.start == pytest.approx(
        plan.script.beats[0].seconds() + timing.start)


def test_a_beat_with_no_word_timings_gets_no_sticker_even_when_asked():
    """The picker lives on the clips screen because a cue cannot exist
    before the voice stage. A midpoint fallback that fires on a beat with
    no timings would put stickers on the script screen and break that."""
    plan = make_plan(beats=3, measured=4.0)
    for beat in plan.script.beats:
        assert not beat.words
    plan.script.beats[0].sticker = StickerCue(word="code", terms=["laptop"])
    assert stk.find_cues(plan) == []


def test_a_word_that_is_not_in_the_caption_lands_at_the_beat_midpoint():
    plan = _timed(beats=2, measured=4.0,
                  captions=["ek ladki college gayi", "usne code likha"])
    plan.script.beats[1].sticker = StickerCue(word="rocket", terms=["rocket"])
    cue = next(c for c in stk.find_cues(plan) if c.beat_index == 1)
    assert cue.start == pytest.approx(
        plan.script.beats[0].seconds() + plan.script.beats[1].seconds() / 2)


def test_an_empty_word_is_treated_as_not_found():
    plan = _timed(beats=2, measured=4.0,
                  captions=["ek ladki college gayi", "usne code likha"])
    plan.script.beats[1].sticker = StickerCue(word="   ", terms=["laptop"])
    cue = next(c for c in stk.find_cues(plan) if c.beat_index == 1)
    assert cue.start == pytest.approx(
        plan.script.beats[0].seconds() + plan.script.beats[1].seconds() / 2)


def test_a_repeated_word_takes_its_first_occurrence():
    plan = _timed(beats=1, measured=6.0,
                  captions=["code likha phir code chala phir code ruka"])
    plan.script.beats[0].sticker = StickerCue(word="code", terms=["code"])
    first = plan.script.beats[0].words[0]
    assert first.word == "code"
    cue = stk.find_cues(plan)[0]
    assert cue.start == pytest.approx(first.start)


def test_empty_terms_still_produce_a_cue():
    """No candidates to offer is not the same as no sticker. The emoji
    still renders."""
    plan = _timed(beats=2, measured=4.0,
                  captions=["ek ladki college gayi", "usne code likha"])
    plan.script.beats[1].sticker = StickerCue(word="code", terms=[])
    cue = next(c for c in stk.find_cues(plan) if c.beat_index == 1)
    assert cue.terms == ()


def test_the_cap_does_not_bind_the_scripts_own_stickers():
    """DEFAULT_CAP is three. Ten beats that each ask for one get ten."""
    captions = [f"beat {n} ka andar code likha gaya tha yahan" for n in range(10)]
    plan = _timed(beats=10, measured=4.0, captions=captions)
    for beat in plan.script.beats:
        beat.sticker = StickerCue(word="code", terms=["laptop"])
    assert len(stk.find_cues(plan, cap=3)) == 10


def test_the_gap_still_holds_between_the_two_rungs():
    """A model cue and a trigger cue are two things the viewer sees, so
    the 2.5s gap is about both of them together.

    The script asks for a sticker on the last word of beat 0; the trigger
    map finds `raat` on the first word of beat 1. They are far closer than
    the gap, so exactly one survives.
    """
    plan = _timed(beats=2, measured=4.0, captions=[
        "ek ladki thi jisne likha code",
        "raat bhar wo jaagti rahi thi yahan"])
    plan.script.beats[0].sticker = StickerCue(word="code", terms=["laptop"])

    # Precondition, asserted rather than assumed: with the gap switched off
    # both rungs fire and they land closer together than MIN_GAP_SECONDS.
    # Without this the assertion below would pass just as happily on a plan
    # that only ever produced one cue.
    both = stk.find_cues(plan, min_gap=0.0)
    assert {c.source for c in both} == {"model", "trigger"}
    starts = sorted(c.start for c in both)
    assert starts[1] - starts[0] < stk.MIN_GAP_SECONDS

    cues = stk.find_cues(plan)
    assert [c.source for c in cues] == ["model"], \
        "the trigger cue is inside the gap and must lose to the script's own"


def test_a_beat_that_asks_is_never_also_scanned_for_triggers():
    """One sticker per beat. The script's request wins its own beat."""
    plan = _timed(beats=1, measured=5.0,
                  captions=["raat ke waqt code likha gaya"])
    plan.script.beats[0].sticker = StickerCue(word="code", terms=["laptop"])
    cues = stk.find_cues(plan)
    assert len(cues) == 1
    assert cues[0].source == "model"
```

Add `from engine.contract import StickerCue` to the imports at the top of the file, and `make_plan` is already imported from `tests.factories`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stickers.py -k "script_can_ask or no_word_timings or beat_midpoint or empty_word or first_occurrence or empty_terms or cap_does_not_bind or two_rungs or never_also_scanned" -v`
Expected: FAIL — `AttributeError: 'Cue' object has no attribute 'source'` and friends.

- [ ] **Step 3: Widen `Cue`**

In `engine/assembly/stickers.py`, replace the `Cue` dataclass (lines 177-187):

```python
@dataclass(frozen=True)
class Cue:
    """A moment that earns a sticker, placed on the render timeline."""

    name: str
    emoji: str
    word: str
    beat_index: int
    start: float           # absolute seconds from the first frame
    weight: int
    # The stable identity. ``beat_index`` is a position and would move if
    # beats were ever reordered; a stored choice is keyed on this, so it is
    # carried rather than recomputed.
    beat_id: str = ""
    # What the panel searches the catalogue with. Filled from the beat's
    # ``sticker.terms`` on the model rung and from ``Trigger.search`` on the
    # fallback rung, so the route that builds candidates reads one field and
    # never has to know which rung produced the cue.
    terms: tuple[str, ...] = ()
    # "model" when the script asked for this, "trigger" when the map found
    # it. The cap binds only the second kind.
    source: str = "trigger"
```

- [ ] **Step 4: Add the model rung to `_candidates`**

Replace `_candidates` (lines 194-229):

```python
def _candidates(plan: ReelPlan, triggers: list[Trigger],
                total: float) -> list[Cue]:
    cues: list[Cue] = []
    offset = 0.0
    for index, beat in enumerate(plan.script.beats):
        # No word timings means no clock, and a sticker without a clock is
        # worse than no sticker. Beats before the voice stage have none --
        # including a beat whose script asked for a sticker, which is why
        # the midpoint fallback below sits inside this guard and not
        # outside it. A cue that could exist before the voice stage would
        # put stickers on the script screen, where the picker cannot live.
        if not beat.words:
            offset += beat.seconds()
            continue

        asked = getattr(beat, "sticker", None)
        if asked is not None:
            start = _model_start(beat, asked, offset)
            if start + POP_SECONDS <= total:
                cues.append(Cue(
                    name=beat.beat_id, emoji=DEFAULT_EMOJI, word=asked.word,
                    beat_index=index, start=start, weight=0,
                    beat_id=beat.beat_id,
                    terms=tuple(t for t in asked.terms if t),
                    source="model"))
            # One sticker per beat: a beat that asked does not also get
            # scanned for triggers.
            offset += beat.seconds()
            continue

        for timing in beat.words:
            token = normalise(timing.word)
            if not token:
                continue
            for trigger in triggers:
                if not _hits(token, trigger):
                    continue
                start = offset + timing.start
                # It needs room to finish popping before the file ends.
                if start + POP_SECONDS > total:
                    continue
                cues.append(Cue(
                    name=trigger.name, emoji=trigger.emoji,
                    word=timing.word, beat_index=index, start=start,
                    weight=trigger.weight, beat_id=beat.beat_id,
                    terms=trigger.search, source="trigger"))
                break
        offset += beat.seconds()
    return cues
```

Add the helper directly above `_candidates`:

```python
def _model_start(beat, asked, offset: float) -> float:
    """When the sticker the script asked for lands.

    The named word's own timing when that word is in the caption, and the
    beat's midpoint when it is not. The midpoint is a placement, not a
    failure: the model naming a word its caption does not contain should
    cost a looser landing, never the sticker.
    """
    wanted = normalise(asked.word)
    if wanted:
        for timing in beat.words:
            if normalise(timing.word) == wanted:
                return offset + timing.start
    return offset + beat.seconds() / 2
```

and the emoji constant beside `DEFAULT_FONT` near line 51:

```python
# What a model-asked sticker falls back to when its terms find no icon and
# nothing was picked in the panel. The trigger map's own concepts each carry
# a glyph; the script's do not, so they share this one.
DEFAULT_EMOJI = "✨"  # sparkles
```

- [ ] **Step 5: Split the cap out of `find_cues`**

Replace the body of `find_cues` (lines 280-297) below its docstring:

```python
    triggers = load_triggers() if triggers is None else triggers
    total = sum(beat.seconds() for beat in plan.script.beats)
    everything = _candidates(plan, triggers, total)

    chosen: list[Cue] = []

    def _fits(cue: Cue) -> bool:
        # One per beat, and never two on screen together. Both rules are
        # about what the viewer sees, so both apply across the two rungs
        # together rather than within each one.
        if any(c.beat_index == cue.beat_index for c in chosen):
            return False
        return not any(abs(c.start - cue.start) < min_gap for c in chosen)

    # The script's own stickers first, in timeline order, uncapped. The cap
    # exists to stop a word list firing on everything it happens to match;
    # a model that asked for this beat specifically has already made that
    # judgement.
    for cue in sorted((c for c in everything if c.source == "model"),
                      key=lambda c: c.start):
        if _fits(cue):
            chosen.append(cue)

    # Then the trigger map spends what the cap allows, strongest first.
    taken = 0
    for cue in sorted((c for c in everything if c.source == "trigger"),
                      key=lambda c: (-c.weight, c.start)):
        if taken >= cap:
            break
        if _fits(cue):
            chosen.append(cue)
            taken += 1

    return sorted(chosen, key=lambda c: c.start)
```

Delete the `if cap <= 0: return []` guard at line 280. `cap=0` still yields `[]` for a plan with no model stickers, which is what `tests/test_stickers.py:132` asserts; for a plan that has them it now yields those, which is the point of the change.

Update the docstring to say so:

```python
    """The moments that earn a sticker, in timeline order.

    Two rungs. A beat whose script asked for a sticker gets that one, and
    is not also scanned for triggers. Every other beat is matched against
    the trigger map, strongest first, so ``cap`` is spent on the biggest
    moments rather than on whichever trigger came first.

    ``cap`` binds only the second rung. A script that asks for ten
    stickers gets ten.
    """
```

- [ ] **Step 6: Run the new tests, then the whole file**

Run: `python -m pytest tests/test_stickers.py -v`
Expected: PASS, including the nine new tests and every test that was already there.

- [ ] **Step 7: Prove the guard tests bite**

For each of these, make the edit, run the named test, record the failure, then revert:

| Disable | Test that must FAIL |
|---|---|
| Move the `if not beat.words` guard to after the `asked` block | `test_a_beat_with_no_word_timings_gets_no_sticker_even_when_asked` |
| Make `_model_start` always return `offset + beat.seconds() / 2` | `test_the_script_can_ask_for_a_sticker_the_trigger_map_has_never_heard_of` |
| Make `_fits` ignore `min_gap` | `test_the_gap_still_holds_between_the_two_rungs` |
| Apply `taken >= cap` to the model loop as well | `test_the_cap_does_not_bind_the_scripts_own_stickers` |

Record the four failure messages in the report.

- [ ] **Step 8: Prove the seventeen stored plans are unchanged**

```bash
python - <<'PY'
from engine.store import Store
from engine.assembly import stickers as S
st = Store("engine.db"); st.init()
for r in st.list_plans(limit=100):
    plan = st.get_plan(r["plan_id"])
    if plan is None: continue
    cues = S.find_cues(plan)
    print(r["plan_id"][:8], len(cues), [ (c.name, round(c.start,2)) for c in cues ])
PY
```

Expected, unchanged from the spec's table: eleven plans with 3 cues, three with 2 (`f5bfe71c`, `2fe4e7b8`, `13861fc1` → `time`/`night`), four with none. Paste the output into the report.

- [ ] **Step 9: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain
git add engine/assembly/stickers.py tests/test_stickers.py
git commit -m "feat: let the script's own sticker request outrank the word list"
```

---

### Task 3: Choices keyed by beat, not by trigger name

**Files:**
- Modify: `engine/store.py` (SCHEMA at lines 63-69, `choose_sticker` at line 372, `sticker_choices` at line 388)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Store.choose_sticker(plan_id: str, beat_id: str, slug: str) -> None` and `Store.sticker_choices(plan_id: str) -> dict[str, str]` returning `{beat_id: slug}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_two_beats_in_one_reel_can_wear_different_icons(tmp_path):
    """Keyed by trigger name, a reel's two `water` cues shared one icon.
    Keyed by beat, they do not."""
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p1", "b7", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "27-globe",
                                           "b7": "1875-planet"}


def test_choosing_again_for_one_beat_replaces_it(tmp_path):
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p1", "b3", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "1875-planet"}


def test_choices_do_not_leak_between_plans(tmp_path):
    store = Store(tmp_path / "t.db"); store.init()
    store.choose_sticker("p1", "b3", "27-globe")
    store.choose_sticker("p2", "b3", "1875-planet")
    assert store.sticker_choices("p1") == {"b3": "27-globe"}
    assert store.sticker_choices("p2") == {"b3": "1875-planet"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_store.py -k sticker -v`
Expected: FAIL — the second call in the first test overwrites the first, because the primary key is `(plan_id, trigger)` and both rows carry the same trigger value.

- [ ] **Step 3: Change the schema and the two methods**

In `engine/store.py`, the `sticker_choices` table at lines 63-69:

```sql
CREATE TABLE IF NOT EXISTS sticker_choices (
  plan_id TEXT NOT NULL,
  beat_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, beat_id)
);
```

`choose_sticker` at line 372:

```python
    def choose_sticker(self, plan_id: str, beat_id: str, slug: str) -> None:
        """Record which Lordicon icon this reel should use at this beat.

        Per beat, not per trigger: ``find_cues`` already allows one sticker
        per beat, so the beat id is the cue's identity on both rungs. Keyed
        by trigger name, a reel whose script said "water" twice could only
        ever wear one icon for both.

        Per plan on purpose: the same beat can wear different art in
        different reels, the way clips already do. Replacing rather than
        appending, because there is one answer per beat per reel and a
        history of rejected picks would only have to be filtered out again.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sticker_choices(plan_id, beat_id, slug, "
                "created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(plan_id, beat_id) DO UPDATE SET "
                "slug=excluded.slug, created_at=excluded.created_at",
                (plan_id, beat_id, slug, _now()))

    def sticker_choices(self, plan_id: str) -> dict[str, str]:
        """``{beat_id: slug}`` for this plan, empty when nothing was chosen."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT beat_id, slug FROM sticker_choices WHERE plan_id=?",
                (plan_id,)).fetchall()
        return {row["beat_id"]: row["slug"] for row in rows}
```

- [ ] **Step 4: Drop the five stale rows from the working database**

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so `engine.db` still has the old shape. A trigger name cannot be mapped back to a beat, so the five rows are dropped rather than migrated — all five were written today against `test`, `my story` and a junk-topic plan, and the bakes they point at stay in the cache, so re-picking any of them is instant.

The rows are written to a backup file first. A `DROP TABLE` cannot be undone,
and five rows of someone's afternoon are not worth an irreversible step when
making it reversible costs three lines.

```bash
python - <<'PY'
import json, sqlite3
from pathlib import Path
c = sqlite3.connect("engine.db")
c.row_factory = sqlite3.Row
cols = {r[1] for r in c.execute("PRAGMA table_info(sticker_choices)")}
if "beat_id" in cols:
    print("already migrated")
else:
    rows = [dict(r) for r in c.execute("SELECT * FROM sticker_choices")]
    out = Path(".superpowers/sdd/2026-09-24-model-emitted-stickers"
               "/sticker_choices-backup.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"backed up {len(rows)} rows to {out}")
    c.execute("DROP TABLE sticker_choices")
    c.commit()
    print("dropped; Store.init() will recreate it with the new shape")
PY
python -c "from engine.store import Store; s=Store('engine.db'); s.init(); print(s.sticker_choices('05f4456d'))"
```

Expected: `dropped; ...` then `{}`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store.py -k sticker -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Prove the first test bites**

Change the primary key back to `(plan_id, trigger)` shape by renaming the column to `trigger` in the schema and both methods, run `test_two_beats_in_one_reel_can_wear_different_icons` against a fresh `tmp_path` database, record the failure, then restore. This is the defect the task exists to fix, so it must be witnessed.

- [ ] **Step 7: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain
git add engine/store.py tests/test_store.py
git commit -m "fix: a chosen sticker belongs to a beat, not to a trigger name"
```

---

### Task 4: `prepare()` resolves a choice by beat

**Files:**
- Modify: `engine/assembly/stickers.py` (`prepare` at lines 487-570, the `chosen` lookup at line 530)
- Test: `tests/test_sticker_choices.py`

**Interfaces:**
- Consumes: `Cue.beat_id` from Task 2; `Store.sticker_choices -> {beat_id: slug}` from Task 3.
- Produces: `prepare(plan, settings, *, cache_dir=None, choices=None)` where `choices` is `{beat_id: slug}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sticker_choices.py`:

```python
def test_two_beats_resolve_their_own_chosen_icons(tmp_path, monkeypatch):
    """The defect the beat key fixes, proven through prepare() rather than
    through the store: two cues, two different bakes, both used."""
    plan = _timed(beats=4, measured=4.0, captions=[
        "ek ladki college gayi thi",
        "usne raat bhar code likha",
        "ek aur line yahan par hai",
        "phir usne paani piya tha"])
    plan.script.beats[1].sticker = StickerCue(word="code", terms=["laptop"])
    plan.script.beats[3].sticker = StickerCue(word="paani", terms=["water"])

    settings = shipped_settings(work_dir=str(tmp_path), stickers=True)
    size = stk.sticker_size(settings.width, settings.sticker_scale)
    fps = int(settings.fps)
    root = sticker_choices.cache_root(settings)
    for slug, beat in (("27-globe", plan.script.beats[1]),
                       ("1875-planet", plan.script.beats[3])):
        style = stk.style_for_role(beat.role)
        _fake_bake(root, slug, style, size=size, fps=fps)

    prepared = stk.prepare(plan, settings, choices={
        plan.script.beats[1].beat_id: "27-globe",
        plan.script.beats[3].beat_id: "1875-planet"})

    patterns = {s.beat_index: s.pattern for s in prepared}
    assert sticker_choices.bake_key(
        "27-globe", stk.style_for_role(plan.script.beats[1].role),
        size, fps) in patterns[1]
    assert sticker_choices.bake_key(
        "1875-planet", stk.style_for_role(plan.script.beats[3].role),
        size, fps) in patterns[3]
    assert all(s.baked for s in prepared)
```

Add the bake fixture helper to the same file if it is not already there:

```python
def _fake_bake(root, slug, style, *, size, fps):
    """Write a bake of the right shape without running Pillow.

    ``cached_sequence`` validates fps, size and frame count, so the cheapest
    honest fixture is the right number of correctly sized PNGs.
    """
    from PIL import Image
    frames = round(stk.HOLD_SECONDS * fps)
    canvas = stk.sticker_canvas(size)
    folder = (Path(root) / sticker_choices.BAKES_DIRNAME
              / sticker_choices.bake_key(slug, style, size, fps))
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(frames):
        Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0)).save(
            folder / f"frame-{n:03d}.png")
    return folder
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_sticker_choices.py -k two_beats_resolve -v`
Expected: FAIL — `prepare` looks up `choices[cue.name]`, and `cue.name` is now the beat id on the model rung but the trigger name on the fallback rung, so the lookup is inconsistent and at least one beat falls through to the emoji.

- [ ] **Step 3: Change the lookup**

In `engine/assembly/stickers.py`, inside `prepare`'s loop (line 530), replace:

```python
        chosen = (choices or {}).get(cue.name)
```

with:

```python
        # By beat, not by name: ``cue.name`` is a trigger on the fallback
        # rung and a beat id on the model rung, and a reel's two cues of one
        # trigger used to be unable to wear two different icons.
        chosen = (choices or {}).get(cue.beat_id)
```

and update `prepare`'s docstring where it describes `choices`:

```python
    ``choices`` maps a beat id to a Lordicon slug someone picked for this
    reel in the panel. It is the top rung: a chosen icon beats the committed
    art, which beats the emoji glyph. A slug whose bake is missing falls
    through to the rung below rather than failing, so cleaning the cache
    costs a nicer sticker and never a render.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_sticker_choices.py -v`
Expected: PASS.

- [ ] **Step 5: Prove it bites**

Revert the lookup to `choices.get(cue.name)`, run `test_two_beats_resolve_their_own_chosen_icons`, record the failure, restore.

- [ ] **Step 6: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain
git add engine/assembly/stickers.py tests/test_sticker_choices.py
git commit -m "fix: resolve a reel's chosen sticker by beat"
```

---

### Task 5: Bake one style, not two

**Files:**
- Modify: `engine/assembly/sticker_choices.py` (`ensure_baked` at lines 169-190)
- Modify: `engine/app.py` (the single `ensure_baked` call in `choose_sticker`, around line 2097) — one line, so this task leaves no broken commit behind
- Test: `tests/test_sticker_choices.py`

**Interfaces:**
- Consumes: `engine.assembly.stickers.style_for_role(role) -> str` (already exists at line 104; returns `"dark"` for `reveal` and `twist`, `"punchy"` otherwise).
- Produces: `ensure_baked(slug: str, *, root: Path, size: int, fps: int, style: str) -> None`. `style` is required — there is no default, because a caller that does not know the style is a caller that should not be baking.

- [ ] **Step 1: Write the failing test**

```python
def test_only_the_style_the_beat_needs_is_baked(tmp_path, monkeypatch):
    """A bake is ~8s a style. Keyed by trigger, both had to be made because
    the role was not knowable; keyed by beat, one is."""
    from engine.assembly import sticker_art
    made = []

    def _fake_bake_one(source, folder, *, style, size, fps):
        made.append(style)
        _fake_bake_into(folder, size=size, fps=fps)

    monkeypatch.setattr("scripts.bake_stickers.bake_one", _fake_bake_one)
    monkeypatch.setattr(sticker_choices, "_source",
                        lambda slug, root: tmp_path / "src.gif")
    (tmp_path / "src.gif").write_bytes(b"GIF89a")

    sticker_choices.ensure_baked("27-globe", root=tmp_path, size=220,
                                 fps=30, style="dark")
    assert made == ["dark"], f"baked {made}, wanted only the one needed"
    assert set(sticker_art.STYLES) == {"punchy", "dark"}, \
        "this test is only meaningful while there is another style to skip"
```

`_fake_bake_into` is the frame-writing half of Task 4's `_fake_bake`. Put
it at module level in `tests/test_sticker_choices.py` and have `_fake_bake`
call it, so the PNG-writing lives in one place:

```python
def _fake_bake_into(folder, *, size, fps):
    """Write a bake of the right shape without running Pillow's real work.

    ``cached_sequence`` validates fps, size and frame count, so the cheapest
    honest fixture is the right number of correctly sized transparent PNGs.
    """
    from PIL import Image
    frames = round(stk.HOLD_SECONDS * fps)
    canvas = stk.sticker_canvas(size)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(frames):
        Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0)).save(
            folder / f"frame-{n:03d}.png")
    return folder
```

and Task 4's helper becomes:

```python
def _fake_bake(root, slug, style, *, size, fps):
    return _fake_bake_into(
        Path(root) / sticker_choices.BAKES_DIRNAME
        / sticker_choices.bake_key(slug, style, size, fps),
        size=size, fps=fps)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_sticker_choices.py -k only_the_style -v`
Expected: FAIL — `made == ["punchy", "dark"]`, and `ensure_baked() got an unexpected keyword argument 'style'`.

- [ ] **Step 3: Take the style as an argument**

Replace `ensure_baked` (lines 169-190):

```python
def ensure_baked(slug: str, *, root: Path, size: int, fps: int,
                 style: str) -> None:
    """Bake one style of one icon into the cache, if it is not there.

    ``style`` is required. This used to bake every style in ``STYLES``,
    because a choice keyed by trigger name could be used by beats of
    different roles and ``style_for_role`` could not be resolved in advance.
    A choice keyed by a beat has exactly one role and therefore exactly one
    style, which halves the wait on the click that commits.

    Raises ``ValueError`` when the icon has a pocket of trapped white --
    ``bake_one``'s own refusal, passed straight through, because an icon
    that would render with a white blob in it is a choice to reject rather
    than a failure to swallow.
    """
    # Guarded, not unconditional: this runs on every call, from a
    # long-lived server process where an unconditional insert would grow
    # sys.path by one duplicate entry per request forever.
    repo_root = str(Path(__file__).resolve().parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from engine.assembly.sticker_art import STYLES
    from scripts.bake_stickers import bake_one

    if style not in STYLES:
        raise ValueError(f"unknown sticker style {style!r}")

    root = Path(root)
    folder = root / BAKES_DIRNAME / bake_key(slug, style, size, fps)
    if _resolve(folder, fps=fps, size=size) is not None:
        return
    bake_one(_source(slug, root), folder, style=style, size=size, fps=fps)
```

Note the source download now happens only when a bake is actually needed, where before it ran before the per-style loop.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_sticker_choices.py -v`
Expected: PASS.

- [ ] **Step 5: Prove it bites**

Put the `for style in STYLES:` loop back, run `test_only_the_style_the_beat_needs_is_baked`, record the failure, restore.

- [ ] **Step 6: Update the one caller, so this task leaves nothing broken**

`style` is required, and `engine/app.py` is the only caller. Updating it here
rather than leaving it to Task 6 keeps this commit's suite green — a commit
that ships a known `TypeError` makes its own review unable to tell planned
breakage from real breakage.

In `choose_sticker` (around line 2097), the call currently reads:

```python
            sticker_choices_mod.ensure_baked(
                body.slug, root=cache, size=size, fps=int(settings.fps))
```

The route still keys on a trigger name at this point and has no beat, so it
cannot know the beat's role yet — Task 6 gives it one. Until then pass the
module's own default grade explicitly:

```python
            sticker_choices_mod.ensure_baked(
                body.slug, root=cache, size=size, fps=int(settings.fps),
                style=stickers_mod.DEFAULT_STYLE)
```

`DEFAULT_STYLE` is `"punchy"`, defined at `engine/assembly/stickers.py:101`.
Task 6 replaces this line with the beat's real style.

- [ ] **Step 7: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain
git add engine/assembly/sticker_choices.py engine/app.py tests/test_sticker_choices.py
git commit -m "perf: bake the one style the beat actually needs"
```

---

### Task 6: The routes — keyed by beat, plus search by hand

**Files:**
- Modify: `engine/app.py` (`sticker_candidates` at lines 1980-2028, `choose_sticker` at lines 2075-2112, and the `StickerChoice` model at module level)
- Test: `tests/test_sticker_routes.py` (create if absent; otherwise add to the file that already covers the picker routes)

**Interfaces:**
- Consumes: `Cue.beat_id`, `Cue.terms` (Task 2); `Store.choose_sticker(plan_id, beat_id, slug)` (Task 3); `ensure_baked(..., style=...)` (Task 5); `stickers_mod.style_for_role(role)`.
- Produces: `GET /api/plan/{plan_id}/stickers`, `POST /api/plan/{plan_id}/sticker/{beat_id}`, `GET /api/sticker-search?term=...`.

- [ ] **Step 1: Write the fixtures**

Create `tests/test_sticker_routes.py`. The fixture shape copies
`tests/test_clip_player.py:42-57`, which is how every route test in this
suite builds a client.

The catalogue is copied from the working tree rather than fetched, so the
tests never touch the network. `work/_lordicon/lordicon-wired.xml` is 5.4MB
and already present; if it is missing, run
`python -c "from engine.assembly import sticker_catalog as c; c.refresh('work/_lordicon')"`
once before starting.

```python
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.assembly import sticker_catalog
from engine.config import Settings
from engine.contract import StickerCue
from engine.media.voice import caption_timings
from engine.store import Store
from tests.factories import make_plan

REAL_CATALOGUE = Path("work/_lordicon") / sticker_catalog.CACHE_NAME


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    settings.stickers = True
    app = TestClient(create_app(settings=settings))
    # A real sitemap, copied not fetched: `refresh` no-ops when the cache is
    # already there, so this is what keeps the suite off the network.
    cache = Path(settings.work_dir) / "_lordicon"
    cache.mkdir(parents=True, exist_ok=True)
    if not REAL_CATALOGUE.exists():
        pytest.skip(f"catalogue not cached at {REAL_CATALOGUE}")
    shutil.copy(REAL_CATALOGUE, cache / sticker_catalog.CACHE_NAME)
    return app


def _store(client) -> Store:
    return client.app.state.store


def _stored(client, stickers):
    """A saved plan with caption timings, and the given per-beat stickers.

    ``stickers`` maps a beat id to a ``StickerCue``. Timings matter: a beat
    with no ``words`` produces no cue at all, so a plan saved without them
    would make every assertion below vacuous.
    """
    plan = make_plan(beats=4, measured=4.0)
    for beat in plan.script.beats:
        beat.words = caption_timings(beat.caption_text, 4.0)
    for beat_id, cue in stickers.items():
        beat = next(b for b in plan.script.beats if b.beat_id == beat_id)
        beat.sticker = cue
        # The named word has to be in the caption, or the cue lands at the
        # midpoint and the test stops testing what it says it tests.
        beat.caption_text = f"{beat.caption_text} {cue.word}"
        beat.words = caption_timings(beat.caption_text, 4.0)
    _store(client).save_plan(plan)
    return plan.plan_id


@pytest.fixture()
def plan_with_stickers(client):
    return _stored(client, {"b2": StickerCue(
        word="raat", terms=["moon", "star", "night"])})


@pytest.fixture()
def plan_no_hits(client):
    """Terms that are all abstract nouns, which the catalogue does not have."""
    return _stored(client, {"b2": StickerCue(
        word="raat", terms=["dream", "journey", "farewell"])})
```

- [ ] **Step 2: Write the failing tests**

In the same file. `store` in the signatures below is `_store(client)` —
call the helper rather than taking a fixture, so the client is built first.

```python
def test_candidates_are_keyed_by_beat_and_searched_by_the_scripts_terms(client, plan_with_stickers):
    body = client.get(f"/api/plan/{plan_with_stickers}/stickers").json()
    row = next(r for r in body["rows"] if r["beat_id"] == "b2")
    assert row["choose"].endswith("/sticker/b2")
    assert row["terms"] == ["moon", "star", "night"]
    assert row["candidates"], "the script's terms should find icons"


def test_a_beat_whose_terms_find_nothing_still_gets_a_row(client, plan_no_hits):
    """No candidates is not no sticker. The emoji renders and the person
    can type their own term."""
    body = client.get(f"/api/plan/{plan_no_hits}/stickers").json()
    row = next(r for r in body["rows"] if r["beat_id"] == "b2")
    assert row["candidates"] == []


def test_choosing_is_keyed_by_beat(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b2",
                    json={"slug": "27-globe"})
    assert r.status_code == 200
    assert r.json()["style"] in ("punchy", "dark")
    assert _store(client).sticker_choices(plan_with_stickers) == \
        {"b2": "27-globe"}


def test_choosing_for_a_beat_that_does_not_exist_is_a_404(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b99",
                    json={"slug": "27-globe"})
    assert r.status_code == 404


def test_search_by_hand_returns_catalogue_slugs(client):
    body = client.get("/api/sticker-search", params={"term": "cloud"}).json()
    assert body["slugs"], "cloud is in the catalogue"
    assert all("-" in slug for slug in body["slugs"])


def test_search_never_treats_the_term_as_a_path(client, tmp_path):
    """The term is caller-controlled and is only ever matched against slugs
    already in the sitemap. It must not reach the filesystem."""
    for hostile in ("../../../etc/passwd", "/etc/passwd", "..\\..\\win.ini"):
        body = client.get("/api/sticker-search",
                          params={"term": hostile}).json()
        assert body["slugs"] == []


def test_choosing_a_slug_that_is_not_in_the_catalogue_is_refused(client, plan_with_stickers):
    r = client.post(f"/api/plan/{plan_with_stickers}/sticker/b2",
                    json={"slug": "../../../evil"})
    assert r.status_code == 400
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_routes.py -v`
Expected: FAIL — `row["beat_id"]` exists already but `row["choose"]` still ends in the trigger name, `row["terms"]` is absent, and `/api/sticker-search` 404s.

- [ ] **Step 4: Rewrite the candidates route**

In `engine/app.py`, the row-building loop inside `sticker_candidates` (lines 2004-2027). The `triggers` lookup and the per-trigger `search` walk both go, because `Cue.terms` now carries the terms on both rungs:

```python
        chosen = store.sticker_choices(plan_id)
        by_id = {beat.beat_id: beat for beat in plan.script.beats}

        rows = []
        for cue in stickers_mod.find_cues(plan,
                                          cap=int(settings.sticker_max)):
            seen: list[str] = []
            for term in cue.terms:
                for slug in sticker_catalog.search(term, slugs):
                    if slug not in seen:
                        seen.append(slug)
            beat = by_id.get(cue.beat_id)
            rows.append({
                "beat_id": cue.beat_id,
                "trigger": cue.name,
                "source": cue.source,
                "word": cue.word,
                "terms": list(cue.terms),
                "start": round(cue.start, 2),
                "style": stickers_mod.style_for_role(
                    getattr(beat, "role", None)),
                "chosen": chosen.get(cue.beat_id),
                "candidates": [
                    {"slug": slug,
                     "preview": f"/api/sticker-preview/{slug}",
                     "motion": f"/api/sticker-motion/{slug}"}
                    for slug in seen[:8]],
                "choose": f"/api/plan/{plan_id}/sticker/{cue.beat_id}",
                "search": "/api/sticker-search",
            })
        return {"plan_id": plan_id, "rows": rows, "note": note}
```

- [ ] **Step 5: Rewrite the choose route**

Replace the signature and the two lines that differ (lines 2075-2112):

```python
    @app.post("/api/plan/{plan_id}/sticker/{beat_id}")
    def choose_sticker(plan_id: str, beat_id: str,
                       body: StickerChoice) -> dict:
        """Bake one icon for this reel's beat and remember it.

        The slug is checked against the catalogue before anything is
        fetched. Without that check this route is an arbitrary URL fetcher
        with the panel's network access.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        beat = next((b for b in plan.script.beats if b.beat_id == beat_id),
                    None)
        if beat is None:
            raise HTTPException(404, "no such beat")

        cache = sticker_choices_mod.cache_root(settings)
        try:
            sticker_catalog.refresh(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            raise HTTPException(503, f"catalogue unavailable: {exc}") from exc
        if body.slug not in sticker_catalog.load(cache):
            raise HTTPException(400, "not a catalogue slug")

        size = stickers_mod.sticker_size(settings.width,
                                         settings.sticker_scale)
        style = stickers_mod.style_for_role(beat.role)
        try:
            sticker_choices_mod.ensure_baked(
                body.slug, root=cache, size=size, fps=int(settings.fps),
                style=style)
        except ValueError as exc:
            # Two refusals land here, and the detail below is `str(exc)`, so
            # the caller sees whichever it was:
            #   - `bake_one` refusing an icon with a pocket of trapped white,
            #     which would render with a blob in it;
            #   - `safe_slug` refusing a slug that could not become a path.
            # Either way, refusing keeps whatever was chosen before.
            raise HTTPException(422, str(exc)) from exc

        store.choose_sticker(plan_id, beat_id, body.slug)
        return {"plan_id": plan_id, "beat_id": beat_id, "slug": body.slug,
                "style": style,
                "preview": f"/api/sticker-preview/{body.slug}"}
```

- [ ] **Step 6: Add the search route**

Beside `sticker_preview`:

```python
    @app.get("/api/sticker-search")
    def sticker_search(term: str) -> dict:
        """Catalogue slugs for a term someone typed.

        Needed because the catalogue is named for objects and not for ideas:
        a script that asks for "dream" or "journey" finds nothing, and
        without this the person is left with an emoji and no way to reach
        the 3,578 icons that are sitting right there.

        ``term`` never becomes a path. It is matched against slugs already
        in the cached sitemap, and a term that matches none returns none.
        """
        cache = sticker_choices_mod.cache_root(settings)
        try:
            sticker_catalog.refresh(cache)
            slugs = sticker_catalog.load(cache)
        except sticker_catalog.CatalogUnavailable as exc:
            raise HTTPException(503, f"catalogue unavailable: {exc}") from exc
        found = sticker_catalog.search(term, slugs, limit=12)
        return {"term": term,
                "slugs": found,
                "candidates": [
                    {"slug": slug,
                     "preview": f"/api/sticker-preview/{slug}",
                     "motion": f"/api/sticker-motion/{slug}"}
                    for slug in found]}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sticker_routes.py -v`
Expected: PASS (7 tests).

- [ ] **Step 8: Prove the two security tests bite**

| Disable | Test that must FAIL |
|---|---|
| Remove the `if body.slug not in sticker_catalog.load(cache)` check | `test_choosing_a_slug_that_is_not_in_the_catalogue_is_refused` |
| Make `sticker_search` return `[str(cache / term)]` instead of searching | `test_search_never_treats_the_term_as_a_path` |

Record both failures in the report, then restore.

- [ ] **Step 9: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain   # engine/ui/index.html must NOT appear
git add engine/app.py tests/test_sticker_routes.py
git commit -m "feat: pick a reel's sticker per beat, and search the catalogue by hand"
```

---

### Task 7: Teach the prompt what the catalogue can actually find

**Files:**
- Modify: `engine/prompts/script.txt` (the DUAL TEXT block around the `on_screen_text` description, and the JSON example at the end)
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: the `sticker` shape from Task 1.
- Produces: nothing code-level.

- [ ] **Step 1: Pin the asymmetry the prompt's rule exists for**

Create **`tests/test_sticker_vocabulary.py`** — a new file, not an addition
to `tests/test_sticker_catalog.py`. That file's own docstring says "Nothing
here talks to the network except `refresh`, and its test writes the sitemap
itself": it builds a synthetic ten-slug sitemap on purpose, and loading the
real 5.4MB cache into it would break a property it states about itself.
This test genuinely needs the real 3,578 icons — ten synthetic slugs cannot
show that `dream` finds nothing. Note that file imports the module as `cat`.

This is the measurement the prompt's whole rule rests on. Without it, the
instruction in `script.txt` is just an opinion in a text file.

```python
"""What the icon catalogue can and cannot be asked for.

Measured against the real cached sitemap, not a synthetic one: the claim is
about the shape of 3,578 real icon names, and ten fixtures cannot carry it.
Skips rather than fetches when the cache is cold -- a test that pulls 5.4MB
is a test people learn to skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.assembly import sticker_catalog as cat

# Every word on the left is an abstract noun and finds nothing; every word on
# the right is a thing you could photograph and finds an icon. This is why
# `sticker.terms` is a list of concrete objects rather than one word for the
# idea, and why the prompt forbids abstract nouns.
ABSTRACT = ("dream", "journey", "memory", "farewell", "goodbye", "job",
            "interview", "resume", "effort", "strength", "college",
            "friendship", "study", "failure", "luggage")
CONCRETE = ("cloud", "star", "road", "train", "photo", "camera",
            "briefcase", "office", "book", "graduation", "student",
            "code", "laptop", "heart", "muscle", "sad")


@pytest.fixture(scope="module")
def catalogue():
    cache = Path("work/_lordicon")
    if not (cache / cat.CACHE_NAME).exists():
        pytest.skip("catalogue not cached; run sticker_catalog.refresh first")
    slugs = cat.load(cache)
    assert len(slugs) > 3000, f"only {len(slugs)} slugs; cache looks truncated"
    return slugs


def test_the_catalogue_is_named_for_things_not_for_ideas(catalogue):
    hit = [w for w in ABSTRACT if cat.search(w, catalogue)]
    missed = [w for w in CONCRETE if not cat.search(w, catalogue)]
    assert not hit, f"these abstract words unexpectedly hit: {hit}"
    assert not missed, f"these concrete words unexpectedly missed: {missed}"


def test_a_multi_word_term_finds_nothing(catalogue):
    """`search` ranks against hyphen-separated slug name parts, which is why
    `terms` is a list of single words rather than one phrase."""
    assert cat.search("moon night", catalogue) == []
```

- [ ] **Step 2: Write the failing prompt test**

Add to `tests/test_prompts.py`:

```python
def test_the_script_prompt_asks_for_concrete_sticker_terms():
    """The catalogue is named for objects, not ideas -- see
    test_the_catalogue_is_named_for_things_not_for_ideas. A prompt that
    does not say so gets abstract nouns and empty candidate lists."""
    text = Path("engine/prompts/script.txt").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "sticker" in lowered
    assert "caption_text" in lowered
    for word in ("concrete", "abstract"):
        assert word in lowered, f"the prompt never says {word}"
    # The example is what a model actually copies.
    assert '"terms"' in text and '"word"' in text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_sticker_catalog.py tests/test_prompts.py -k "named_for_things or multi_word or concrete_sticker" -v`
Expected: the two catalogue tests PASS (they measure what is already true), and `test_the_script_prompt_asks_for_concrete_sticker_terms` FAILS — the prompt has no `sticker` section.

- [ ] **Step 4: Add the field description**

In `engine/prompts/script.txt`, directly after the `on_screen_text` block:

```
sticker        : OPTIONAL, and worth using wherever a beat has one clear
                 thing in it. "word" MUST be a word that appears in this
                 beat's caption_text, spelled exactly as it appears there —
                 it is what gives the sticker its timing, and a word that is
                 not in the caption makes the sticker land loosely.
                 "terms" are 3-4 CONCRETE, PHOTOGRAPHABLE OBJECTS in
                 English, best first, single words only.

                 The icon library is named for THINGS, NOT IDEAS. These
                 find nothing: dream, journey, memory, farewell, goodbye,
                 job, interview, resume, effort, strength, college,
                 friendship, study, failure, luggage. These find something:
                 cloud, star, road, train, photo, camera, briefcase,
                 office, book, graduation, student, code, laptop, heart,
                 muscle, sad. Never emit an abstract noun — give the object
                 that stands for it. For a beat about a dream, write
                 ["cloud", "star", "moon"]. For one about a farewell, write
                 ["suitcase", "train", "door"].

                 Give at most one sticker per beat, and null on the beats
                 that have no one clear thing.
```

- [ ] **Step 5: Add it to the JSON example**

In the `Return ONLY this JSON:` block, inside the beat object after `"on_screen_text"`:

```
        "sticker": {{"word": "<a word from caption_text>",
                     "terms": ["<object>", "<object>", "<object>"]}},
```

The file is formatted with `str.format`, so the braces must be doubled exactly as shown — the surrounding block already doubles its own.

- [ ] **Step 6: Run the test, and prove the prompt still formats**

```bash
python -m pytest tests/test_prompts.py -v
python -c "
from pathlib import Path
t = Path('engine/prompts/script.txt').read_text(encoding='utf-8')
import re
fields = {m for m in re.findall(r'\{(\w+)\}', t)}
print('placeholders:', sorted(fields))
print(t.format(**{f: 'X' for f in fields})[-400:])
"
```

Expected: PASS, and the formatted tail prints the JSON example with single braces and the `sticker` key present. A `KeyError` here means a brace was not doubled.

- [ ] **Step 7: Generate one real script and count what the model does**

This is the spec's first named risk and the only way to measure it:

There is no `engine/cli.py`. The entry point is
`engine.pipeline.plan_stage(topic_raw, client, store, settings)`
(`engine/pipeline.py:106`), driven by `OmniRouteClient` from
`engine.omniroute` — the same client `engine/app.py` builds.

```bash
python - <<'MEASURE'
from engine.config import Settings
from engine.omniroute import OmniRouteClient
from engine.pipeline import plan_stage
from engine.store import Store
from engine.assembly import stickers as S
from engine.assembly import sticker_catalog as cat

settings = Settings()
store = Store(settings.db_path); store.init()
client = OmniRouteClient(settings=settings)
plan = plan_stage("Rajkot ki ek ladki ka Indore mein pehla saal",
                  client, store, settings)

asked = [b for b in plan.script.beats if b.sticker]
print(f"{len(asked)} of {len(plan.script.beats)} beats asked for a sticker")
try:
    slugs = cat.load("work/_lordicon")
except Exception as exc:
    slugs = ()
    print("catalogue unavailable, skipping the term check:", exc)
for b in asked:
    caption = [S.normalise(w) for w in b.caption_text.split()]
    in_caption = S.normalise(b.sticker.word) in caption
    hits = [t for t in b.sticker.terms if slugs and cat.search(t, slugs)]
    print(f"  {b.beat_id} word={b.sticker.word!r} in_caption={in_caption} "
          f"terms={b.sticker.terms} terms_that_hit={hits}")
MEASURE
```

Record in the report: how many beats asked, how many named a word that is genuinely in their own caption, and how many of the terms find catalogue icons. If fewer than half the words are in their captions, say so — the prompt needs another pass and that is a finding, not a failure of this task.

- [ ] **Step 8: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git status --porcelain
git add engine/prompts/script.txt tests/test_prompts.py tests/test_sticker_vocabulary.py
git commit -m "feat: ask the script for objects, because the icons are named for things"
```

---

## What this plan does not do

`engine/ui/index.html` is untouched. Until it is updated, the picker's cards
still key on `trigger` and will not line up with the new routes — the
backend is correct and the panel is one task behind it. That task is
deliberately deferred: a concurrent session rewrote 184 lines of that file
in its last commit, and landing a second rewrite on top of it would cost
more in conflict resolution than it saves in waiting.

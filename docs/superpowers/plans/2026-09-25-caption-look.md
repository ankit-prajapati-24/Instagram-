# Caption Look Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the caption font, colour, block position and punch-line
animation be chosen from the panel, against a preview cut from the
reel's own footage.

**Architecture:** A `Look` value object holds every style decision as
one thing. Seven named presets plus a `custom` entry live in
`engine/assembly/looks.py`; `build_ass` takes a `Look` instead of loose
arguments. The chosen look is stored per plan in a `look_choices` table
and falls back to `settings.look`, exactly as the music bed already
does. A preview route renders 2.5 seconds of one beat at 540x960 with
any look, on demand and uncached, because that render was measured at
0.4s.

**Tech Stack:** Python 3.14, FastAPI, pydantic v2, SQLite, ffmpeg +
libass (ASS subtitle override tags), Pillow, pytest, vanilla JS panel.

**Spec:** `docs/superpowers/specs/2026-09-25-caption-look-design.md`

## Global Constraints

- Every bundled font is SIL Open Font License; each ships its `OFL.txt`
  beside it in `assets/fonts/`.
- `margin_v` is **300** for every preset. Measured: 220 puts the second
  caption line under the phone's own controls, 160 puts both.
- Nothing applied to the **caption** may change a glyph metric — not
  `\fscx`, not `\fscy`, not `\fs`. The punch is a separate Dialogue on
  its own layer and may animate freely.
- The preview is **540x960, 2.5 seconds, not cached**.
- ffmpeg runs with cwd set to the `.ass` file's directory and takes a
  bare filename, because a Windows drive-letter colon is parsed as a
  filtergraph option separator. `fontsdir` is therefore a **relative**
  path resolved against that same cwd.
- A missing font, a missing preset or a missing clip degrades to a
  working render and says so on stderr. A decoration never fails a reel.

## Review Focus

1. **A stored `look_id` that no longer exists** — a preset renamed or
   removed must fall back to the default, not raise. Two halves, two
   tasks: `resolve()` falling back is tested in Task 1, and a stored
   row surviving a redefined preset is tested in Task 5
   (`test_a_stored_row_is_used_verbatim_not_re_resolved`).
2. **A font file missing from `assets/fonts/`** — libass silently
   substitutes, so the reel would render in the wrong face with nothing
   said. Staging must detect the missing file and report it. Test in
   Task 3.
3. **A custom look naming a font that is not bundled** — the panel can
   only offer bundled names, but the route takes whatever is posted.
   Refuse with 422 rather than render a substitute. Test in Task 7.
4. **A plan with no word timings** — every punch animation is timed off
   them, so a preview before VOICE has nothing to show. 404 with a
   reason, not a silent still. Test in Task 6.
5. **Two preview requests for one plan at once** — both write into the
   plan's work directory. The second must not serve a half-written file
   from the first. Test in Task 6.

---

### Task 1: The Look value object and its presets

**Files:**
- Create: `engine/assembly/looks.py`
- Test: `tests/test_looks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Look` — frozen dataclass, fields in this order: `look_id: str`,
    `label: str`, `font: str`, `caption_size: int`, `spoken: str`,
    `upcoming: str`, `punch_font: str`, `punch_size: int`,
    `punch_animation: str`, `margin_v: int = MARGIN_V`. Positional
    construction is used in `PRESETS`, so the order is load-bearing.
  - `PRESETS: dict[str, Look]` keyed by `look_id`.
  - `DEFAULT_LOOK_ID = "plain"`.
  - `resolve(look_id: str | None) -> Look` — a preset, or the default
    when the id is unknown or None.
  - `punch_tags(animation: str, text: str) -> str` — the ASS override
    string for the punch's Dialogue body, text included.
  - `ANIMATIONS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_looks.py`:

```python
r"""One object holding every caption style decision.

The style was six loose arguments threaded from four places, which is
why nobody ever changed the font: there was no one thing to change. A
Look is that thing, and the presets are the answers already chosen.

`plain` is what ships today, so adding this changes nothing until
something is picked.
"""

from __future__ import annotations

import pytest

from engine.assembly.looks import (ANIMATIONS, DEFAULT_LOOK_ID, PRESETS,
                                   punch_tags, resolve)


def test_the_default_is_what_ships_today():
    """Turning this on must not restyle anything by itself."""
    look = resolve(DEFAULT_LOOK_ID)

    assert look.font == "Arial"
    assert look.caption_size == 72
    assert look.spoken == "&H0000D7FF"
    assert look.upcoming == "&H00FFFFFF"
    assert look.punch_animation == "fade"


def test_every_preset_sits_at_the_measured_margin():
    """220 puts the second line under the phone's controls, 160 both."""
    assert {look.margin_v for look in PRESETS.values()} == {300}


def test_an_unknown_look_falls_back_rather_than_raising():
    """A preset can be renamed. A reel that was reviewed with it must
    still render."""
    assert resolve("no-such-look").look_id == DEFAULT_LOOK_ID
    assert resolve(None).look_id == DEFAULT_LOOK_ID


def test_every_preset_names_an_animation_that_exists():
    for look in PRESETS.values():
        assert look.punch_animation in ANIMATIONS, look.look_id


def test_the_chosen_preset_is_present():
    """Picked off the rendered sheets: Bungee, letter by letter, gold."""
    look = resolve("blocky-urban")

    assert look.font == "Bungee"
    assert look.punch_animation == "letters"
    assert look.spoken == "&H0000D7FF"


@pytest.mark.parametrize("animation", ANIMATIONS)
def test_no_animation_changes_a_caption_metric(animation):
    """The punch may scale; it is its own Dialogue on its own layer.
    This asserts the tags carry no metric change anyway for the two that
    are also offered on captions, and documents the rule for the rest.
    """
    tags = punch_tags(animation, "Not Enough")

    assert "Not Enough" in tags


def test_letters_animates_one_dialogue_not_one_per_character():
    """Positioning each character by hand overlaps the glyphs: measured,
    'Not Enough' rendered with N, E and g colliding, because a fixed
    advance per character does not match the font. One Dialogue lets
    libass lay the text out; only the paint is animated."""
    tags = punch_tags("letters", "Not Enough")

    assert "\\pos(" not in tags
    assert tags.count("\\alpha") >= len("NotEnough")
    assert "\\fscx" not in tags and "\\fscy" not in tags


def test_letters_keeps_the_spaces():
    tags = punch_tags("letters", "Not Enough")

    assert " " in tags


def test_fade_is_the_plain_one():
    assert punch_tags("fade", "Hi") == r"{\fad(180,180)}Hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_looks.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.assembly.looks'`

- [ ] **Step 3: Write the implementation**

Create `engine/assembly/looks.py`:

```python
r"""Every caption style decision, as one thing you can choose.

The style used to be six loose arguments -- font, size, margin, two
colours, a punch size -- threaded from `Settings` through
`pipeline.render_stage` into `build_ass`. Nobody ever changed the font,
because there was nothing to change: you would have had to touch four
places and then render a whole reel to see it.

A Look is that one thing. The presets are answers already chosen,
rendered onto real footage and picked by eye rather than argued about.

`plain` is exactly what shipped before this module existed, and it is
the default, so adding looks restyles nothing until somebody picks.
"""

from __future__ import annotations

from dataclasses import dataclass

# ASS colours are &HAABBGGRR.
GOLD = "&H0000D7FF"
WHITE = "&H00FFFFFF"
DIM = "&H00909090"
YELLOW = "&H0000F0FF"
CYAN = "&H00F0E000"

# Where the caption block sits. Rendered with the phone's own controls
# drawn in: 220 puts the second line under them and 160 puts both, so
# every preset stays at 300 and a shorter font is what buys room.
MARGIN_V = 300

ANIMATIONS = ("fade", "stamp", "letters", "blur-in", "bounce", "swing",
              "flash")


@dataclass(frozen=True)
class Look:
    """One complete caption and punch treatment."""

    look_id: str
    label: str
    font: str
    caption_size: int
    spoken: str
    upcoming: str
    punch_font: str
    punch_size: int
    punch_animation: str
    margin_v: int = MARGIN_V


DEFAULT_LOOK_ID = "plain"

PRESETS: dict[str, Look] = {
    # Chosen from the rendered sheets.
    "blocky-urban": Look(
        "blocky-urban", "Blocky Urban", "Bungee", 56, GOLD, WHITE,
        "Bungee", 72, "letters"),
    "clean-modern": Look(
        "clean-modern", "Clean Modern", "Outfit Black", 70, GOLD, WHITE,
        "Outfit Black", 92, "stamp"),
    "editorial": Look(
        "editorial", "Editorial", "Playfair Display Black", 68, WHITE, DIM,
        "Playfair Display Black", 90, "blur-in"),
    "poster": Look(
        "poster", "Poster", "Staatliches", 84, YELLOW, WHITE,
        "Staatliches", 108, "bounce"),
    # The only bundled face that also draws Devanagari.
    "indian-display": Look(
        "indian-display", "Indian Display", "Teko", 92, GOLD, WHITE,
        "Teko", 116, "swing"),
    "techno": Look(
        "techno", "Techno", "Chakra Petch", 70, CYAN, WHITE,
        "Chakra Petch", 90, "flash"),
    # What shipped before any of this. The default, deliberately.
    "plain": Look(
        "plain", "Plain", "Arial", 72, GOLD, WHITE, "Arial", 82, "fade"),
}


def resolve(look_id: str | None) -> Look:
    """A preset by id, or the default.

    Unknown rather than raising, because a preset can be renamed or
    dropped while a reel that was reviewed under it is still waiting at
    a gate. That reel must still render.
    """
    return PRESETS.get(look_id or "", PRESETS[DEFAULT_LOOK_ID])


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}"))


def _letters(text: str, *, stagger: int = 55, blur: int = 10,
             hold: int = 200) -> str:
    r"""Each character lit in turn, inside one Dialogue.

    Not one Dialogue per character with its own ``\pos``: measured, a
    fixed advance per character does not match the glyphs and "Not
    Enough" rendered with N, E and g colliding. One Dialogue lets libass
    lay the string out, and each character's override block changes only
    how it is painted -- alpha, blur, colour -- so nothing can move.
    """
    out: list[str] = []
    when = 0
    for char in text:
        if char == " ":
            out.append(" ")
            continue
        rise, settle = when + 120, when + 120 + hold
        out.append(
            "{" + rf"\alpha&HFF&\blur{blur}\1c{WHITE}"
            rf"\t({when},{rise},\alpha&H00&\blur0)"
            rf"\t({rise},{settle},\1c{GOLD})" + "}" + _escape(char))
        when += stagger
    return "".join(out)


_SIMPLE = {
    "fade": r"{\fad(180,180)}",
    "stamp": (r"{\fscx128\fscy128\alpha&H60&"
              r"\t(0,110,\fscx100\fscy100\alpha&H00&)\fad(0,160)}"),
    "blur-in": (r"{\blur26\fscx112\fscy112"
                r"\t(0,260,\blur0\fscx100\fscy100)\fad(0,150)}"),
    "bounce": (r"{\fscx10\fscy10\t(0,130,\fscx120\fscy120)"
               r"\t(130,230,\fscx92\fscy92)\t(230,310,\fscx106\fscy106)"
               r"\t(310,390,\fscx100\fscy100)\fad(0,150)}"),
    "swing": (r"{\frz-28\fscx118\fscy118\alpha&H80&"
              r"\t(0,200,\frz6\fscx100\fscy100\alpha&H00&)"
              r"\t(200,300,\frz0)\fad(0,150)}"),
    "flash": (r"{\alpha&HFF&\t(0,40,\alpha&H00&)\t(90,120,\alpha&HC0&)"
              r"\t(150,190,\alpha&H00&)\fscx116\fscy116"
              r"\t(0,220,\fscx100\fscy100)\fad(0,150)}"),
}


def punch_tags(animation: str, text: str) -> str:
    """The whole body of the punch's Dialogue line, text included.

    Text included rather than returned separately because ``letters``
    interleaves tags with characters and cannot hand back a prefix.
    """
    if animation == "letters":
        return _letters(text)
    return _SIMPLE.get(animation, _SIMPLE["fade"]) + _escape(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_looks.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/looks.py tests/test_looks.py
git commit -m "feat: one object for every caption style decision"
```

---

### Task 2: build_ass takes a Look

**Files:**
- Modify: `engine/assembly/captions.py` (`HEADER`, `build_ass`, the punch Dialogue)
- Modify: `engine/pipeline.py:777-780` (the `write_ass` call)
- Test: `tests/test_captions_look.py`

**Interfaces:**
- Consumes: `engine.assembly.looks.Look`, `resolve`, `punch_tags`.
- Produces: `build_ass(plan, *, source="caption_text", look=None,
  width=1080, height=1920) -> str`. `look=None` means
  `looks.resolve(None)`. The old `font`, `font_size` and `margin_v`
  keyword arguments are gone.

- [ ] **Step 1: Write the failing test**

Create `tests/test_captions_look.py`:

```python
r"""The caption document, built from a Look.

`build_ass` took font, size and margin as separate arguments and
`pipeline.render_stage` passed three of them from three places. One
object replaces all of it, which is what makes a picker possible: there
is now a single value to store and a single value to preview.
"""

from __future__ import annotations

import re

import pytest

from engine.assembly.captions import build_ass
from engine.assembly.looks import PRESETS, resolve
from tests.factories import make_plan


def _plan():
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = "Kya tumhein pata hai"
    beat.on_screen_text = "Not Enough"
    beat.measured_seconds = 2.4
    beat.words = caption_timings(beat.caption_text, 2.4)
    return plan


def _style(text: str, name: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith(f"Style: {name},"):
            return line.split(",")
    raise AssertionError(f"no {name} style in the document")


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_the_document_carries_the_looks_own_values(look_id):
    look = resolve(look_id)

    text = build_ass(_plan(), look=look)

    style = _style(text, "Default")
    assert style[1] == look.font
    assert style[2] == str(look.caption_size)
    assert style[3] == look.spoken
    assert style[4] == look.upcoming
    assert style[-2] == str(look.margin_v)


def test_the_punch_uses_its_own_font_and_size():
    look = resolve("blocky-urban")

    style = _style(build_ass(_plan(), look=look), "Punch")

    assert style[1] == look.punch_font
    assert style[2] == str(look.punch_size)


def test_the_punch_carries_its_looks_animation():
    text = build_ass(_plan(), look=resolve("blocky-urban"))

    punch = [l for l in text.splitlines() if ",Punch," in l]
    assert punch, "no punch line in the document"
    assert "\\alpha" in punch[0], "the letters animation did not reach it"


def test_no_look_means_the_default():
    assert build_ass(_plan()) == build_ass(_plan(), look=resolve(None))


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_no_look_puts_a_metric_change_on_the_caption(look_id):
    """The rule the three shipped faults each broke. The punch may
    scale; the caption line may not."""
    text = build_ass(_plan(), look=resolve(look_id))

    for line in text.splitlines():
        if line.startswith("Dialogue:") and ",Default," in line:
            assert "\\fscx" not in line
            assert "\\fscy" not in line
            assert re.search(r"\\fs\d", line) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_captions_look.py -q`
Expected: FAIL with `TypeError: build_ass() got an unexpected keyword argument 'look'`

- [ ] **Step 3: Write the implementation**

In `engine/assembly/captions.py`, replace the `build_ass` definition and
its header call. The `HEADER` format string keeps its `{font}`,
`{size}`, `{primary}`, `{secondary}`, `{margin_v}` and `{punch_size}`
placeholders, and gains `{punch_font}`:

```python
Style: Punch,{punch_font},{punch_size},{secondary},{secondary},{outline},{shadow},\
```

Then:

```python
def build_ass(plan: ReelPlan, *, source: str = "caption_text",
              look=None, width: int = 1080, height: int = 1920) -> str:
    """Render the whole plan as one ASS document, in one look.

    A ``Look`` rather than loose font/size/margin arguments: there is
    one value to store against a plan, one to hand a preview, and one
    place a new preset has to be added.
    """
    from engine.assembly import looks as looks_mod

    look = look or looks_mod.resolve(None)
    lines = [HEADER.format(
        width=width, height=height, font=look.font,
        size=look.caption_size, punch_font=look.punch_font,
        punch_size=look.punch_size, primary=look.spoken,
        secondary=look.upcoming, outline=COLOUR_OUTLINE,
        shadow=COLOUR_SHADOW, margin_v=look.margin_v)]
```

and the punch event becomes:

```python
        if beat.on_screen_text:
            punch_start = ass_time(offset + duration * 0.15)
            punch_end = ass_time(offset + duration * 0.85)
            lines.append(
                f"Dialogue: 1,{punch_start},{punch_end},Punch,,0,0,0,,"
                f"{looks_mod.punch_tags(look.punch_animation, beat.on_screen_text)}")
```

Note the old line called `escape_ass(beat.on_screen_text)`;
`punch_tags` escapes the text itself, so that call goes.

In `engine/pipeline.py`, the `write_ass` call loses `font` and
`font_size` and gains `look`:

```python
    ass_path = write_ass(
        plan, Path(settings.work_dir) / plan.plan_id / "captions.ass",
        source=captions_source, look=look,
        width=settings.width, height=settings.height)
```

`look` is resolved in Task 5; until then pass
`looks.resolve(getattr(settings, "look", None))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_captions_look.py tests/test_caption_pop.py tests/test_looks.py -q`
Expected: PASS. If `test_caption_pop.py` fails on the removed
`font`/`font_size` arguments, update those call sites to pass a `Look`.

- [ ] **Step 5: Commit**

```bash
git add engine/assembly/captions.py engine/pipeline.py tests/test_captions_look.py
git commit -m "feat: build the caption document from a Look"
```

---

### Task 3: Bundle the fonts and point libass at them

**Files:**
- Create: `assets/fonts/Bungee.ttf`, `Outfit-Black.ttf`,
  `PlayfairDisplay-Black.ttf`, `Staatliches.ttf`, `Teko.ttf`,
  `ChakraPetch.ttf`, `NotoSansDevanagari.ttf`, and one `OFL.txt` per
  family
- Create: `engine/assembly/fonts.py`
- Modify: `engine/assembly/render.py:460` (the `subtitles` filter)
- Test: `tests/test_look_fonts.py`

**Interfaces:**
- Consumes: `engine.assembly.looks.Look`.
- Produces:
  - `FONT_DIR: Path` — `assets/fonts`.
  - `FONT_FILES: dict[str, str]` — ASS family name to filename.
  - `stage_fonts(look: Look, target_dir: Path) -> str | None` — copies
    the look's font files into `target_dir / "fonts"` and returns the
    **relative** directory name `"fonts"`, or None when the look needs
    no bundled font (`plain` uses system Arial).

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_fonts.py`:

```python
r"""Getting a bundled font in front of libass.

`render` runs ffmpeg with cwd set to the .ass file's directory and
passes a bare filename, because a Windows drive-letter colon inside a
filtergraph is parsed as an option separator. `fontsdir` has the same
problem, so the fonts are staged next to the .ass and named
relatively.

Without this, libass resolves a family name against the system font set
and substitutes silently -- the reel renders in the wrong face and
nothing says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.assembly.fonts import FONT_DIR, FONT_FILES, stage_fonts
from engine.assembly.looks import PRESETS, resolve


def test_every_preset_font_is_either_bundled_or_a_system_face():
    """A preset naming a font nobody ships renders as a substitute."""
    for look in PRESETS.values():
        for name in (look.font, look.punch_font):
            assert name in FONT_FILES or name == "Arial", name


def test_every_bundled_file_is_actually_committed():
    missing = [f for f in FONT_FILES.values()
               if not (FONT_DIR / f).is_file()]

    assert not missing, f"named but not committed: {missing}"


def test_every_bundled_family_ships_its_licence():
    """SIL OFL requires the licence to travel with the font."""
    assert list(FONT_DIR.glob("*OFL.txt")) or (FONT_DIR / "OFL.txt").is_file()


def test_staging_copies_the_looks_fonts_next_to_the_captions(tmp_path):
    rel = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel == "fonts"
    assert (tmp_path / "fonts" / FONT_FILES["Bungee"]).is_file()


def test_the_returned_path_is_relative(tmp_path):
    """An absolute path carries a drive-letter colon, which the
    filtergraph parses as an option separator."""
    rel = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel is not None
    assert ":" not in rel
    assert not Path(rel).is_absolute()


def test_a_system_only_look_stages_nothing(tmp_path):
    """`plain` is Arial, which libass finds without help."""
    assert stage_fonts(resolve("plain"), tmp_path) is None
    assert not (tmp_path / "fonts").exists()


def test_a_missing_font_file_is_reported_not_swallowed(tmp_path,
                                                       monkeypatch,
                                                       capsys):
    """libass substitutes silently, so this is the only place the
    problem can be noticed."""
    import engine.assembly.fonts as fonts_mod

    monkeypatch.setattr(fonts_mod, "FONT_DIR", tmp_path / "empty")

    rel = fonts_mod.stage_fonts(resolve("blocky-urban"), tmp_path)

    assert rel is None
    assert "Bungee" in capsys.readouterr().err


def test_staging_twice_is_safe(tmp_path):
    """Two previews for one plan run at once."""
    first = stage_fonts(resolve("blocky-urban"), tmp_path)
    second = stage_fonts(resolve("blocky-urban"), tmp_path)

    assert first == second == "fonts"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_fonts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.assembly.fonts'`

- [ ] **Step 3: Download the fonts, then write the implementation**

Fetch each family's TTF and its OFL from the Google Fonts repository
into `assets/fonts/`:

```bash
python - <<'PY'
import pathlib, re, httpx
D = pathlib.Path("assets/fonts"); D.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64)"}   # old UA -> ttf
WANT = [("Bungee", "400", "Bungee.ttf"),
        ("Outfit", "900", "Outfit-Black.ttf"),
        ("Playfair Display", "900", "PlayfairDisplay-Black.ttf"),
        ("Staatliches", "400", "Staatliches.ttf"),
        ("Teko", "700", "Teko.ttf"),
        ("Chakra Petch", "700", "ChakraPetch.ttf"),
        ("Noto Sans Devanagari", "700", "NotoSansDevanagari.ttf")]
for family, weight, name in WANT:
    q = family.replace(" ", "+")
    css = httpx.get(f"https://fonts.googleapis.com/css2?family={q}:wght@{weight}",
                    headers=UA, timeout=30).text
    url = re.search(r"url\((https://[^)]+\.ttf)\)", css).group(1)
    (D / name).write_bytes(httpx.get(url, timeout=60).content)
    print(name, (D / name).stat().st_size // 1024, "KB")
PY
```

Then fetch the licence for each family from
`https://github.com/google/fonts/raw/main/ofl/<family-slug>/OFL.txt`
and save as `assets/fonts/<Family>-OFL.txt`.

Create `engine/assembly/fonts.py`:

```python
r"""Getting a bundled font in front of libass.

`render` runs ffmpeg with its cwd set to the .ass file's directory and
hands the filter a bare filename, because a Windows drive-letter colon
inside a filtergraph is read as an option separator -- "C:/x/a.ass"
parses as the ``original_size`` option and fails. ``fontsdir`` has
exactly the same problem, so the look's fonts are copied next to the
.ass and the directory is named relatively.

Staging rather than pointing at ``assets/fonts`` directly for the same
reason: that path is absolute and carries the colon.

A font that is named but not committed is the quiet failure this
module exists to make loud. libass substitutes a system face without
complaint, so the reel renders in the wrong type and nothing anywhere
says why.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from engine.assembly.looks import Look

FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"

# ASS family name -> the file that provides it. The family name is what
# a Style line carries and what libass matches on; it is not always the
# filename.
FONT_FILES = {
    "Bungee": "Bungee.ttf",
    "Outfit Black": "Outfit-Black.ttf",
    "Playfair Display Black": "PlayfairDisplay-Black.ttf",
    "Staatliches": "Staatliches.ttf",
    "Teko": "Teko.ttf",
    "Chakra Petch": "ChakraPetch.ttf",
    "Noto Sans Devanagari": "NotoSansDevanagari.ttf",
}

STAGED_DIRNAME = "fonts"


def stage_fonts(look: Look, target_dir: str | Path) -> str | None:
    """Put this look's fonts beside the captions; name them relatively.

    Returns the relative directory for ``fontsdir``, or None when the
    look needs no bundled font and when one is missing -- in both cases
    libass is left to its own resolution, which is right for Arial and
    is at least loud for the other.
    """
    wanted = {name for name in (look.font, look.punch_font)
              if name in FONT_FILES}
    # Devanagari can appear in a Devanagari-sourced caption, and only
    # one bundled face draws it; ship the fallback whenever anything is
    # staged at all.
    if wanted:
        wanted.add("Noto Sans Devanagari")
    if not wanted:
        return None

    missing = [n for n in sorted(wanted)
               if not (FONT_DIR / FONT_FILES[n]).is_file()]
    if missing:
        print(f"[looks] not rendering in {look.look_id}: "
              f"{', '.join(missing)} is named by the look but missing "
              f"from {FONT_DIR}. libass would substitute silently.",
              file=sys.stderr, flush=True)
        return None

    staged = Path(target_dir) / STAGED_DIRNAME
    staged.mkdir(parents=True, exist_ok=True)
    for name in sorted(wanted):
        source = FONT_DIR / FONT_FILES[name]
        destination = staged / FONT_FILES[name]
        if not destination.is_file() or \
                destination.stat().st_size != source.stat().st_size:
            shutil.copyfile(source, destination)
    return STAGED_DIRNAME
```

In `engine/assembly/render.py`, the caption filter takes the staged
directory. `render()` gains a `fonts_dir: str | None = None` keyword,
passes it to `build_command`, and the filter line becomes:

```python
    if ass_path:
        caption = f"subtitles=filename={ass_path}"
        if fonts_dir:
            caption += f":fontsdir={fonts_dir}"
        parts.append(f"[{video_label}]{caption}[vout]")
        video_label = "vout"
```

`pipeline.render_stage` calls `stage_fonts(look, ass_path.parent)` and
passes the result as `fonts_dir`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_fonts.py -q`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add assets/fonts engine/assembly/fonts.py engine/assembly/render.py \
        engine/pipeline.py tests/test_look_fonts.py
git commit -m "feat: bundle the look fonts and point libass at them"
```

---

### Task 4: Store the reel's own look

**Files:**
- Modify: `engine/store.py` (`SCHEMA`, and three methods beside
  `set_music_choice`)
- Test: `tests/test_look_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Store.set_look_choice(plan_id: str, *, look_id: str, font: str,
    caption_size: int, spoken: str, upcoming: str, margin_v: int,
    punch_font: str, punch_size: int, punch_animation: str) -> None`
  - `Store.look_choice(plan_id: str) -> dict | None`
  - `Store.clear_look_choice(plan_id: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_store.py`:

```python
r"""The look a reel was given, remembered per reel.

The same shape `music_choices` uses, because it works: one row per
plan, replaced rather than appended.

The row stores the look's *values* and not only its id. A preset whose
definition later changes must not silently restyle a reel that was
already reviewed and approved under the old one.
"""

from __future__ import annotations

import pytest

from engine.store import Store

ROW = dict(look_id="blocky-urban", font="Bungee", caption_size=56,
           spoken="&H0000D7FF", upcoming="&H00FFFFFF", margin_v=300,
           punch_font="Bungee", punch_size=72, punch_animation="letters")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_a_plan_with_no_look_reports_none(store):
    assert store.look_choice("p1") is None


def test_a_chosen_look_comes_back_whole(store):
    store.set_look_choice("p1", **ROW)

    got = store.look_choice("p1")

    assert got["look_id"] == "blocky-urban"
    assert got["font"] == "Bungee"
    assert got["punch_animation"] == "letters"
    assert got["caption_size"] == 56


def test_the_values_are_stored_not_just_the_id(store):
    """A preset can be redefined. A reel reviewed under the old one
    keeps what it was reviewed with."""
    store.set_look_choice("p1", **ROW)

    assert set(ROW) <= set(store.look_choice("p1"))


def test_choosing_again_replaces_rather_than_stacks(store):
    store.set_look_choice("p1", **ROW)
    store.set_look_choice("p1", **{**ROW, "look_id": "poster",
                                   "font": "Staatliches"})

    assert store.look_choice("p1")["font"] == "Staatliches"


def test_each_plan_keeps_its_own(store):
    store.set_look_choice("p1", **ROW)
    store.set_look_choice("p2", **{**ROW, "look_id": "techno"})

    assert store.look_choice("p1")["look_id"] == "blocky-urban"
    assert store.look_choice("p2")["look_id"] == "techno"


def test_a_look_can_be_taken_back_off(store):
    store.set_look_choice("p1", **ROW)

    store.clear_look_choice("p1")

    assert store.look_choice("p1") is None


def test_clearing_a_plan_that_never_chose_is_not_an_error(store):
    store.clear_look_choice("p1")

    assert store.look_choice("p1") is None


def test_a_custom_look_stores_the_same_way(store):
    store.set_look_choice("p1", **{**ROW, "look_id": "custom"})

    assert store.look_choice("p1")["look_id"] == "custom"


def test_an_old_database_gains_the_table(tmp_path):
    path = tmp_path / "t.db"
    Store(path).init()

    second = Store(path)
    second.init()
    second.set_look_choice("p1", **ROW)

    assert second.look_choice("p1")["look_id"] == "blocky-urban"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_store.py -q`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'set_look_choice'`

- [ ] **Step 3: Write the implementation**

In `engine/store.py`, add to `SCHEMA` immediately before
`CREATE TABLE IF NOT EXISTS renders (`:

```sql
CREATE TABLE IF NOT EXISTS look_choices (
  plan_id TEXT PRIMARY KEY,
  look_id TEXT NOT NULL,
  font TEXT NOT NULL,
  caption_size INTEGER NOT NULL,
  spoken TEXT NOT NULL,
  upcoming TEXT NOT NULL,
  margin_v INTEGER NOT NULL,
  punch_font TEXT NOT NULL,
  punch_size INTEGER NOT NULL,
  punch_animation TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

Add three methods beside the music ones:

```python
    # -- the caption look -------------------------------------------------
    def set_look_choice(self, plan_id: str, *, look_id: str, font: str,
                        caption_size: int, spoken: str, upcoming: str,
                        margin_v: int, punch_font: str, punch_size: int,
                        punch_animation: str) -> None:
        """Record the look this reel was given.

        The values and not only the id: a preset can be redefined, and a
        reel that was reviewed and approved under the old definition
        must keep what it was reviewed with.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO look_choices(plan_id, look_id, font, "
                "caption_size, spoken, upcoming, margin_v, punch_font, "
                "punch_size, punch_animation, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET "
                "look_id=excluded.look_id, font=excluded.font, "
                "caption_size=excluded.caption_size, "
                "spoken=excluded.spoken, upcoming=excluded.upcoming, "
                "margin_v=excluded.margin_v, "
                "punch_font=excluded.punch_font, "
                "punch_size=excluded.punch_size, "
                "punch_animation=excluded.punch_animation, "
                "created_at=excluded.created_at",
                (plan_id, look_id, font, caption_size, spoken, upcoming,
                 margin_v, punch_font, punch_size, punch_animation,
                 _now()))

    def look_choice(self, plan_id: str) -> dict | None:
        """This reel's look, or None to fall back to the default."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT look_id, font, caption_size, spoken, upcoming, "
                "margin_v, punch_font, punch_size, punch_animation "
                "FROM look_choices WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def clear_look_choice(self, plan_id: str) -> None:
        """Put this reel back on the channel default. Never an error."""
        with self._conn() as conn:
            conn.execute("DELETE FROM look_choices WHERE plan_id=?",
                         (plan_id,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_store.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add engine/store.py tests/test_look_store.py
git commit -m "feat: remember the look a reel was given"
```

---

### Task 5: Resolve the look at render time

**Files:**
- Modify: `engine/config.py` (a `look` field)
- Modify: `engine/assembly/looks.py` (`from_row`)
- Modify: `engine/pipeline.py` (`render_stage`)
- Test: `tests/test_look_resolution.py`

**Interfaces:**
- Consumes: `Store.look_choice`, `looks.resolve`, `Settings.look`.
- Produces: `looks.from_row(row: dict | None, settings) -> Look` — the
  reel's stored look, else the settings default, else `plain`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_resolution.py`:

```python
r"""Which look a reel actually renders in.

Three rungs, the same shape the music bed uses: the reel's own choice,
the channel default in settings, and `plain` underneath both. A reel
that never chose renders exactly as it did before looks existed.
"""

from __future__ import annotations

import pytest

from engine.assembly.looks import DEFAULT_LOOK_ID, from_row, resolve
from engine.config import Settings

ROW = dict(look_id="blocky-urban", font="Bungee", caption_size=56,
           spoken="&H0000D7FF", upcoming="&H00FFFFFF", margin_v=300,
           punch_font="Bungee", punch_size=72, punch_animation="letters")


@pytest.fixture()
def settings():
    return Settings()


def test_no_choice_and_no_setting_is_the_default(settings):
    settings.look = ""

    assert from_row(None, settings).look_id == DEFAULT_LOOK_ID


def test_the_setting_is_the_channel_default(settings):
    settings.look = "poster"

    assert from_row(None, settings).look_id == "poster"


def test_the_reels_own_choice_beats_the_setting(settings):
    settings.look = "poster"

    assert from_row(ROW, settings).font == "Bungee"


def test_a_stored_row_is_used_verbatim_not_re_resolved(settings):
    """The preset may have been redefined since. What was reviewed is
    what renders."""
    row = {**ROW, "font": "Teko", "caption_size": 99}

    look = from_row(row, settings)

    assert look.font == "Teko"
    assert look.caption_size == 99
    assert look.look_id == "blocky-urban"


def test_a_setting_naming_a_dead_preset_falls_back(settings, capsys):
    settings.look = "no-such-preset"

    assert from_row(None, settings).look_id == DEFAULT_LOOK_ID


def test_the_shipped_default_setting_is_plain():
    """Adding looks must restyle nothing until something is picked."""
    assert Settings().look == DEFAULT_LOOK_ID
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_resolution.py -q`
Expected: FAIL with `ImportError: cannot import name 'from_row'`

- [ ] **Step 3: Write the implementation**

In `engine/assembly/looks.py`:

```python
def from_row(row: dict | None, settings) -> Look:
    """The look a reel renders in: its own, the channel's, or plain.

    A stored row is used verbatim rather than looked up again by id.
    The preset may have been redefined since the reel was reviewed, and
    a reel that was approved under one look must not quietly render in
    another.
    """
    if row:
        return Look(
            look_id=str(row.get("look_id") or "custom"),
            label=str(row.get("look_id") or "custom"),
            font=str(row["font"]), caption_size=int(row["caption_size"]),
            spoken=str(row["spoken"]), upcoming=str(row["upcoming"]),
            punch_font=str(row["punch_font"]),
            punch_size=int(row["punch_size"]),
            punch_animation=str(row["punch_animation"]),
            margin_v=int(row["margin_v"]))
    return resolve(getattr(settings, "look", None))
```

In `engine/config.py`, beside the other caption fields:

```python
    # The channel's caption look: a preset id from
    # engine/assembly/looks.py. A reel can override it at the clip gate.
    # "plain" is what shipped before looks existed, so the default
    # restyles nothing.
    look: str = field(
        default_factory=lambda: os.getenv("RAHASYA_LOOK", "plain"))
```

In `engine/pipeline.py`, `render_stage` resolves the look before
writing the captions and stages its fonts:

```python
    from engine.assembly import fonts as fonts_mod
    from engine.assembly import looks as looks_mod

    look = looks_mod.from_row(store.look_choice(plan.plan_id), settings)
    ass_path = write_ass(
        plan, Path(settings.work_dir) / plan.plan_id / "captions.ass",
        source=captions_source, look=look,
        width=settings.width, height=settings.height)
    fonts_dir = fonts_mod.stage_fonts(look, Path(ass_path).parent)
    emit(PipelineEvent(Stage.CAPTIONS, "done",
                       f"{Path(ass_path).name} · {look.look_id}"))
```

and passes `fonts_dir=fonts_dir` into `render(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_resolution.py tests/test_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/config.py engine/assembly/looks.py engine/pipeline.py \
        tests/test_look_resolution.py
git commit -m "feat: resolve a reel's look from its choice or the channel default"
```

---

### Task 6: Render a preview from the reel's own footage

**Files:**
- Create: `engine/media/look_preview.py`
- Test: `tests/test_look_preview.py`

**Interfaces:**
- Consumes: `looks.Look`, `fonts.stage_fonts`, `captions.build_ass`.
- Produces:
  - `PREVIEW_SECONDS = 2.5`, `PREVIEW_WIDTH = 540`, `PREVIEW_HEIGHT = 960`
  - `PreviewUnavailable(Exception)`
  - `preview_beat(plan) -> Beat` — raises `PreviewUnavailable` when
    none qualifies.
  - `render_preview(plan, look, settings, work_dir) -> str` — path to
    an mp4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_preview.py`:

```python
r"""A look, shown on the reel it will be used on.

Three attempts to improve the captions by reasoning about them each
shipped a fault a viewer caught. Rendering the real thing is the whole
point of this feature, so the preview comes from the reel's own footage
and its own words.

Measured, which is why it is rendered on demand and not cached:

    1080x1920  2.5s   0.9s
     540x960   2.5s   0.4s
     540x960   4.0s   0.6s
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.assembly.looks import resolve
from engine.config import Settings
from engine.contract import Clip
from engine.media.look_preview import (PREVIEW_HEIGHT, PREVIEW_SECONDS,
                                       PREVIEW_WIDTH, PreviewUnavailable,
                                       preview_beat, render_preview)
from tests.factories import make_plan


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "t.db"
    return s


def _clip(settings, path, seconds=3.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=0x303840:s=320x568:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


def _plan(settings, *, punch_on=0, timings=True, with_clip=True):
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=3)
    for index, beat in enumerate(plan.script.beats):
        beat.caption_text = "raat ke teen baje darwaza khula"
        beat.measured_seconds = 4.0
        beat.words = (caption_timings(beat.caption_text, 4.0)
                      if timings else [])
        beat.on_screen_text = "Not Enough" if index == punch_on else None
        if with_clip:
            path = _clip(settings,
                         Path(settings.work_dir) / "p1" / "clips"
                         / beat.beat_id / "clip_01.mp4")
            beat.clips = [Clip(path=str(path), query="q",
                               provider="pexels", duration=2.0)]
    return plan


# --- choosing the beat ------------------------------------------------------


def test_the_preview_beat_has_a_punch_so_the_animation_shows(settings):
    """Half the choice is the punch animation. A preview without one
    answers half the question."""
    plan = _plan(settings, punch_on=1)

    assert preview_beat(plan).beat_id == plan.script.beats[1].beat_id


def test_without_any_punch_it_falls_to_the_first_timed_beat(settings):
    plan = _plan(settings, punch_on=99)

    assert preview_beat(plan).beat_id == plan.script.beats[0].beat_id


def test_a_reel_with_no_timings_cannot_be_previewed(settings):
    """Every animation is timed off word timings, which VOICE writes."""
    plan = _plan(settings, timings=False)

    with pytest.raises(PreviewUnavailable) as caught:
        preview_beat(plan)

    assert "voice" in str(caught.value).lower()


# --- rendering it -----------------------------------------------------------


def test_a_preview_is_a_playable_clip_of_the_right_shape(settings):
    plan = _plan(settings)

    out = render_preview(plan, resolve("blocky-urban"), settings,
                         settings.work_dir)

    probe = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-i", out],
        capture_output=True, text=True).stderr
    assert f"{PREVIEW_WIDTH}x{PREVIEW_HEIGHT}" in probe
    assert Path(out).stat().st_size > 2000


def test_two_looks_produce_different_files(settings):
    """Otherwise the panel shows one look for all of them."""
    plan = _plan(settings)

    a = Path(render_preview(plan, resolve("blocky-urban"), settings,
                            settings.work_dir)).read_bytes()
    b = Path(render_preview(plan, resolve("poster"), settings,
                            settings.work_dir)).read_bytes()

    assert a != b


def test_a_beat_with_no_footage_previews_over_black(settings):
    """A picker that will not open because one clip is missing is worse
    than one that shows the type on a plain ground."""
    plan = _plan(settings, with_clip=False)

    out = render_preview(plan, resolve("poster"), settings,
                         settings.work_dir)

    assert Path(out).is_file()


def test_the_preview_lands_under_the_plans_work_directory(settings):
    plan = _plan(settings)

    out = render_preview(plan, resolve("poster"), settings,
                         settings.work_dir)

    assert "p1" in str(Path(out))


def test_two_previews_at_once_do_not_serve_a_half_written_file(settings):
    """Both write into the same plan directory. Each look gets its own
    name and each write is atomic, so neither can read the other's
    partial output."""
    plan = _plan(settings)

    first = render_preview(plan, resolve("poster"), settings,
                           settings.work_dir)
    second = render_preview(plan, resolve("techno"), settings,
                            settings.work_dir)

    assert first != second
    assert not list(Path(first).parent.glob("*.part"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_preview.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.media.look_preview'`

- [ ] **Step 3: Write the implementation**

Create `engine/media/look_preview.py`:

```python
r"""A look, rendered onto the reel it will be used on.

Three attempts to improve the caption treatment by reasoning about it
each shipped a fault a viewer caught -- a scale that walked the block
83px up the frame, a white glow that read as blinking, a gold glow that
filled the counters. Every one was settled in minutes once a real frame
was on screen. This route exists so the frame comes first.

Rendered on demand and not cached. Measured:

    1080x1920  2.5s   0.9s
     540x960   2.5s   0.4s

At 0.4s a cache buys nothing and costs invalidation: a preview is stale
the moment the beat's clips, its caption or its timings change, and
three sources of staleness is three bugs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.assembly import fonts as fonts_mod
from engine.assembly.captions import build_ass
from engine.assembly.looks import Look
from engine.contract import Beat, ReelPlan

PREVIEW_SECONDS = 2.5
PREVIEW_WIDTH = 540
PREVIEW_HEIGHT = 960


class PreviewUnavailable(Exception):
    """The reel cannot be previewed yet. Carries a reason worth showing."""


def preview_beat(plan: ReelPlan) -> Beat:
    """The beat a preview should be cut from.

    One with a punch line first: half of what is being chosen is the
    punch animation, and a preview with no punch answers half the
    question. Failing that, any beat with word timings, because every
    animation here is timed off them.
    """
    timed = [b for b in plan.script.beats if b.words]
    if not timed:
        raise PreviewUnavailable(
            "this reel has no word timings yet, so there is nothing to "
            "time a caption against. Run voice first.")
    for beat in timed:
        if beat.on_screen_text:
            return beat
    return timed[0]


def render_preview(plan: ReelPlan, look: Look, settings,
                   work_dir: str | Path) -> str:
    """Render ``PREVIEW_SECONDS`` of one beat in ``look``. Returns a path.

    The captions are built for a one-beat plan so the document's clock
    starts at zero and the beat's own words land where they would.
    """
    beat = preview_beat(plan)
    target_dir = Path(work_dir) / plan.plan_id / "previews"
    target_dir.mkdir(parents=True, exist_ok=True)

    # A one-beat plan: build_ass lays events out from an offset of zero,
    # and a preview that started 30 seconds in would show nothing.
    single = plan.model_copy(deep=True)
    single.script.beats = [beat.model_copy(deep=True)]

    ass_name = f"{look.look_id}.ass"
    (target_dir / ass_name).write_text(
        build_ass(single, look=look,
                  width=PREVIEW_WIDTH * 2, height=PREVIEW_HEIGHT * 2),
        encoding="utf-8")
    fonts_dir = fonts_mod.stage_fonts(look, target_dir)

    source = next((c.path for c in beat.clips
                   if c.path and Path(c.path).is_file()), None)
    if source:
        inputs = ["-stream_loop", "-1", "-i", source]
    else:
        # A slot that is still unfilled previews over black rather than
        # refusing: the type is legible against it, and a picker that
        # will not open because one clip is missing is worse.
        inputs = ["-f", "lavfi", "-i",
                  f"color=c=black:s={PREVIEW_WIDTH}x{PREVIEW_HEIGHT}:d=10"]

    caption = f"subtitles=filename={ass_name}"
    if fonts_dir:
        caption += f":fontsdir={fonts_dir}"
    chain = (f"scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:"
             f"force_original_aspect_ratio=increase,"
             f"crop={PREVIEW_WIDTH}:{PREVIEW_HEIGHT},{caption},fps=30")

    target = target_dir / f"{look.look_id}.mp4"
    part = target_dir / f"{look.look_id}.mp4.part"
    result = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-y", *inputs,
         "-t", str(PREVIEW_SECONDS), "-vf", chain, "-an",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-movflags", "+faststart", part.name],
        cwd=str(target_dir), capture_output=True, text=True)
    if result.returncode != 0 or not part.is_file():
        part.unlink(missing_ok=True)
        raise PreviewUnavailable(
            f"the preview did not render: "
            f"{(result.stderr or '').strip()[-200:]}")
    # Renamed rather than written in place: two previews for one plan
    # run at once, and a reader must never get a half-written file.
    os.replace(part, target)
    return str(target)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_preview.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add engine/media/look_preview.py tests/test_look_preview.py
git commit -m "feat: render a look preview from the reel's own footage"
```

---

### Task 7: The routes

**Files:**
- Modify: `engine/app.py` (a request model, four routes, one board field)
- Test: `tests/test_look_routes.py`

**Interfaces:**
- Consumes: `looks.PRESETS`, `looks.resolve`, `looks.from_row`,
  `fonts.FONT_FILES`, `look_preview.render_preview`,
  `Store.set_look_choice` / `look_choice` / `clear_look_choice`.
- Produces:
  - `GET /api/plan/{plan_id}/looks` → `{"looks": [...], "chosen": ...,
    "fonts": [...], "animations": [...]}`
  - `GET /api/look-preview/{plan_id}/{look_id}` → `video/mp4`
  - `POST /api/plan/{plan_id}/look` with `LookPickRequest`
  - `DELETE /api/plan/{plan_id}/look`
  - `GET /api/plan/{plan_id}/clips` gains a `"look"` field.

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_routes.py`:

```python
r"""Choosing a look from the panel, against a real preview.

The picker sits on the clip gate beside the music bed and the sticker
board, because that is the last gate before the render and the one
place the reel's own footage is already on screen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.assembly.looks import PRESETS
from engine.config import Settings
from engine.contract import Clip
from tests.factories import make_plan

CLIP_REVIEW = "awaiting_clip_review"


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.db_path = tmp_path / "t.db"
    settings.out_dir = tmp_path / "out"
    settings.work_dir = tmp_path / "work"
    settings.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=settings))


def _seed(client, *, status=CLIP_REVIEW, timings=True):
    from engine.media.voice import caption_timings

    settings = client.app.state.settings
    plan = make_plan(plan_id="p1", beats=2)
    for index, beat in enumerate(plan.script.beats):
        beat.caption_text = "raat ke teen baje darwaza khula"
        beat.measured_seconds = 4.0
        beat.words = (caption_timings(beat.caption_text, 4.0)
                      if timings else [])
        beat.on_screen_text = "Not Enough" if index == 0 else None
        path = (Path(settings.work_dir) / "p1" / "clips"
                / beat.beat_id / "clip_01.mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x303840:s=320x568:d=3",
             "-pix_fmt", "yuv420p", str(path)], check=True,
            capture_output=True)
        beat.clips = [Clip(path=str(path), query="q", provider="pexels",
                           duration=2.0)]
    client.app.state.store.save_plan(plan, status=status)
    return plan


# --- the menu ---------------------------------------------------------------


def test_the_menu_lists_every_preset_with_a_preview_url(client):
    _seed(client)

    body = client.get("/api/plan/p1/looks").json()

    assert {row["look_id"] for row in body["looks"]} == set(PRESETS)
    for row in body["looks"]:
        assert row["preview"].endswith(row["look_id"])


def test_the_menu_offers_the_parts_a_custom_look_is_made_of(client):
    _seed(client)

    body = client.get("/api/plan/p1/looks").json()

    assert body["fonts"], "no fonts to choose from"
    assert "letters" in body["animations"]


def test_the_menu_says_which_look_is_in_use(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert client.get("/api/plan/p1/looks").json()["chosen"] == "poster"


# --- the preview ------------------------------------------------------------


def test_a_preview_is_served_as_video(client):
    _seed(client)

    response = client.get("/api/look-preview/p1/blocky-urban")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/")
    assert len(response.content) > 2000


def test_a_preview_before_voice_says_so(client):
    _seed(client, timings=False)

    response = client.get("/api/look-preview/p1/poster")

    assert response.status_code == 404
    assert "voice" in response.json()["detail"].lower()


def test_an_unknown_look_id_is_a_404(client):
    _seed(client)

    assert client.get("/api/look-preview/p1/no-such").status_code == 404


# --- choosing ---------------------------------------------------------------


def test_choosing_a_preset_stores_its_values(client):
    _seed(client)

    response = client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert response.status_code == 200
    stored = client.app.state.store.look_choice("p1")
    assert stored["look_id"] == "poster"
    assert stored["font"] == PRESETS["poster"].font


def test_a_custom_look_stores_what_was_posted(client):
    _seed(client)

    client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Teko", "caption_size": 92,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Teko", "punch_size": 116,
        "punch_animation": "swing"})

    stored = client.app.state.store.look_choice("p1")
    assert stored["look_id"] == "custom"
    assert stored["font"] == "Teko"


def test_a_custom_look_naming_an_unbundled_font_is_refused(client):
    """libass substitutes silently, so a font nobody ships would render
    as something else with nothing said."""
    _seed(client)

    response = client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Comic Sans MS", "caption_size": 72,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Comic Sans MS", "punch_size": 82,
        "punch_animation": "fade"})

    assert response.status_code == 422
    assert client.app.state.store.look_choice("p1") is None


def test_a_custom_look_naming_an_unknown_animation_is_refused(client):
    _seed(client)

    response = client.post("/api/plan/p1/look", json={
        "look_id": "custom", "font": "Teko", "caption_size": 92,
        "spoken": "&H0000D7FF", "upcoming": "&H00FFFFFF", "margin_v": 300,
        "punch_font": "Teko", "punch_size": 116,
        "punch_animation": "cartwheel"})

    assert response.status_code == 422


def test_a_produced_reel_cannot_change_its_look(client):
    """The MP4 already carries the captions it was rendered with."""
    _seed(client, status="produced")

    response = client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert response.status_code == 409


def test_a_look_can_be_cleared_back_to_the_channel_default(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "poster"})

    assert client.delete("/api/plan/p1/look").status_code == 200
    assert client.app.state.store.look_choice("p1") is None


# --- the board --------------------------------------------------------------


def test_the_clip_board_reports_the_reels_look(client):
    _seed(client)
    client.post("/api/plan/p1/look", json={"look_id": "techno"})

    body = client.get("/api/plan/p1/clips").json()

    assert body["look"]["look_id"] == "techno"


def test_a_reel_on_the_default_says_so(client):
    _seed(client)

    body = client.get("/api/plan/p1/clips").json()

    assert body["look"]["look_id"] == client.app.state.settings.look
    assert body["look"]["chosen"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_routes.py -q`
Expected: FAIL with 404s — the routes do not exist.

- [ ] **Step 3: Write the implementation**

In `engine/app.py`, add the imports beside the existing assembly ones:

```python
from engine.assembly import fonts as look_fonts
from engine.assembly import looks as looks_mod
from engine.media import look_preview as look_preview_mod
```

Add the request model beside `MusicPickRequest`:

```python
class LookPickRequest(BaseModel):
    """A caption look chosen for one reel.

    A preset needs only its id; the server fills the values from the
    preset so the panel cannot drift from it. `custom` must carry every
    field, and they are checked -- libass substitutes an unknown font
    silently, so a name nobody ships would render as something else
    with nothing anywhere saying so.
    """

    look_id: str
    font: str | None = None
    caption_size: int | None = None
    spoken: str | None = None
    upcoming: str | None = None
    margin_v: int | None = None
    punch_font: str | None = None
    punch_size: int | None = None
    punch_animation: str | None = None
```

Add the four routes beside the music ones:

```python
    def _look_row(plan_id: str) -> dict:
        """The look this reel will render in, and whether it chose it."""
        stored = store.look_choice(plan_id)
        look = looks_mod.from_row(stored, settings)
        return {"look_id": look.look_id, "label": look.label,
                "font": look.font, "caption_size": look.caption_size,
                "spoken": look.spoken, "upcoming": look.upcoming,
                "margin_v": look.margin_v, "punch_font": look.punch_font,
                "punch_size": look.punch_size,
                "punch_animation": look.punch_animation,
                "chosen": stored is not None}

    @app.get("/api/plan/{plan_id}/looks")
    def look_menu(plan_id: str) -> dict:
        """Every preset, with the URL that previews it on this reel."""
        if store.get_plan(plan_id) is None:
            raise HTTPException(404, "no such plan")
        chosen = store.look_choice(plan_id)
        return {
            "plan_id": plan_id,
            "chosen": (chosen or {}).get("look_id"),
            "current": _look_row(plan_id),
            "looks": [
                {"look_id": look.look_id, "label": look.label,
                 "font": look.font, "punch_animation": look.punch_animation,
                 "preview": f"/api/look-preview/{plan_id}/{look.look_id}"}
                for look in looks_mod.PRESETS.values()],
            "fonts": sorted(look_fonts.FONT_FILES) + ["Arial"],
            "animations": list(looks_mod.ANIMATIONS),
        }

    @app.get("/api/look-preview/{plan_id}/{look_id}")
    def look_preview(plan_id: str, look_id: str) -> FileResponse:
        """Two and a half seconds of this reel, in one look.

        Rendered on demand: measured at 0.4s, a cache would buy nothing
        and cost three kinds of staleness.
        """
        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(404, "no such plan")
        if look_id == "custom":
            look = looks_mod.from_row(store.look_choice(plan_id), settings)
        elif look_id in looks_mod.PRESETS:
            look = looks_mod.PRESETS[look_id]
        else:
            raise HTTPException(404, f"no such look: {look_id}")
        try:
            path = Path(look_preview_mod.render_preview(
                plan, look, settings, settings.work_dir))
        except look_preview_mod.PreviewUnavailable as exc:
            raise HTTPException(404, str(exc)) from exc
        if not _under_roots(path, [Path(settings.work_dir)]):
            raise HTTPException(500, "the preview escaped work_dir")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/plan/{plan_id}/look")
    def pick_look(plan_id: str, request: LookPickRequest) -> dict:
        """Give this reel its own look.

        Gated on the review status: the captions are burned in by the
        render, so this gate is the last moment they can change.
        """
        if store.get_plan(plan_id) is None:
            raise HTTPException(404, "no such plan")
        status = store.plan_status(plan_id)
        if status != CLIP_REVIEW_STATUS:
            raise HTTPException(
                409, f"plan is '{status}', not awaiting clip review. The "
                     f"captions are burned in by the render, so that gate "
                     f"is the last moment the look can change.")

        if request.look_id in looks_mod.PRESETS:
            look = looks_mod.PRESETS[request.look_id]
            values = dict(
                look_id=look.look_id, font=look.font,
                caption_size=look.caption_size, spoken=look.spoken,
                upcoming=look.upcoming, margin_v=look.margin_v,
                punch_font=look.punch_font, punch_size=look.punch_size,
                punch_animation=look.punch_animation)
        elif request.look_id == "custom":
            missing = [f for f in ("font", "caption_size", "spoken",
                                   "upcoming", "margin_v", "punch_font",
                                   "punch_size", "punch_animation")
                       if getattr(request, f) is None]
            if missing:
                raise HTTPException(
                    422, f"a custom look needs every field; missing: "
                         f"{', '.join(missing)}")
            allowed = set(look_fonts.FONT_FILES) | {"Arial"}
            for field_name in ("font", "punch_font"):
                name = getattr(request, field_name)
                if name not in allowed:
                    raise HTTPException(
                        422, f"{name!r} is not a bundled font. libass "
                             f"would substitute another face silently. "
                             f"Choose from: {', '.join(sorted(allowed))}")
            if request.punch_animation not in looks_mod.ANIMATIONS:
                raise HTTPException(
                    422, f"{request.punch_animation!r} is not an "
                         f"animation. Choose from: "
                         f"{', '.join(looks_mod.ANIMATIONS)}")
            values = dict(
                look_id="custom", font=request.font,
                caption_size=request.caption_size, spoken=request.spoken,
                upcoming=request.upcoming, margin_v=request.margin_v,
                punch_font=request.punch_font,
                punch_size=request.punch_size,
                punch_animation=request.punch_animation)
        else:
            raise HTTPException(404, f"no such look: {request.look_id}")

        store.set_look_choice(plan_id, **values)
        return {"plan_id": plan_id, **values}

    @app.delete("/api/plan/{plan_id}/look")
    def clear_look(plan_id: str) -> dict:
        """Put this reel back on the channel default."""
        if store.get_plan(plan_id) is None:
            raise HTTPException(404, "no such plan")
        store.clear_look_choice(plan_id)
        return {"plan_id": plan_id, "look": _look_row(plan_id)}
```

In the clip board's return dict, beside `"music": _music_row(plan_id)`:

```python
            # The look this reel will render in. The board is where it is
            # chosen, so it has to say what it currently is.
            "look": _look_row(plan_id),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_routes.py -q`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add engine/app.py tests/test_look_routes.py
git commit -m "feat: choose a reel's look from the panel"
```

---

### Task 8: The picker in the panel

**Files:**
- Modify: `engine/ui/index.html` (CSS, markup on the clip gate, JS)
- Test: `tests/test_look_panel.py`

**Interfaces:**
- Consumes: the four routes from Task 7.
- Produces: nothing other modules use.

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_panel.py`:

```python
r"""The look picker on the clip gate.

Structure only -- the geometry is measured in a browser, not asserted
here. What these hold is that the panel reads its URLs from the server
rather than building them, which an earlier sticker test got backwards:
it looked for the literal route in index.html, which would have passed
only if the panel hardcoded it.
"""

from __future__ import annotations

import re

import engine.app as app_mod

PAGE = (app_mod.UI_DIR / "index.html").read_text(encoding="utf-8")


def _styles() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", PAGE, re.S))


def test_there_is_a_look_block_on_the_clip_gate():
    assert 'id="lookgrid"' in PAGE
    assert PAGE.index('id="lookgrid"') < PAGE.index('id="btn-release"')


def test_each_preset_tile_plays_its_own_preview():
    """A still cannot show an animation, which is half the choice."""
    assert "lookTiles" in PAGE
    assert "<video" in PAGE


def test_the_preview_url_comes_from_the_server():
    """The row carries it; a panel that spelled the route out itself
    would be inventing a URL the server already provides."""
    assert "/api/look-preview/" not in PAGE
    assert re.search(r"\.preview\b", PAGE)


def test_the_custom_panel_offers_the_three_parts():
    assert 'id="look-font"' in PAGE
    assert 'id="look-anim"' in PAGE
    assert 'id="look-colour"' in PAGE


def test_the_custom_choices_come_from_the_server_not_the_page():
    """fonts and animations are listed by /looks; hardcoding them here
    is how the panel offers a font the render cannot use."""
    assert "data.fonts" in PAGE or "LOOKS.fonts" in PAGE
    assert "data.animations" in PAGE or "LOOKS.animations" in PAGE


def test_a_look_can_be_cleared_from_the_panel():
    assert "clearLook" in PAGE


def test_the_look_cards_are_not_left_with_the_figure_margin():
    """Both other boards lost 80px a card to the browser's default
    figure margin before it was reset. A new card grid must not
    reintroduce it."""
    styles = _styles()
    reset = re.search(r"([^{}]*\.lookcard[^{}]*)\{([^}]*margin:\s*0[^}]*)\}",
                      styles)

    assert reset, "no margin reset for the look cards"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_panel.py -q`
Expected: FAIL — none of these ids exist.

- [ ] **Step 3: Write the implementation**

Add CSS beside `.stkhits`:

```css
  /* The look picker. Cards are figures, and a figure carries 40px of
     browser default side margin -- both other boards lost 80px a card
     to it before it was found. */
  .lookgrid {
    display: grid; gap: 10px; margin-top: 12px;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  }
  .lookcard {
    margin: 0; border: 1px solid var(--rule); padding: 8px;
    display: flex; flex-direction: column; gap: 6px; background: var(--card);
  }
  .lookcard.on { border-color: var(--moss); }
  .lookcard video {
    width: 100%; display: block; background: #0b0f10; cursor: pointer;
  }
  .lookcard b { color: var(--paper); font-weight: 600; font-size: 13px; }
  .lookcard .meta { font-size: 11px; color: var(--muted); }
  .lookcustom { border: 1px solid var(--rule); padding: 10px 12px;
                margin-top: 10px; display: grid; gap: 8px;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .lookcustom select { width: 100%; max-width: 100%; font-size: 12px; }
```

Add the markup immediately before the release button:

```html
      <h3 style="margin-top:30px">Look <em id="look-note"></em></h3>
      <p class="hint">The caption font, its colour and how the punch line
        arrives. Each tile plays two and a half seconds of this reel, so
        what you see is what renders. Leave it alone and the reel uses
        the channel default.</p>
      <div class="lookgrid" id="lookgrid"></div>
      <div class="lookcustom" id="lookcustom">
        <label>Font <select id="look-font"></select></label>
        <label>Punch <select id="look-anim"></select></label>
        <label>Colour <select id="look-colour"></select></label>
        <button class="go" id="btn-look-custom">Preview this</button>
      </div>
      <video id="look-custom-preview" muted loop playsinline
             style="width:180px;display:none"></video>
      <p class="hint" id="look-said"></p>
```

Add the JS beside the music bed's:

```javascript
/* The look picker.

   Each tile plays its own preview, because half of what is being chosen
   is an animation and a still cannot show one. The preview URL comes
   off the row the server sent; spelling the route out here would be the
   panel inventing one it already has. */
let LOOKS = null;

const LOOK_COLOURS = [
  ["gold on white", "&H0000D7FF", "&H00FFFFFF"],
  ["white on grey", "&H00FFFFFF", "&H00909090"],
  ["yellow on white", "&H0000F0FF", "&H00FFFFFF"],
  ["cyan on white", "&H00F0E000", "&H00FFFFFF"],
];

async function loadLooks() {
  if (!PLAN) return;
  const response = await fetch(`/api/plan/${PLAN.plan_id}/looks`);
  LOOKS = await response.json();
  renderLooks();
}

function lookTiles() {
  return LOOKS.looks.map((row) => `
    <figure class="lookcard ${row.look_id === LOOKS.chosen ? "on" : ""}"
            id="lookcard-${esc(row.look_id)}">
      <video muted loop playsinline preload="none"
             data-src="${row.preview}"></video>
      <b>${esc(row.label)}</b>
      <div class="meta">${esc(row.font)} &middot; ${esc(row.punch_animation)}</div>
      <button class="uselook" data-look="${esc(row.look_id)}">Use this</button>
    </figure>`).join("");
}

function renderLooks() {
  if (!LOOKS) return;
  $("look-note").textContent = LOOKS.chosen
    ? `this reel: ${LOOKS.chosen}`
    : `channel default: ${LOOKS.current.look_id}`;
  $("lookgrid").innerHTML = lookTiles();

  $("look-font").innerHTML = LOOKS.fonts.map(
    (f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
  $("look-anim").innerHTML = LOOKS.animations.map(
    (a) => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
  $("look-colour").innerHTML = LOOK_COLOURS.map(
    (c, i) => `<option value="${i}">${esc(c[0])}</option>`).join("");

  /* Loaded on hover, not on render: eight previews at once is eight
     ffmpeg runs the moment the gate opens. */
  $("lookgrid").querySelectorAll("video").forEach((video) => {
    const start = () => {
      if (!video.src) video.src = video.dataset.src;
      video.play().catch(() => {});
    };
    video.onmouseenter = start;
    video.onclick = start;
  });
  $("lookgrid").querySelectorAll("button.uselook").forEach((button) => {
    button.onclick = () => useLook({look_id: button.dataset.look});
  });
}

async function useLook(body) {
  const said = $("look-said");
  said.textContent = "Setting the look…";
  try {
    const response = await fetch(`/api/plan/${PLAN.plan_id}/look`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) { said.textContent = String(data.detail); return; }
    LOOKS.chosen = data.look_id;
    LOOKS.current = data;
    renderLooks();
    said.textContent = `This reel renders in ${data.look_id}.`;
  } catch (err) {
    said.textContent = "Request failed: " + String(err);
  }
}

async function clearLook() {
  await fetch(`/api/plan/${PLAN.plan_id}/look`, {method: "DELETE"});
  LOOKS.chosen = null;
  renderLooks();
  $("look-said").textContent = "Back on the channel default.";
}

$("btn-look-custom").onclick = async () => {
  const [, spoken, upcoming] = LOOK_COLOURS[Number($("look-colour").value)];
  await useLook({
    look_id: "custom", font: $("look-font").value, caption_size: 70,
    spoken: spoken, upcoming: upcoming, margin_v: 300,
    punch_font: $("look-font").value, punch_size: 92,
    punch_animation: $("look-anim").value,
  });
  const preview = $("look-custom-preview");
  preview.style.display = "block";
  preview.src = `/api/look-preview/${PLAN.plan_id}/custom?t=${Date.now()}`;
  preview.play().catch(() => {});
};
```

Call `loadLooks()` from `renderReview()` beside `renderBedNow(data.music)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_look_panel.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Measure it in a browser**

Run the panel against a seeded plan and check, with the probe pattern
used for the clip and sticker boards: every look card fills its grid
track (not 80px short), nothing escapes its card, the page gains no
horizontal scroll, and one preview actually plays.

- [ ] **Step 6: Commit**

```bash
git add engine/ui/index.html tests/test_look_panel.py
git commit -m "fix: pick a reel's look from the clip gate, against a real preview"
```

---

### Task 9: The guard the last three faults would have failed

**Files:**
- Test: `tests/test_look_no_jitter.py`

**Interfaces:**
- Consumes: `looks.PRESETS`, `captions.build_ass`, ffmpeg.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_look_no_jitter.py`:

```python
r"""No look may move the caption block.

Three caption treatments shipped and a viewer caught each one. The one
that mattered most was invisible to every test in the suite: `\fscy` on
the active word grows the *line box*, so a block anchored at the bottom
walks up the frame. Measured on the rendered reel, 83px of travel.

The test that let it through asserted the line did not move sideways.
It does not. The vertical was the axis that broke.

This renders each preset's captions over black and tracks the block's
top edge through a beat. Slow -- one ffmpeg run per sample -- so it
samples sparsely and runs over presets rather than over frames.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from engine.assembly.captions import build_ass
from engine.assembly.looks import PRESETS
from engine.config import Settings
from tests.factories import make_plan

W, H = 1080, 1920


def _plan():
    from engine.media.voice import caption_timings

    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]
    beat.caption_text = ("Lok Sabha ya Rajya Sabha pahunchna darwaza hai "
                         "lekin sansad banna kaafi nahi")
    beat.on_screen_text = "Not Enough"
    beat.measured_seconds = 4.0
    beat.words = caption_timings(beat.caption_text, 4.0)
    return plan


def _top_edge(settings, ass_path, at):
    """The topmost lit row of the caption band, over black."""
    raw = subprocess.run(
        [settings.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={W}x{H}:d=8", "-ss", str(at),
         "-frames:v", "1", "-vf",
         f"subtitles={ass_path.name},crop={W}:600:0:1320,format=gray",
         "-f", "rawvideo", "-"],
        cwd=str(ass_path.parent), capture_output=True).stdout
    if len(raw) < W * 600:
        return None
    for y in range(600):
        if max(raw[y * W:(y + 1) * W]) > 150:
            return y
    return None


@pytest.mark.parametrize("look_id", sorted(PRESETS))
def test_no_preset_moves_the_caption_block(look_id, tmp_path):
    settings = Settings()
    ass_path = tmp_path / f"{look_id}.ass"
    ass_path.write_text(build_ass(_plan(), look=PRESETS[look_id],
                                  width=W, height=H), encoding="utf-8")

    tops = [t for t in (_top_edge(settings, ass_path, at)
                        for at in (0.4, 1.0, 1.6, 2.2, 2.8, 3.4))
            if t is not None]

    assert tops, f"{look_id} rendered no captions at all"
    assert max(tops) - min(tops) <= 2, (
        f"{look_id} moves the caption block {max(tops) - min(tops)}px; "
        f"83px of this shipped once")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_look_no_jitter.py -q`
Expected: FAIL on import if Task 1 and 2 are not done; PASS once they
are. To prove the test bites, temporarily add `\fscy130` to a caption
word in `captions._pop_tags` and confirm it goes red.

- [ ] **Step 3: No implementation**

This task adds no code. If it fails, the failing preset's animation is
leaking onto the caption line and that is the bug to fix.

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS. The suite takes about 14 minutes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_look_no_jitter.py
git commit -m "test: no look may move the caption block"
```

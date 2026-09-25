# A look you can see before you choose it

Let the caption font, its colour, where the block sits and how the punch
line arrives be chosen from the panel, against a preview cut from the
reel's own footage.

## Why

Three attempts were made to improve the caption treatment by reasoning
about it, and a viewer caught each one:

| Attempt | What it cost |
|---|---|
| `\fscy 130` on the active word | grew the *line box*, walking the caption block **83px** up the frame |
| a white glow | white already means "not said yet", so every word flashed it — read as blinking |
| a gold glow | the outline is 5px and already gold, so any visible blur filled the counters |

Each was measured only after it shipped. The pattern is not that the
effects were wrong; it is that a still, a pixel count and an argument
are all worse than looking at it. The last two rounds of this were
settled in minutes once real frames were on screen.

The same is true of the choices that were never offered at all. The
caption font has been Arial since the first commit, and `caption_font`
is a setting nobody has had a reason to change because there was no way
to see what changing it would do. Rendered side by side on real
footage, the same line takes four lines in Arial, three in Poppins and
two in Anton — which is not a matter of taste but of how much frame the
captions eat.

## What a look is

One value object, in `engine/assembly/looks.py`:

```
Look:
    look_id            "blocky-urban"
    label              "Blocky Urban"
    font               "Bungee"
    caption_size       56
    spoken             "&H0000D7FF"     gold
    upcoming           "&H00FFFFFF"     white
    margin_v           300
    punch_font         "Bungee"
    punch_size         72
    punch_animation    "letters"
```

`caption_size` belongs to the look and not to settings, because the
fonts are not interchangeable at one size: Bungee at 72px is enormous
and Playfair Display at 72px is small. Each preset carries the size that
font actually wants.

`build_ass` currently takes `font`, `font_size`, `margin_v` and
`width`/`height` as separate arguments, and `pipeline.render_stage`
passes four of them from four different places. It takes a `Look` and
the frame size instead; that is the one refactor this needs and it
shrinks the call site.

## The presets

Seven, each a different register rather than a different accent. The
first is the one already chosen by eye; the last is what ships today, so
that turning this on changes nothing until something is picked.

| id | font | punch | colour | why it is here |
|---|---|---|---|---|
| `blocky-urban` | Bungee 56 | letters | gold / white | chosen from the sheets — blocky, urban, unmistakable |
| `clean-modern` | Outfit Black 70 | stamp | gold / white | the safe modern default |
| `editorial` | Playfair Display Black 68 | blur-in | white / dim grey | serif, quiet, for a slower story |
| `poster` | Staatliches 84 | bounce | bright yellow / white | condensed caps, loudest |
| `indian-display` | Teko 92 | swing | gold / white | condensed, and the only one whose font also draws Devanagari |
| `techno` | Chakra Petch 70 | flash | cyan / white | squarish, for anything technical |
| `plain` | Arial 72 | fade | gold / white | exactly what ships now |

Every preset uses `margin_v` 300, which is where the block sits today.
Rendered with the phone's own controls drawn in, 220 puts the second
line under them and 160 puts both — so the block does not move, and a
shorter font is what buys room rather than a smaller margin. `margin_v`
stays on the look so a future preset can differ; none does yet.

`custom` is an eighth entry that is not a preset: it carries whatever
font, animation and colour were picked in the panel's three dropdowns.

### The punch animations

All seven are expressible as ASS override tags on the punch's own
Dialogue event. That event is `Alignment 5`, `MarginV 0`, on its own
layer, so it is positioned independently of the caption block —
scaling it cannot move the captions, which is the trap the `\fscy`
attempt fell into.

`letters` is the one that needs care. The obvious implementation gives
each character its own `\pos` and staggers them; measured, that
overlaps, because a fixed advance per character does not match the
glyphs — "Not Enough" rendered with `N`, `E` and `g` colliding. The
implementation that works keeps **one** Dialogue and gives each
character its own override block changing only `\alpha`, `\blur` and
`\1c`. libass then does the layout and only the paint is animated.

That is also the rule for every animation here: on the punch, anything
goes; on the captions, nothing that changes a metric.

## Where a look lives

The shape `music_choices` already uses, because it already works:

* **Global default** — `settings.look`, a preset id. The channel's look.
* **Per reel** — a `look_choices` table keyed by `plan_id`. One row,
  replaced not appended, as `music_choices` does. It holds a `look_id`
  plus the resolved fields (font, size, colours, margin, punch font and
  animation). A preset stores its id *and* its values: the id is what
  the panel highlights, and the values are what renders, so a preset
  whose definition later changes does not silently restyle a reel that
  was already reviewed. `custom` stores `look_id = "custom"` with the
  same fields.
* At render: the reel's own look, else the global default, else `plain`.

A stored look that names a preset which no longer exists falls back to
the default rather than failing the render, and says so — the same rule
the bed and the baked art follow.

## The preview

`GET /api/plan/{plan_id}/look-preview` renders a short clip of that
reel's own footage with a given look, and serves it.

It renders on demand and is **not cached**. Measured:

| output | duration | render time |
|---|---|---|
| 1080x1920 | 2.5s | 0.9s |
| 540x960 | 2.5s | **0.4s** |
| 540x960 | 4.0s | 0.6s |

At 0.4s a cache buys nothing and costs invalidation: a preview is wrong
the moment the beat's clips, its caption or its timings change, and
three sources of staleness is three bugs. If it ever gets slow, a cache
is easy to add behind the same route.

540x960 and 2.5 seconds, because the preview is answering "is this the
right look", not "is this the right footage".

**Which beat.** The first beat that has both `on_screen_text` and word
timings, so the punch animation is actually visible; failing that, the
first beat with timings; failing that, a 404 saying the reel has not
been voiced yet. A preview that silently shows no punch would be
answering half the question.

**With what footage.** That beat's first clip if it is on disk. A beat
whose slot is still unfilled previews over flat black rather than
failing — the look is legible against black, and a picker that refuses
to open because one clip is missing is worse than one that shows the
type on a plain ground.

## Fonts

Six families plus a Devanagari fallback, committed under
`assets/fonts/`:

| font | size |
|---|---|
| Bungee | 107 KB |
| Outfit Black | 47 KB |
| Playfair Display Black | 120 KB |
| Staatliches | 56 KB |
| Teko | 150 KB |
| Chakra Petch | 68 KB |
| Noto Sans Devanagari | 626 KB |
| **total** | **1.1 MB** |

All SIL Open Font License, which permits commercial use and bundling;
each font's `OFL.txt` is committed beside it.

The render must be told where they are. `subtitles=` is currently
invoked with no `fontsdir`, so libass resolves names against the system
font set and a bundled font would simply not be found — the reel would
render in the fallback and nothing would say so. Both the render and
the preview pass `fontsdir`.

Arial stays reachable as a system font, which is what `plain` uses.

## The panel

A "Look" block on the clip gate, beside the music bed:

* A strip of preset tiles, each playing its own preview on a loop.
* A "Custom" panel below it: three selects — font, punch animation,
  caption colour — and one preview that re-renders when any changes.
* The current choice is marked, and clearing it returns the reel to the
  channel default.

Previews are `<video muted loop playsinline>` pointed at the route, the
same way the clip board plays its candidates.

## Testing

* `looks.resolve()` — every preset produces the ASS values it claims,
  and an unknown id falls back rather than raising.
* Round trip — preset through `build_ass` and back out: the font,
  colours, size and margin that come out are the ones that went in.
* The preview route — 200 and `video/mp4` for a voiced plan, 404 for one
  with no timings, and the same containment check `/media`,
  `/api/frame`, `/api/audio` and `/api/music` already carry.
* Per-reel beats global; nothing chosen falls to the default; a stored
  preset id that no longer exists falls back and says so.
* **No punch animation moves the caption block.** Rendered per preset,
  the block's top edge is measured across the punch's whole window and
  must not move. This is the test that the three shipped faults would
  each have failed, and it is the reason this section exists.
* No caption treatment emits a metric-changing tag — not `\fscx`, not
  `\fscy`, not `\fs`.

## What is deliberately not here

**No caching of previews.** Argued above.

**No font upload.** The six are chosen and committed. A panel that
accepts a font file would have to validate it, store it, and answer what
happens to a reel whose font was later deleted — none of which serves
picking a look.

**No per-beat looks.** One look per reel. A reel that changes caption
font halfway is a mistake, not a feature.

**The sticker and music pickers are untouched.** They already work and
sit on the same gate; this adds a third block beside them rather than
a shared "style" screen that would mean rewriting two working things.

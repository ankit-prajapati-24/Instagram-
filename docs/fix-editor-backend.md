# Fixes for EDITOR-_BACKEND

Three problems in `EDITOR-_BACKEND`
that will bite as soon as it is driven by something other than a hand-written
call. Rahasya renders with ffmpeg directly, so none of these block it — but
`engine/assembly/compile.py` exists to let that service act as an alternative
renderer, and it needs these first.

---

## 1. The hardcoded asset path silently rewrites absolute paths

`final/generate_video_final.py`, near the top of
`create_video_from_image_and_audio`:

```python
BASE = "C:\\editor_project\\assets\\"

def _with_base(p):
    return p if os.path.isabs(p) else os.path.join(BASE, p)
```

`main.py` already resolves paths through its own `_resolve_path` against
`ASSETS_DIR`, so a relative path gets joined to `assets/` there and then joined
to `C:\editor_project\assets\` here. Any caller that does not happen to use
that exact directory gets a `FileNotFoundError` naming a path it never asked
for.

```python
BASE = os.environ.get(
    "EDITOR_ASSETS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "assets"))
```

Defaulting to a directory beside the repo makes it work on a fresh clone, and
`EDITOR_ASSETS_DIR` covers every other layout. The same literal appears in the
`audio_path` and `output_path` defaults in the signature — those should become
`None` and be resolved inside the function, since a default pointing at one
machine's directory is never right anywhere else.

## 2. An API key in the source file

`agent.py`:

```python
API_KEY = ""
client = Groq(api_key=API_KEY)
```

It is empty in the repo, which is lucky — the risk is the commit that fills it
in. Read it from the environment instead, and while you are in there, point the
client at OmniRoute so this service shares the gateway:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("OMNIROUTE_BASE", "http://localhost:20128/v1"),
    api_key=os.environ.get("OMNIROUTE_KEY", "omniroute"),
)
```

The Groq SDK is OpenAI-compatible, so `client.chat.completions.create(...)`
in `ai_plan_video` keeps working unchanged. One thing does have to change:
**OmniRoute requires a `model` field** — a request without one returns
`{"error": {"message": "Missing model"}}`. `auto/best-chat` works as a default.

Add `.env` to `.gitignore` in that repo if it is not there already.

## 3. moviepy has no ffmpeg binary to call

`requirements.txt` lists `moviepy` but nothing that provides ffmpeg, so the
first render fails on a machine without it on PATH. On this machine `where
ffmpeg` finds nothing.

```
imageio-ffmpeg
```

That ships a binary and moviepy finds it automatically. The one it installs
here is ffmpeg v7.1 built with `libass`, `libharfbuzz` and `fontconfig`, which
is also what makes correct Devanagari subtitle rendering possible.

---

## A note on `overlysubtitletovideo.py`

`overlay_subtitles()` is the most valuable function in that repo, and it will
not work on Windows as written.

It renders text through GI / Pango / PangoCairo, which is the right choice —
those do proper OpenType shaping, so Devanagari conjuncts and matras land
correctly. But PyGObject on Windows needs an MSYS2 toolchain, and the setup
comments in the file are all `apt-get`, so it was written for Colab or Linux.
When the import fails it falls back to Pillow, and **Pillow on this machine
reports `RAQM=False`**:

```
python -c "from PIL import features; print(features.check('raqm'))"
False
```

Without libraqm, Pillow cannot shape complex scripts. Hindi captions come out
with matras in the wrong places and broken conjuncts — worse than no captions,
because it looks careless rather than absent.

Three ways forward, in the order I would try them:

1. **Use ffmpeg's `subtitles` filter instead**, which is what Rahasya does.
   libass does the shaping, it is much faster than per-frame Python
   compositing, and karaoke word highlighting comes free with `\k` tags. See
   `engine/assembly/captions.py`.
2. **Keep the Pango path but run it on Linux** — WSL or a Linux VPS. The code
   is already correct there.
3. **Shape with `uharfbuzz` and draw the glyphs yourself.** It pip-installs
   cleanly on Windows (0.56.1 is installed here) and gives exact control, but
   it is real work: you handle glyph positioning, line breaking and clusters
   by hand.

Option 1 is what I would keep. The Pango version is worth preserving for a
Linux deployment, but it should not be the only path.

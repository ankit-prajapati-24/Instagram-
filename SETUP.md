# Setup

Getting this running from a fresh clone on a Windows machine. Roughly 20
minutes, most of it downloads.

Everything below was measured on the machine this was built on, not copied
from a template. Where something is unverified or known to be missing, it says
so rather than pretending.

---

## What you need first

| Thing | Version here | Notes |
|---|---|---|
| Python | 3.14.3 | 3.11+ should work; 3.14 is what everything was tested on |
| Node.js | v22.23.2 | only needed for OmniRoute |
| Git | any | |
| Disk | ~1.5 GB | the voice model is 61 MB, ffmpeg ~80 MB, the rest is Node |

Windows already ships the two fonts the captions use — **Arial** for Roman and
**Nirmala UI** for Devanagari. Nothing to install.

You do **not** need to install ffmpeg. It arrives as a Python package
(`imageio-ffmpeg`), already built with libass and HarfBuzz, which is what makes
the burned-in captions shape correctly.

---

## 1. Clone and install

```bash
git clone https://github.com/ankit-prajapati-24/Instagram-.git
cd Instagram-
pip install -r requirements.txt
```

That pulls in the whole pipeline including `piper-tts` (the voice) and
`imageio-ffmpeg` (the renderer).

Check it landed:

```bash
python -c "import piper, imageio_ffmpeg, fastapi; print('ok')"
```

---

## 2. Install OmniRoute

This is the AI gateway. Everything that writes text goes through it.

```bash
npm install -g omniroute
```

Start it:

```bash
node "%APPDATA%\npm\node_modules\omniroute\bin\omniroute.mjs"
```

First start takes about 45 seconds. It listens on **port 20128**. Leave that
window open.

Confirm it answers:

```bash
curl http://127.0.0.1:20128/v1/models
```

### Add a provider

**OmniRoute does not give you AI access.** It routes between providers *you*
add keys for. Out of the box it has none, and every request fails with
`Maximum combo retry limit reached`.

Open its dashboard:

```bash
node "%APPDATA%\npm\node_modules\omniroute\bin\omniroute.mjs" open
```

Add a provider there, then restart the gateway. Keys can also go in
`C:\Users\<you>\.omniroute\.env` directly.

`docs/omniroute-setup.md` has the detail, including which capabilities each
provider lights up.

---

## 3. First run

```bash
start.bat
```

That launches OmniRoute and the panel in separate console windows and opens
your browser at **http://127.0.0.1:8765**. `stop.bat` shuts both down.

To run only the panel:

```bash
python -m engine.app
```

The panel header tells you the truth about the gateway:

| Header says | Meaning |
|---|---|
| **gateway ready** | a provider answered a real completion |
| **no provider** | gateway is up but has no working key — falls back to a built-in sample script |
| **down** | gateway is not running |

Even on **no provider**, everything after the script is real — voice, captions,
images, render, QC. You can watch the whole pipeline work before spending
anything.

---

## 4. Make your first video

1. Type **one** topic, one line. One mystery per video.
   The panel refuses anything over 160 characters, because pasting a list of
   topics used to produce an unopenable filename.
2. Pick a hook from the variants.
3. Edit any beat you want — text, visual prompt, motion.
4. **Approve and render.**

The MP4 lands in `outputs/`. The evidence log down the left reports measured
facts as each stage finishes, not progress guesses.

---

## 5. Check it actually works

```bash
python scripts/verify_e2e.py        # full pipeline, fake brain, everything else real
python scripts/probe_omniroute.py   # which gateway endpoints answer
python scripts/reset_cooldown.py    # what the dedup gate is blocking
python -m pytest -q                 # 330 tests
```

`verify_e2e.py` exits non-zero unless it produced a playable 1080x1920 MP4 with
audio and no QC hard failures.

---

## Configuration

Copy `.env.example` to `.env` and edit. Every value is optional — the file
documents the defaults. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `RAHASYA_VOICE_ENGINE` | `piper` | `piper` (offline, MIT) or `edge` |
| `RAHASYA_PIPER_VOICE` | `pratham` | downloads on first use, ~61 MB |
| `RAHASYA_PIPER_LENGTH` | `1.12` | pace; `1.0` is Piper's natural speed |
| `RAHASYA_CAPTIONS` | `caption_text` | Roman Hinglish, or `voice_text` for Devanagari |
| `RAHASYA_DAILY_USD` | `2.0` | pipeline pauses when the day's spend crosses this |
| `RAHASYA_KEYLESS_IMAGES` | `1` | set `0` to skip the free image tier |
| `PEXELS_API_KEY` | _(none)_ | stock footage for scenes, the primary visual source; free to register, and without it every scene falls back to the still-image chain |
| `RAHASYA_VIDEO_GRADE` | `1` | one colour grade + vignette over every frame, so 20-odd clips from different Pexels creators read as one video; `0` renders ungraded |
| `RAHASYA_VIDEO_GRAIN` | `9` | film grain strength within the grade; this is the expensive part of the render (it wrecks inter-frame compression, not the CPU cost of the filter itself) — `0` keeps the colour work and vignette but skips the grain and most of the extra render time |

**Provider API keys do not go in this file.** They belong to OmniRoute, in its
own env file or dashboard.

---

## When something breaks

**Port already in use** — `[Errno 10048] error while attempting to bind`.
Something is already on 8765. Run `stop.bat`, or find it:
```bash
netstat -ano | findstr :8765
```

**The panel renders an old script after you changed a prompt.**
Prompts are read from disk per call, but a stale panel process holds stale
code. Restart the panel.

**`GateError: cooldown`** — the dedup gate is refusing your topic because a
named entity in it appeared in the last 45 days. This is working as designed.
See what is blocked, and clear it if the recorded entities are wrong:
```bash
python scripts/reset_cooldown.py
```

**Voice comes out sounding like a different engine.** Check `voice_engine` on
the beats — the fallback used to be silent. It is recorded per beat now and
shown in the panel.

**Images come out as plain dark gradients.** That is tier 3, the placeholder.
It means both the gateway and the free image endpoint failed. See below.

---

## Known gaps

These are real, current, and not hidden:

**Visuals depend on a free Pexels key.** The chain is Pexels → the still-image
chain → a placeholder frame. Without `PEXELS_API_KEY` the pipeline still
produces a video, but every scene is a still, and the panel's scene strip will
say `fallback` on each one. The old image path is still there underneath and
still carries its own limits: the gateway tier exhausted its quota on
2026-09-18 with a 163-hour reset, and the free keyless endpoint watermarks and
upscales. On a real 12-beat run with a Pexels key, all 12 beats matched real
footage with zero fallbacks, so the fallback chain below is a safety net, not
the expected path.

**The Pexels licence terms have not been independently checked.** Downloaded
clips are tagged `licence='pexels'`, which records the source, not a verified
commercial clearance. Check Pexels' terms before monetising.

**There is no music or sound effects.** `engine/assembly/render.py` supports
mixing in a music bed, but nothing supplies one and `assets/music/` is empty,
so every video renders over silence. Short-form retention research weighs
sound design heavily, so this is a real gap, not a nicety — it is the next
thing worth fixing, not a footnote.

**Whether the grade actually makes 20-odd stock clips read as one video is
still a judgement call, not a measurement.** The colour grade, vignette and
grain (see `README.md`) were chosen from rendered comparison frames, and the
render cost is measured (143.0s → 223.1s on a 49.8s video, `RAHASYA_VIDEO_GRAIN=0`
brings it to 187.5s). Whether the *look* actually reads as one coherent piece
across a full finished render, rather than generic stock filler, still needs
eyes on a real output.

**Moderation is not running.** It routes to its own provider (OpenAI by
default) and needs credentials like anything else. Until then the safety gate
reports `NOT CHECKED` rather than passing silently — but it is a weakened
constraint, and you should know that.

**Semantic dedup layer 3 is skipped** without an embeddings provider. Layers 1,
2 and 4 (exact hash, trigram overlap, entity cooldown) still run.

**There is no publishing.** `engine/publish/payloads.py` imports no HTTP
library and a test asserts it stays that way. It builds the payloads; you copy
them out and upload by hand. This is deliberate — see the constraints section
in `README.md`.

---

## Where to read next

| File | What it covers |
|---|---|
| `README.md` | architecture, and the decisions worth knowing before changing anything |
| `docs/omniroute-setup.md` | providers, capabilities, what each key unlocks |
| `docs/playbook-retention-monetization.md` | the retention and monetization rules, in Hinglish |
| `engine/prompts/` | the retention rules as actual constraints on the model |

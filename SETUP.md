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
| Disk | ~1.5 GB | the voice model is 61 MB, ffmpeg ~80 MB, the rest is Node. Add ~40 MB if you turn on `RAHASYA_ALIGN` |

You do not need to install any font. The caption faces ship in
`assets/fonts/` and are copied next to the subtitle file at render time, so a
reel looks the same on every machine — see `engine/assembly/fonts.py`.

You do **not** need to install ffmpeg. It arrives as a Python package
(`imageio-ffmpeg`), already built with libass and HarfBuzz, which is what makes
the burned-in captions shape correctly.

---

## 1. Clone and install

```bash
git clone <your-repo-url> rahasya
cd rahasya
pip install -r requirements.txt
```

That pulls in the whole pipeline including `piper-tts` (the voice) and
`imageio-ffmpeg` (the renderer).

Check it landed:

```bash
python -c "import piper, imageio_ffmpeg, fastapi; print('ok')"
```

Then generate the placeholder audio, once:

```bash
python scripts/make_audio_assets.py
```

That writes a music bed into `assets/music/` and three sound effects into
`assets/sfx/`. They are synthesised by ffmpeg, they are gitignored, and
**they sound cheap** — they are a stand-in for a sound designer, not one.
Skip this step and the pipeline still runs; the video just comes out over
silence the way it did before. See [Audio](#audio) for how to replace them.

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

### Stopping to check the work

Two tick-boxes under **Approve and render** each park the run so you can
look at what it made before it spends the expensive stages on it.

**Hear every beat** (`review_voice`) stops after the narration is spoken.
Each beat gets a player, the Devanagari line it is meant to say with a copy
button, and an upload control. To say a beat in some other voice — your own,
or whatever tool you like — copy the line, generate it there, and upload
what comes back. mp3, wav, m4a, ogg, flac, or the video your phone
recorded; the sound is taken out of it, re-encoded to match the rest of the
narration and levelled to the same loudness, so an uploaded beat does not
jump out next to a synthesised one. The synthesised original is left on
disk untouched.

Before that re-encode, the upload goes through a cleanup chain: a highpass
to clear room rumble, a denoiser for a steady noise floor, a de-clicker for
mouth noise, and a cap on any silence longer than a natural pause. The raw
upload — exactly the bytes you handed over — is kept on disk beside the
cleaned file, and the board plays both: a second player under the beat's
own audio holds that raw take, and a checkbox next to it switches the beat
between the cleaned file and the untouched upload, re-measuring the beat's
length either way. The line under the checkbox says what cleanup actually
did to that beat — seconds of pause trimmed and the loudness move — read
off the plan itself, never a fixed number written into the page. If
cleanup empties a take entirely (nothing left above the noise floor), it
is abandoned and the raw level is used instead, and the board says that
plainly rather than reading the same as cleanup being off.

| Variable | Default | What it does |
|---|---|---|
| `RAHASYA_VOICE_CLEAN` | `1` | turns the cleanup chain on for uploads; `0` writes the raw upload's own level, the same as switching it off on the board |
| `RAHASYA_VOICE_HIGHPASS` | `80.0` | highpass cutoff in Hz — below any voiced fundamental in Hinglish narration, above typical room rumble |
| `RAHASYA_VOICE_DENOISE` | `-25.0` | afftdn's noise floor estimate in dB; more negative trusts more of the signal as speech, less negative denoises harder |
| `RAHASYA_VOICE_PAUSE_CAP` | `0.35` | no silence in the recording survives longer than this, in seconds, while a natural inter-word gap is left untouched |
| `RAHASYA_VOICE_SILENCE_DB` | `-45.0` | below this a sample counts as silence to trim; clears typical room noise without eating a soft word |

`RAHASYA_VOICE_PAUSE_CAP` is the number to change if pauses feel clipped.

If the voice simply says a word wrong you do not need a recording at all.
Both lines are editable in place — the Devanagari one that gets spoken and
the Roman one that gets burned — and **Say it again** re-speaks *only* that
beat. They move independently on purpose: respell the word phonetically in
the spoken line and the caption keeps the real spelling, so the listener
hears it right and the viewer reads it right. A caption-only edit does not
re-speak anything; the audio has not changed, only where its words fall.

The spoken line has to be Devanagari — the same rule the script gate
enforces, through the same code. Latin script is refused with the
offending words named. That is not a style rule: the voice reads Latin
letters as English and mispronounces them.

Replacing or re-speaking a beat changes how long the whole video is, so
the line above the list tracks the total against both windows: the range
that will render at all, and the narrower range QC will publish. Nothing
after this point shortens a script.

This gate is before the footage is fetched, and it has to be: a beat's clip
count is `ceil(measured / 2.5)` and its slot lengths divide the measured
span, so clips fetched first would be cut to a length that no longer
exists.

**Check every clip** (`review_clips`) stops after the footage is in, with
one card per clip and the search phrase that found it. Either box can be
ticked, or both — with both, the run stops at the voice first and asks
again about the clips when you release it.

---

## 5. Check it actually works

```bash
python scripts/verify_e2e.py        # full pipeline, fake brain, everything else real
python scripts/probe_omniroute.py   # which gateway endpoints answer
python scripts/reset_cooldown.py    # what the dedup gate is blocking
python -m pytest -q                 # 620 tests
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
| `RAHASYA_ALIGN` | `0` | measure caption word timings out of the synthesised audio with faster-whisper instead of interpolating them. **Off by default** — it downloads a model on first use. See [Caption word alignment](#caption-word-alignment) |
| `RAHASYA_ALIGN_MODEL` | `base` | which Whisper model aligns; `tiny` is faster per second of audio but slower to load, `small` is slower than realtime and useless here |
| `RAHASYA_DAILY_USD` | `2.0` | pipeline pauses when the day's spend crosses this |
| `RAHASYA_KEYLESS_IMAGES` | `1` | set `0` to skip the free image tier |
| `PEXELS_API_KEY` | _(none)_ | stock footage for scenes, the primary visual source; free to register, and without it every scene falls back to the still-image chain |
| `RAHASYA_VIDEO_GRADE` | `1` | one colour grade + vignette over every frame, so 20-odd clips from different Pexels creators read as one video; `0` renders ungraded |
| `RAHASYA_VIDEO_GRAIN` | `9` | film grain strength within the grade; this is the expensive part of the render (it wrecks inter-frame compression, not the CPU cost of the filter itself) — `0` keeps the colour work and vignette but skips the grain and most of the extra render time |
| `RAHASYA_MUSIC` | `1` | background bed from `assets/music/`, auto-ducked under the voice; `0` renders over silence. See [Audio](#audio) |
| `RAHASYA_MUSIC_LUFS` | `-25` | where the bed sits before ducking, as an absolute loudness; the voice is at `-15`, so this is 10 LU under it |
| `RAHASYA_SFX` | `1` | whooshes on the cuts that mark a turn, a pop per sticker, one sub-bass hit on the hook; `0` removes the layer |
| `RAHASYA_SFX_WHOOSH_MAX` | `3` | how many cut whooshes may fire in one video |

**Provider API keys do not go in this file.** They belong to OmniRoute, in its
own env file or dashboard.

---

## Caption word alignment

Off by default. Turn it on with `RAHASYA_ALIGN=1` in `.env`.

**What it does.** The burned captions highlight word by word, and the emoji
stickers pop on a particular word. Both need to know when each caption word is
spoken. Normally those times are a guess: the beat's span is measured exactly
from its audio file, and the caption's words are spread across it by character
count. With this on, `faster-whisper` listens to the audio the pipeline just
synthesised and reports where each word actually landed.

**The download.** The `base` model is fetched from Hugging Face the first time
you run with it on — about 40 MB, into `~/.cache/huggingface/hub` (set
`HF_HOME` to move it). Nothing downloads while the switch is off, which is the
whole reason it is off: a clean clone should not stall mid-render on a network
fetch nobody asked for. To pull it ahead of time:

```bash
python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"
```

No GPU and no PyTorch. `faster-whisper` runs on `ctranslate2`, which ships a
CPU wheel for Python 3.14.

**What it costs.** Measured here on a ten-beat, 38.5-second script: the model
loads once per run (7.5s cold, 2.8s from warm disk cache) and transcription
runs at about 2.8x realtime, so `synth_plan` went from 49.1s to 63.9s. Call it
**+15 seconds on a full run** — against a render that takes minutes.

**What it buys.** Less than you might hope, and that is worth saying plainly.
Over those ten beats, five came back with a Whisper token count matching the
caption and got true per-word timings; the other five did not and fell back to
interpolation inside the *measured speech span*, which is still a real
improvement because Piper leaves 0.25-0.57s of silence at the end of every
beat file that plain interpolation spends caption time on. The per-word
difference against plain interpolation averaged **0.151s**, worst case
**0.375s**. It is polish on a 3-5 second beat, not a transformation.

**Why the counts disagree.** Whisper hears this Piper voice as Urdu and
transcribes Hindi into Arabic script — `اسم` for `असम` — so its text can never
be matched to our Roman Hinglish caption words. Only position can be. Forcing
`RAHASYA_ALIGN_LANG=hi` does not change that; it only skips the detection pass
and roughly halves the time.

**When it fails.** It falls back to the old interpolation and the run
continues — a missing model, no network on first use, an empty transcription,
a count it will not salvage. It never fails a render. Which path each beat
took is printed as it goes:

```
[align] b1: whisper-span (10 spoken tokens vs 9 caption words)
[align] whisper=5, whisper-span=5
```

and recorded on each beat as `word_timing_source`, the same way
`voice_engine` records which TTS engine actually spoke. A run that silently
interpolated everything says `[align] interpolated=10`.

---

## Audio

Three tracks go into every render: the narration, a music bed ducked out of
its way, and a thin layer of sound effects.

### Replacing the placeholders

There is no filename to configure and no code to change. Discovery prefers
**any file that is not named `placeholder-`**, and the renderer measures
whatever it finds and normalises it, so the level of the file you drop in
does not matter either.

| Put a file here | And it becomes |
|---|---|
| `assets/music/<anything>.mp3` (or `.wav`, `.m4a`, `.ogg`, `.opus`, `.flac`) | the background bed |
| `assets/sfx/whoosh.wav` or `whoosh-<anything>.<ext>` | the cut whoosh |
| `assets/sfx/pop.wav` or `pop-<anything>.<ext>` | the sticker pop |
| `assets/sfx/subdrop.wav` or `subdrop-<anything>.<ext>` | the sub-bass hit on the hook |

The rule in full: a file belongs to a sound kind when its name, with any
leading `placeholder-` stripped, is the kind's name or starts with the
kind's name and a hyphen. If two files match, the one that is not a
placeholder wins; if both are real, alphabetical order decides. For the
music bed, any audio file in `assets/music/` is a candidate and the same
preference applies — so you can leave the placeholder in place while you
audition a real track.

You do not need to delete the placeholders, and
`scripts/make_audio_assets.py` will not overwrite a file that already
exists (pass `--force` if you want the placeholders regenerated).

### What the placeholders actually are

Four ffmpeg-synthesised tones, and they sound like it:

* **the bed** — four low sines (55/82.5/110/165 Hz) a fifth and an octave
  apart, under a 0.05 Hz tremolo, rolled off at 900 Hz with a long echo.
  Sixty seconds, with every frequency an exact multiple of 1/60 Hz so the
  loop seam is silent.
* **the whoosh** — pink noise through a flanger with a swell in the middle.
  ffmpeg cannot sweep a filter's cutoff over time, which is what a real
  whoosh is, so the moving comb stands in for it.
* **the pop** — two damped sines an octave apart, the upper decaying faster.
* **the sub-drop** — one sine falling 90 Hz → 30 Hz over 1.2s.

They exist so a clean clone renders a video you can *hear* the feature in.
Do not ship them as a finished sound design.

### Ducking

The bed does not sit at a fixed level. `sidechaincompress` in the render
graph is keyed on the narration itself, so the music drops while the voice
speaks and comes back up in the gaps between beats — which is the whole
point, and which a static gain cannot do.

Measured against a real 49.5s Piper narration: 7.1 dB of gain reduction
(median) while the voice is speaking, 9.2 dB at the 90th percentile, and
**0.0 dB** — full recovery — in the real pauses between beats, while the
50-150 ms gaps between words inside a sentence never recover, so it lifts
between lines rather than pumping inside them. With the bed already sitting
10 LU under the voice, that puts the music about 18 dB down under speech.

`RAHASYA_MUSIC_LUFS` moves the bed up or down; `RAHASYA_MUSIC_DUCK=0` mixes
it flat instead, if you want to hear the difference.

### Sound effects, and why there are so few

A whoosh only on cuts into a beat whose role is a turn in the story
(`reveal`, `twist`) — the same beats the picture already pushes in on —
capped at three and never within 4 s of each other. A pop on each emoji
sticker, so at most three. One sub-bass hit under the hook. Seven sounds
over fifty seconds, at the outside.

That restraint is the feature, for the same reason `RAHASYA_STICKER_MAX` is
three: ten beats is nine cuts, and a sound on every cut stops meaning
"something changed" and becomes the texture of the video.
`RAHASYA_SFX_WHOOSH_MAX` raises the cap if you disagree; `RAHASYA_SFX=0`
removes the layer entirely.

### Sticker art

The designed stickers are committed as baked PNG sequences, so a normal
render needs nothing here. Only when the art or the grade changes:

    python scripts/fetch_sticker_art.py    # download source GIFs
    python scripts/bake_stickers.py        # bake both styles

The art comes from Lordicon under its free licence, which requires a visible
credit. The panel puts `Animated icons by Lordicon.com` in the metadata
whenever a designed sticker rendered — paste it into the post description.

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

**The audio is plumbed but the sounds are placeholders.** The music bed, the
ducking and the SFX layer are built, wired and tested — a committed test
renders with ffmpeg and measures the duck in decibels rather than asserting
on the filtergraph string. What is *not* built is the sound design: the four
files `scripts/make_audio_assets.py` synthesises are ffmpeg tones and they
sound like it. Sourcing or commissioning a real bed and three real one-shots
is the remaining work, and it needs no code — see [Audio](#audio).

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

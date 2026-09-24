# Rahasya Engine

One Hinglish mystery topic in, one QC-passed 50-second vertical MP4 out, with a
local panel to run and inspect every stage.

Built for Instagram Reels first and YouTube Shorts second, because on a
Hindi/India audience that is where the money actually is —
`docs/playbook-retention-monetization.md` has the numbers.

```
                      topic (one line, typed by you)
                                   |
   OmniRoute  localhost:20128 ─────┤  research · hooks · script · metadata
   (chat, images, embeddings,      |  moderation · semantic dedup
    moderation)                    |
                                   v
                          [ you approve or edit ]
                                   |
      local Piper (hi-IN) ─────────┤  hi-IN voice, measured per beat
      clip chain ──────────────────┤  pexels -> image -> placeholder
                                   v
      ffmpeg v7.1 ─────────────────┤  zoompan · xfade · libass · loudnorm
                                   v
            outputs/*.mp4  +  metadata to copy out
                     (publishing stays manual, on purpose)
```

## Run it

Setting up from scratch on a new machine? **`SETUP.md`** walks the whole thing,
including OmniRoute and the known gaps. The short version:

```bat
pip install -r requirements.txt
start.bat
```

`start.bat` brings up OmniRoute and the panel in their own console windows, so
they keep running after you close the terminal you launched from, and each has
a readable log. `stop.bat` shuts both down. To run just the panel:

```bash
python -m engine.app          # http://127.0.0.1:8765
```

Type a topic, pick a hook, edit the beats, hit **Approve and render**. The log
down the left side reports measured facts as each stage finishes.

Without any OmniRoute provider configured, the panel says so in the header and
falls back to a built-in sample script. Everything after the script is still
real — voice, captions, render and inspection all run — so you can see the
whole pipeline work before spending anything. To get real scripts, follow
`docs/omniroute-setup.md`.

## Verify it

```bash
python scripts/verify_e2e.py      # full pipeline, fake brain, real everything else
python scripts/probe_omniroute.py # which gateway endpoints actually answer
python scripts/reset_cooldown.py  # what the dedup gate is currently blocking
python -m pytest tests/ -q        # 620 tests
```

`verify_e2e.py` exits non-zero unless it produced a playable 1080x1920 MP4 with
audio and no QC hard failures.

## What is verified, and what is not

Measured on this machine, 2026-09-17/18:

| Verified | Detail |
|---|---|
| End-to-end render | 50.000s render vs 50.010s narration, 0.010s diff (sub-frame — one frame at 30fps is 0.033s, so this is quantisation, not drift); 1080x1920, audio present, 62 MB, QC pass |
| A/V sync | 0.000s drift, proven by rendering labelled frames and reading back image + caption at the middle of every beat |
| Hindi TTS | Piper `pratham` at `length_scale=1.12` |
| Caption burn-in | libass, with karaoke word highlighting |
| Dedup gate | refused a repeat topic on the exact-hash layer |
| Web panel | full flow driven end to end, including an edit to the hook beat surviving approval. No console errors, no mobile overflow |
| Stock footage | 12/12 beats matched real Pexels footage on a real run, zero fallbacks; the matcher now downloads the smallest file that still covers 1080x1920 instead of the largest available (was up to 1.6 GB of 4K source for one video) |
| Colour grade cost | +80.1s on a 49.8s render (143.0s ungraded → 223.1s graded); `RAHASYA_VIDEO_GRAIN=0` buys back most of that (187.5s) because grain, not the colour work, is what defeats inter-frame compression |
| 620 tests | `pytest tests/ -q` |

| Not verified | Why |
|---|---|
| Real model output | OmniRoute has no provider configured — `docs/omniroute-setup.md` |
| Tier-1 image quality | needs a provider key; tier 2 is watermark-cropped and upscaled from a smaller source |
| Voice *tone* | both `hi-IN` voices are tagged "Friendly, Positive", which is wrong for dark mystery. The rate and pitch offsets pull them darker, but this needs your ears, not a test |
| Pexels licence terms | clips are tagged `licence='pexels'`, which records the source, not a verified commercial clearance — check Pexels' licence before monetising |
| Whether the grade reads as "one video" | it has been judged on comparison frames, not evaluated by eye over a full finished render with 20+ cuts |
| Publishing | no upload path exists. Payloads are built for you to copy |

## Layout

| Path | What it is |
|---|---|
| `engine/omniroute.py` | gateway client, cost telemetry, no-provider detection |
| `engine/contract.py` | the ReelPlan models every stage passes around |
| `engine/agents/` | the four prompts and their parsers |
| `engine/prompts/` | **the retention rules live here**, as constraints |
| `engine/gates/` | four-layer dedup, QC scorecard |
| `engine/media/` | Piper voice, stock clip matching (`clips.py`), image generation with fallback |
| `engine/assembly/` | ASS captions, ffmpeg graph, legacy-endpoint compiler |
| `engine/pipeline.py` | stage orchestration, split at each human gate |

### Browser-backed Gemini images

This project can optionally use the working browser-driven image generator from
the sibling `gemini-chat-bot` project. The adapter is enabled by
`RAHASYA_BROWSER_IMAGE_API` in `.env` and calls the local Node endpoint for
each scene. The Node service drives the already logged-in Edge UI through CDP,
downloads the real image response, and returns a local file URL. The existing
OmniRoute, keyless, and placeholder tiers remain as fallbacks.

Start the services in this order:

```powershell
# 1. Start Edge with the logged-in CDP profile
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
   --remote-debugging-port=9222 `
   --user-data-dir="C:\edge-playwright"

# 2. In gemini-chat-bot/
npm run server

# 3. In Instagram-/
python -m engine.app
```

When this tier is enabled, image generation is serialized because one Edge UI
session should not receive concurrent prompts. If the Node service, Edge CDP,
or Gemini UI tool is unavailable, the pipeline falls through to the existing
providers instead of failing the whole render.
| `engine/app.py` + `engine/ui/` | the local panel |
| `engine/fake_client.py` | stands in for the gateway; what the sample script comes from |
| `docs/` | spec, plan, setup, playbook, upstream repo fixes |

## Decisions worth knowing before you change anything

**Voice does not go through OmniRoute.** Its `/v1/audio/speech` documents only
`openai/tts-1`, which speaks Hindi with a foreign accent. Local Piper gives
real `hi-IN` neural voices, offline and MIT-licensed; `edge-tts` remains as
the fallback engine (`RAHASYA_VOICE_ENGINE=edge`).

**Visuals are stock footage, in a three-tier chain**, in
`engine/media/clips.py`: Pexels first, then the still-image chain in
`images.py` (itself gateway → keyless → placeholder), then a generated
placeholder. One clip per 2.5 seconds of narration, matched per beat so a
clip never straddles a beat boundary — that constraint is what keeps the A/V
sync work intact, because the clip layer only subdivides a span the beat
already owns. Clips hard-cut inside a beat and crossfade only where beats
meet, which is what fast-cut pacing actually looks like. A slot Pexels
cannot fill becomes a still with zoompan, keeping its slot duration: a
fallback changes what is on screen, never when. `PEXELS_API_KEY` in `.env`
is what switches tier 1 on. The matcher is sent `beat.visual_prompt` — the
English shot description the script agent writes for exactly this purpose —
never `voice_text`, the Devanagari narration: handing it the narration made
it search for what the story *means* instead of what is physically in
frame (a beat about skeletons in a frozen Himalayan lake, narrated with a
DNA claim about Greek ancestry, pulled "ancient greek temple columns").
Measured on a real 12-beat run: 12/12 beats matched real footage, zero
fallbacks.

**One colour grade ties twenty mismatched clips into one video.** A
50-second Reel pulls 20-26 clips from as many different Pexels creators,
each shot on different glass in different light, with a different camera's
colour science. `engine/assembly/render.py` applies one grade — cold,
desaturated, crushed blacks, a vignette pulling the eye to centre, and film
grain — over every frame alike: clip, fallback still and beat all get the
same treatment, because the grade sits downstream of the point where the
xfade chain could otherwise miss one. Beats tagged `hook`, `reveal` or
`twist` also get a slow push (a gentle zoom); every other beat holds still,
because motion everywhere stops signifying anything and the push has to be
the exception that means "look here." `RAHASYA_VIDEO_GRADE=0` turns the
whole look off; `RAHASYA_VIDEO_GRAIN` (default `9`) controls just the grain,
which is the expensive part — measured on a real 49.8s render, the full
grade costs 143.0s → 223.1s to encode (grain wrecks inter-frame
compression), and `RAHASYA_VIDEO_GRAIN=0` keeps the colour work and vignette
for 187.5s instead. The look was chosen from rendered comparison frames;
whether it reads as one video across a full finished render has not yet
been judged by eye.

**Every beat carries two texts.** `voice_text` in Devanagari drives
pronunciation; `caption_text` in Roman Hinglish is what gets burned on screen.
Roman is the convention in Indian Reels and carries no text-shaping risk.
`--captions=devanagari` switches the burn source, since libass can shape it.

**Beat durations come from measured audio, never from the model's guess.** And
the picture is built to land on the narration's timeline, not the other way
round. An xfade consumes time from both of its inputs, so an unpadded chain ran
0.5s short per join — on a ten-beat Reel that truncated the last 4.5s of voice
and desynced every caption after the first cut. `segment_lengths` pads each
segment by half an overlap per side and centres each transition on its cut, and
the `av_sync` QC check fails the render if the two timelines ever disagree by
more than 0.35s.

## The constraints that are not features

1. A human gate before anything is produced, and two optional ones after
   it. Approving a script is compulsory. Stopping to hear every beat of
   narration (`review_voice`) and stopping to look at every clip
   (`review_clips`) are each a tick-box, and each is a status in the store
   rather than a disabled button — a reload, a retry or a curl meets the
   same refusal. Voice comes before clips because a beat's clip count and
   slot lengths are derived from how long its audio measures.
2. Semantic dedup before production — cosine 0.88, 45-day entity cooldown.
   **Off by default**, because while the pipeline is being built the same
   topic gets run a dozen times and a gate that refuses every attempt is
   only in the way. `RAHASYA_DEDUP=1` turns it on, and it belongs on for a
   channel that is actually publishing: uploading two near-identical
   videos is the thing that gets one flagged. When it is off the DEDUP
   stage says so rather than reporting a pass — a disabled gate that
   looked clean would be a lie the panel repeats.
   `scripts/reset_cooldown.py` shows what is blocked and can clear it when
   the recorded entities are wrong.
3. No auto-publish. `engine/publish/payloads.py` imports no HTTP library, and a
   test asserts it stays that way.
4. Synthetic-media disclosure set on every payload. QC hard-fails without it.
5. Every factual claim carries a source URL, or the claim is cut.

Each traces to a documented policy risk. `docs/superpowers/specs/` has the
reasoning; the short version is that YouTube's inauthentic-content enforcement
moved to channel level in 2026, and these are what keep a high-output setup out
of that bucket.

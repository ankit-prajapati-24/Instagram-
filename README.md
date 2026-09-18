# Rahasya Engine

One Hinglish mystery topic in, one QC-passed 45-second vertical MP4 out, with a
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
      local edge-tts ──────────────┤  hi-IN voice, measured per beat
      image chain ─────────────────┤  gateway -> keyless -> placeholder
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
python -m pytest tests/ -q        # 175 tests
```

`verify_e2e.py` exits non-zero unless it produced a playable 1080x1920 MP4 with
audio and no QC hard failures.

## What is verified, and what is not

Measured on this machine, 2026-09-17/18:

| Verified | Detail |
|---|---|
| End-to-end render | 50.16s, 1080x1920, 6.80 MB, audio present, QC pass |
| A/V sync | 0.000s drift, proven by rendering labelled frames and reading back image + caption at the middle of every beat |
| Hindi TTS | `hi-IN-MadhurNeural` at `rate=-8% pitch=-6Hz` |
| Caption burn-in | libass, with karaoke word highlighting |
| Dedup gate | refused a repeat topic on the exact-hash layer |
| Web panel | full flow driven end to end, including an edit to the hook beat surviving approval. No console errors, no mobile overflow |
| Scene images | 9 of 10 beats from the keyless tier, watermark cropped, matching their captions |
| 175 tests | `pytest tests/ -q` |

| Not verified | Why |
|---|---|
| Real model output | OmniRoute has no provider configured — `docs/omniroute-setup.md` |
| Tier-1 image quality | needs a provider key; tier 2 is watermark-cropped and upscaled from a smaller source |
| Voice *tone* | both `hi-IN` voices are tagged "Friendly, Positive", which is wrong for dark mystery. The rate and pitch offsets pull them darker, but this needs your ears, not a test |
| edge-tts commercial terms | check Microsoft's terms before monetising the audio |
| Publishing | no upload path exists. Payloads are built for you to copy |

## Layout

| Path | What it is |
|---|---|
| `engine/omniroute.py` | gateway client, cost telemetry, no-provider detection |
| `engine/contract.py` | the ReelPlan models every stage passes around |
| `engine/agents/` | the four prompts and their parsers |
| `engine/prompts/` | **the retention rules live here**, as constraints |
| `engine/gates/` | four-layer dedup, QC scorecard |
| `engine/media/` | edge-tts voice, image generation with fallback |
| `engine/assembly/` | ASS captions, ffmpeg graph, legacy-endpoint compiler |
| `engine/pipeline.py` | stage orchestration either side of the human gate |
| `engine/app.py` + `engine/ui/` | the local panel |
| `engine/fake_client.py` | stands in for the gateway; what the sample script comes from |
| `docs/` | spec, plan, setup, playbook, upstream repo fixes |

## Three decisions worth knowing before you change anything

**Voice does not go through OmniRoute.** Its `/v1/audio/speech` documents only
`openai/tts-1`, which speaks Hindi with a foreign accent. Local `edge-tts`
gives real `hi-IN` neural voices, free and unmetered.

**Images walk a three-tier chain**, in `engine/media/images.py`: the gateway
first, then a keyless public endpoint, then a generated placeholder. With no
provider key the gateway lists zero image models, and this machine has no
usable GPU for local generation, so tier 2 is what actually draws scenes
today. It retries — one attempt per beat landed 3 of 10 images, retrying
landed 9 of 10 — crops the bottom 7% to remove the endpoint's watermark, and
scales to cover 1080x1920. Add a key and tier 1 takes over with no code
change. `RAHASYA_KEYLESS_IMAGES=0` skips tier 2.

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

1. One human gate, between script and render.
2. Semantic dedup before production — cosine 0.88, 45-day entity cooldown.
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

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
      OmniRoute images ────────────┤  scenes (placeholder if no provider)
                                   v
      ffmpeg v7.1 ─────────────────┤  zoompan · xfade · libass · loudnorm
                                   v
            outputs/*.mp4  +  metadata to copy out
                     (publishing stays manual, on purpose)
```

## Run it

```bash
pip install -r requirements.txt
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
python scripts/verify_e2e.py     # full pipeline, fake brain, real everything else
python scripts/probe_omniroute.py  # which gateway endpoints actually answer
python -m pytest tests/ -q       # 115 tests
```

`verify_e2e.py` exits non-zero unless it produced a playable 1080x1920 MP4 with
audio and no QC hard failures.

## What is verified, and what is not

Measured on this machine, 2026-09-17:

| Verified | Detail |
|---|---|
| End-to-end render | 45.67s, 1080x1920, 6.38 MB, audio present, QC pass |
| Hindi TTS | `hi-IN-MadhurNeural` at `rate=-8% pitch=-6Hz` |
| Caption burn-in | libass, with karaoke word highlighting |
| Dedup gate | refused a repeat topic on the exact-hash layer |
| Web panel | full flow driven end to end, no console errors, no mobile overflow |
| 115 tests | `pytest tests/ -q` |

| Not verified | Why |
|---|---|
| Real model output | OmniRoute has no provider configured — `docs/omniroute-setup.md` |
| Generated scene images | same; placeholder frames stand in |
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

**Every beat carries two texts.** `voice_text` in Devanagari drives
pronunciation; `caption_text` in Roman Hinglish is what gets burned on screen.
Roman is the convention in Indian Reels and carries no text-shaping risk.
`--captions=devanagari` switches the burn source, since libass can shape it.

**Beat durations come from measured audio, never from the model's guess.** And
QC checks the *rendered* length, not the sum of beats — xfade transitions
overlap, so nine joins remove about 4.5s from a ten-beat Reel.

## The constraints that are not features

1. One human gate, between script and render.
2. Semantic dedup before production — cosine 0.88, 45-day entity cooldown.
3. No auto-publish. `engine/publish/payloads.py` imports no HTTP library, and a
   test asserts it stays that way.
4. Synthetic-media disclosure set on every payload. QC hard-fails without it.
5. Every factual claim carries a source URL, or the claim is cut.

Each traces to a documented policy risk. `docs/superpowers/specs/` has the
reasoning; the short version is that YouTube's inauthentic-content enforcement
moved to channel level in 2026, and these are what keep a high-output setup out
of that bucket.

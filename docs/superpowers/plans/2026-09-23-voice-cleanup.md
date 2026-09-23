# Cleaning up narration a human recorded

**Status:** design approved in chat 2026-09-23. Bounded work; this file
exists because subagent-driven-development needs tasks to dispatch, not
because the change is architectural.

## Why

The voice gate lets a user upload their own narration for any beat. A
phone or a laptop mic brings room tone, fan hum and mouth clicks with it,
and a person reading a script leaves gaps that read as dead air rather
than as tension.

`ingest_narration` already runs every upload through two ffmpeg passes —
one to measure loudness, one to re-encode into Piper's format at Piper's
level. Cleanup belongs inside those two passes. It is not a new stage, a
new dependency or a new service: `highpass`, `afftdn`, `adeclick` and
`silenceremove` are all in the bundled imageio-ffmpeg build, verified on
2026-09-23.

## Global Constraints

These bind every task. A reviewer checks against them.

1. **Two ffmpeg passes, not three.** Cleanup filters are prepended to the
   filter chain of the existing measure pass and the existing write pass.
   Adding a third invocation fails review.
2. **Cleanup runs before loudness is measured.** Measuring a noisy signal
   and normalising to that measurement is the bug this ordering exists to
   prevent. The same filter chain must appear in both passes so the
   measured values describe the signal that is written.
3. **The raw upload is never destroyed.** It is written beside the cleaned
   file and both stay on disk for the life of the plan.
4. **Nothing changes silently.** `ingest_narration` returns what it did —
   seconds before and after, loudness before and after — and that reaches
   the review board. A transform that leaves no record fails review. This
   is the same rule that `voice_engine`, `Clip.provider` and
   `word_timing_source` already follow.
5. **Every number is a Setting with an env key.** No tuning constant is
   written into a route, a filter string built inline, or the panel.
6. **The output format is unchanged.** 24000 Hz mono mp3 at the loudness
   `UPLOAD_LOUDNESS` names, because `stitch_narration` uses the concat
   demuxer and every beat has to agree.
7. **Tests run ffmpeg.** A test that asserts on a filter string proves
   nothing about audio. Each audio claim is measured off a real file.
   See `tests/test_voice_upload.py` for the existing shape.
8. **Cleanup off behaves exactly as today.** With the switch off the
   written file must be what the current code writes.

## Task 1 — the cleanup chain

Add to `engine/media/voice.py`:

```python
CleanupReport   # dataclass: seconds_before, seconds_after,
                # loudness_before, loudness_after, filters_applied
def cleanup_filters(settings) -> str
```

`cleanup_filters` returns the ffmpeg filter string, or `""` when cleanup
is off. Order and defaults:

```
highpass=f={settings.voice_clean_highpass}     default 80
afftdn=nf={settings.voice_clean_denoise}       default -25
adeclick
silenceremove=start_periods=1
              :start_silence={pause_cap}
              :start_threshold={silence_threshold}
              :stop_periods=-1
              :stop_silence={pause_cap}
              :stop_threshold={silence_threshold}
              :detection=peak
```

New `Settings` fields, each `os.getenv`-backed exactly as the existing
voice fields are:

| field | env key | default |
|---|---|---|
| `voice_clean` | `RAHASYA_VOICE_CLEAN` | `True` |
| `voice_clean_highpass` | `RAHASYA_VOICE_HIGHPASS` | `80.0` |
| `voice_clean_denoise` | `RAHASYA_VOICE_DENOISE` | `-25.0` |
| `voice_pause_cap` | `RAHASYA_VOICE_PAUSE_CAP` | `0.35` |
| `voice_silence_threshold` | `RAHASYA_VOICE_SILENCE_DB` | `-45.0` |

`voice_pause_cap` is the one number that matters to a user: no silence
longer than it survives, and natural inter-word gaps (0.1-0.3s) fall
below it untouched, so a deliberate pause is shortened rather than
removed.

`silenceremove`'s exact parameter behaviour must be **verified against
ffmpeg**, not assumed, before the filter string is fixed. If `detection=peak`
or the `start_*`/`stop_*` split does not do what this task claims, the
implementer changes the string to whatever measurably caps internal
silences and trims the head and tail, and records the correction in its
report.

**Tests** (`tests/test_voice_cleanup.py`, real ffmpeg throughout):

- a 3s tone with white noise mixed in: after the chain the noise floor,
  measured over a segment, is lower than before by a stated margin
- a file built as `tone / 2s silence / tone`: after the chain the total
  duration drops by roughly the excess (2.0 - pause_cap) and
  `silencedetect` reports no remaining gap longer than
  `pause_cap + 0.1`
- a file whose gaps are all 0.2s: duration is within 0.15s of the
  original, i.e. natural speech gaps are not squeezed
- `voice_clean = False` returns `""` and the chain is absent
- each Setting reaches the filter string (change the setting, see the
  number move)

## Task 2 — fold it into the ingest, keep the raw

Change `ingest_narration` in `engine/media/voice.py`:

- signature gains `raw_target: Path | None = None`; when given, the
  source bytes are copied there before anything is written, and that copy
  is what a later revert re-ingests
- both ffmpeg passes get `cleanup_filters(settings)` prepended to their
  filter chain, comma-joined ahead of `loudnorm`
- the return value becomes `(seconds, CleanupReport)` — every caller
  updates. `seconds` keeps its current meaning: the length of the file
  actually written
- `loudness_before` is the `input_i` the measure pass already reports;
  `loudness_after` is measured off the written file. `seconds_before` is
  the raw source's duration, `seconds_after` the written file's
- a `clean: bool | None = None` parameter overrides
  `settings.voice_clean` for one call, which is what the revert route
  uses

The existing refusals keep their order and their meaning: too short,
silent, does not decode. **They are judged on the raw upload, before
cleanup** — a recording that is nothing but noise must not pass because
the denoiser made it quiet enough to look like silence, and must not be
refused as silent because cleanup removed its only content.

**Tests** — extend `tests/test_voice_upload.py`:

- the raw file lands at `raw_target` and is byte-identical to the source
- with cleanup on, a noisy upload's written file is measurably cleaner
  than the raw, and the report's numbers match what the files measure
- with cleanup off, the written file matches today's behaviour
- a silent upload is still refused, with cleanup on
- the report's `seconds_before`/`seconds_after` bracket a real trim

## Task 3 — the revert route and what the board says

`engine/app.py`:

- the upload route passes a `raw_target` beside the cleaned file
  (`<beat>-upload.raw<suffix>`, suffix from the sniffed container) and
  returns the `CleanupReport` fields in its response
- new `POST /api/plan/{plan_id}/voice/{beat_id}/cleanup` taking
  `{"enabled": bool}`: re-ingests the stored raw with cleanup on or off.
  404 when no raw is stored for that beat, 409 off the voice gate, same
  containment checks as the upload route. Idempotent — calling it twice
  with the same value is not an error
- `GET /api/audio/{plan_id}/{beat_id}` gains `?raw=1`, serving the stored
  raw under the same containment rule
- the voice board rows gain `raw_audio`, `cleaned`, and the report
  numbers, so a row can show both takes

**Tests** — extend `tests/test_voice_review.py`:

- upload, then revert to raw: the beat's audio measures like the raw
  (longer, if pauses were trimmed) and `cleaned` is false
- revert to raw and back to cleaned: the beat returns to the cleaned
  measurement, proving the raw survived the round trip
- `?raw=1` serves the raw and is 404 for a beat with no raw stored
- the route is 409 off the gate and 404 for an unknown beat
- a raw path outside `work_dir` is never served

## Task 4 — the panel and the docs

`engine/ui/index.html`: each beat row gains a second player for the raw
take and a control that switches the beat between cleaned and raw,
calling the Task 3 route. The row states what cleanup did — seconds
removed and the loudness move — from the board's own numbers, nothing
hardcoded.

`SETUP.md`: extend the voice-gate section with what cleanup does, the
five env keys and their defaults, and the one sentence that matters —
`RAHASYA_VOICE_PAUSE_CAP` is the number to change if pauses feel clipped.

The panel test in `tests/test_voice_review.py` already asserts that every
`$("id")` the page reaches for exists; the new ids are covered by it
automatically. Add the cleanup route to the route-coverage test beside
the others.

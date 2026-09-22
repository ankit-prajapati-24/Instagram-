"""The four-agent chain.

Each agent is one OmniRoute chat call with a strict JSON contract. They live in
one module because they change together: they share the prompt-loading
convention, the parse-and-validate path, and the ReelPlan models they emit.

The prompts in ``engine/prompts/`` carry the retention and policy rules as
enforced constraints rather than advice, and the QC scorecard re-checks the
ones that can be checked mechanically.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

from pydantic import ValidationError

from engine.contract import (Hook, Metadata, Provenance, Script, Topic)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class AgentError(RuntimeError):
    """An agent's output did not match its contract."""

    def __init__(self, stage: str, detail: str, raw=None):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.raw = raw


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise AgentError(name, f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8")


def _claims_block(provenance: Provenance) -> str:
    if not provenance.claims:
        return "(no verified claims available — assert nothing factual)"
    return "\n".join(
        f"- [{c.confidence}] {c.text}  (source: {c.source_url})"
        for c in provenance.claims)


def _ask(client, stage: str, prompt: str, *, model: str | None = None,
         temperature: float = 0.85, parse=None):
    """One agent call, with a single repair attempt on a shape mismatch.

    Models miss the schema in small, mechanical ways — a claim id as the
    integer 1 instead of "b1", a number as "4.5s". Failing the stage outright
    throws away every call made so far in the run, so the exact validation
    error is handed back once and the model is asked to correct it. A second
    failure is real and raises.

    ``parse`` takes the decoded JSON and returns the model object, raising
    ValidationError if the shape is wrong.

    This path is for SHAPE errors only, and the message below says so. It
    used to also carry semantic failures — the script word budget raised a
    ``Repairable`` that landed here — and that is precisely why the repair
    never worked on them: a model that had written 122 words was told "that
    did not match the required schema" and reminded to keep its ids quoted,
    so it returned the same content at 123. Length is now fixed by
    ``_fit_to_budget`` below, in its own call, and nothing semantic reaches
    this message any more.
    """
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(2):
        started = time.monotonic()
        print(f"[AGENT] {stage} attempt={attempt + 1}/2 model={model or 'default'} started", flush=True)
        result = client.chat(messages, model=model, want_json=True,
                             temperature=temperature)
        elapsed = time.monotonic() - started
        print(f"[AGENT] {stage} attempt={attempt + 1}/2 completed seconds={elapsed:.1f} json={result.data is not None}", flush=True)
        if result.data is None:
            problem = "model returned no JSON"
        else:
            if parse is None:
                return result, None
            try:
                return result, parse(result.data)
            except ValidationError as exc:
                problem = _explain(exc)

        if attempt == 0:
            print(f"[AGENT] {stage} schema validation failed; starting repair attempt", flush=True)
            messages = messages + [
                {"role": "assistant",
                 "content": json.dumps(result.data)[:4000]
                 if result.data is not None else (result.text or "")[:4000]},
                {"role": "user",
                 "content": ("That did not match the required schema:\n"
                             f"{problem}\n\n"
                             "Return the SAME content again, corrected. JSON "
                             "only, no commentary. Keep every id a quoted "
                             "string and every duration a plain number.")},
            ]
            continue

        raise AgentError(stage, problem,
                         result.data if result.data is not None else result.text)


def _explain(exc: ValidationError) -> str:
    """The parts of a pydantic error a model can act on."""
    lines = []
    for error in exc.errors()[:6]:
        where = ".".join(str(p) for p in error["loc"])
        lines.append(f"- {where}: {error['msg']} "
                     f"(got {error.get('input')!r})")
    return "\n".join(lines)


# --- voice_text must be pure Devanagari -------------------------------------
# script.txt already says it: "No English words in Latin script -- translit-
# erate them (DNA -> डीएनए, report -> रिपोर्ट)." A real run broke that rule
# inconsistently within one script -- "fog" and "magnetic anomaly" left in
# Latin in beats 1 and 8, the identical words correctly transliterated to
# Devanagari in beat 7 of the same script. Piper does not skip the Latin
# text or fail on it; it speaks it, badly: synthesising both forms produced
# durations within 2% of each other (3.62s vs 3.55s), and a person compared
# the audio and confirmed the transliterated version is the correct one.
# Phonemizing both forms directly (see this fix's report) shows the same
# thing at the phoneme level -- "confirm" and "कन्फर्म" come out as two
# different sequences under this voice's espeak-ng frontend.
#
# The rule existed only as prose, and nothing enforced it -- a deterministic,
# cheaply checkable property left entirely to the model's goodwill. This is
# that enforcement.
#
# What counts as a violation, decided against the evidence above rather than
# assumed:
#   * Any Latin letter (A-Za-z) anywhere in voice_text, including a single
#     stray letter inside an otherwise-Devanagari word. Piper does not
#     partially mispronounce a word -- there is no quantity of Latin script
#     that is safe to let through, so the check does not special-case whole
#     "words" vs. fragments.
#   * ASCII digits are NOT a violation. script.txt asks for them ("write
#     numbers as digits"), and phonemizing "1965" against the Devanagari-
#     digit spelling "१९६५" under this voice's frontend produced identical
#     phoneme sequences -- the digit's script does not change how it is
#     read. Digits are 0-9, never A-Za-z, so the regex below already leaves
#     them alone without a special case.
#   * Punctuation, the en dash, and the Devanagari abbreviation sign (॰,
#     U+0970 -- beat 4 of the reported run used "ई॰पी॰ गी") are not a
#     violation either. None of them are Latin letters, so, like digits,
#     they simply never match [A-Za-z] and need no special case.
#   * caption_text is NEVER checked here. It is Roman Hinglish on purpose --
#     script.txt: "Keep well-known English words in Latin (DNA, report,
#     carbon dating)" -- and running this check on it would fail every
#     clean script.
LATIN_LETTERS_RE = re.compile(r"[A-Za-z]+")


def _latin_words(text) -> list[str]:
    """Latin-script runs inside ``text``, in order, duplicates kept."""
    return LATIN_LETTERS_RE.findall(str(text or ""))


def _latin_violations(script: Script) -> list[tuple[str, list[str]]]:
    """``(beat_id, latin_words)`` for every beat whose voice_text still has
    Latin script. Only voice_text is inspected -- see the module note above
    for why caption_text never is."""
    out = []
    for beat in script.beats:
        words = _latin_words(beat.voice_text)
        if words:
            out.append((beat.beat_id, words))
    return out


def _latin_repair_message(script: Script, violations) -> str:
    """Tell the model plainly which beats and which words, in the same
    vocabulary script.txt already uses for the rule.

    Deliberately not routed through ``_ask``'s repair path: that message is
    written for a pydantic ``ValidationError`` -- "That did not match the
    required schema" -- and a model told its JSON *types* were wrong when
    the actual problem is spelling just returns the same content again. This
    is the same lesson the word-budget fix already learned; see
    ``_fit_to_budget`` below.
    """
    by_id = {b.beat_id: b for b in script.beats}
    rows = "\n".join(
        f'{beat_id}: "{by_id[beat_id].voice_text}"\n'
        "  Latin-script words that must be transliterated: "
        + ", ".join(sorted(set(words)))
        for beat_id, words in violations)
    return (
        "voice_text is fed directly to the Hindi text-to-speech engine, and "
        "spelling drives its pronunciation. The lines below still have "
        "English words written in Latin script, and the voice speaks them "
        "wrong because of it. This is not a JSON problem; the shape is "
        "fine.\n\n"
        f"{rows}\n\n"
        "Rewrite ONLY these beats' voice_text with every Latin-script word "
        "transliterated into Devanagari (English -> इंग्लिश, confirm -> "
        "कन्फर्म, DNA -> डीएनए). Keep the meaning and every other word "
        "unchanged. Do not touch caption_text -- it stays Roman Hinglish on "
        "purpose. ASCII digits are fine exactly as they are.\n\n"
        "Return ONLY this JSON, one entry per beat listed above:\n"
        '{"lines": [{"beat_id": "...", "voice_text": "<Devanagari only>"}]}')


def _fix_latin_script(client, script: Script, *, model,
                      words_per_second: float, stage: str = "script"):
    """One repair round for Latin script surviving in voice_text.

    Checked here, after the script already parsed, for the same reason the
    word budget is: a semantic miss is not a shape mismatch, and routing it
    through the generic repair told the model the wrong thing entirely (see
    ``_latin_repair_message``). Exactly one repair attempt is made, matching
    the one-retry convention ``_ask`` itself uses for schema mismatches --
    a script still carrying Latin script after being told precisely which
    beats and which words is a real failure, not something to paper over
    with another round.
    """
    violations = _latin_violations(script)
    if not violations:
        return None

    print(f"[AGENT] {stage} voice_text has Latin script in "
          f"{len(violations)} beat(s); starting repair attempt", flush=True)
    message = _latin_repair_message(script, violations)
    result = client.chat([{"role": "user", "content": message}],
                         model=model, want_json=True, temperature=0.3)

    rows = result.data.get("lines") if isinstance(result.data, dict) else None
    by_id = {b.beat_id: b for b in script.beats}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            beat = by_id.get(str(row.get("beat_id") or ""))
            voice = str(row.get("voice_text") or "").strip()
            if beat is not None and voice:
                beat.voice_text = voice
                _sync_seconds(beat, words_per_second)

    remaining = _latin_violations(script)
    if remaining:
        detail = "; ".join(
            f"{beat_id}: {', '.join(sorted(set(words)))}"
            for beat_id, words in remaining)
        raise AgentError(
            stage,
            "voice_text still has Latin script after one repair attempt "
            f"({detail}) -- these words were never transliterated to "
            "Devanagari", result.data)
    return result.cost


# --- the trim pass ---------------------------------------------------------
# Writing exactly N words is a planning problem, and five real-model runs of
# the same task (122, 123, 136, 152, 160 words against budgets of 103-114)
# say this model cannot do it however the target is phrased. Shortening
# existing text to N words is an editing problem, and it is good at that.
# So length is not asked of the script call at all any more: the script call
# writes the story, and a second, much narrower call fits it to the budget.

# The band either side of the budget that needs no edit at all. Same +/-15%
# the stage has always used, named here because three places read it.
WORD_TOLERANCE = 0.15
# The twist beat is the pattern interrupt, and the prompt requires it to
# open on this word. An edit that loses it is not an edit, it is damage.
TWIST_PREFIX = "लेकिन"          # "lekin"
# Deliberate sentence-length variance is the point of the per-beat range,
# and a flat rhythm is the clearest AI tell there is. A trim that flattens
# the script is thrown away. The floor is the 4.0 population stdev
# tests/test_images.py already holds the worked example to, except when the
# script arrives flatter than that — then the rule is only that the trim
# must not make it appreciably worse, because a trim cannot be asked to
# create variance the script never had.
VARIANCE_FLOOR = 4.0
VARIANCE_KEEP = 0.7
# The answer is ten lines in two scripts, and Devanagari is expensive to
# tokenise -- but the real reason this is not the client's 4096 default is
# thinking tokens. Measured against the gateway on a nine-line trim: prompt
# 3,478 + completion 737 but total 6,466, so ~2,250 invisible reasoning
# tokens were charged against the output budget, and a run whose reasoning
# went longer came back as a JSON object cut off mid-string with
# finish_reason "length". extract_json then finds nothing at all and the
# whole round is wasted. Two of the first six real runs died this way at
# 4096 and 8192. Setting RAHASYA_MODEL_TRIM to one of the gateway's
# "no-think/" aliases removes the reasoning tokens instead; this ceiling,
# and the escalation in _fit_to_budget, are what keep the stage alive
# without it.
TRIM_MAX_TOKENS = 16384


def _words(text) -> int:
    return len(str(text or "").split())


def script_words(script: Script) -> int:
    """Spoken words in the whole script. Runtime follows this number."""
    return sum(_words(b.voice_text) for b in script.beats)


def _drift(words: int, target: int) -> float:
    return (words - target) / target if target else 0.0


def _editable(script: Script) -> list:
    """The beats the trim pass may touch.

    Beat 1 is the chosen hook reused verbatim — it was picked by its own
    agent and the whole plan refers to it — so it is not merely
    "asked not to change", it is never sent. Anything else with the hook
    role is treated the same way.
    """
    return [beat for index, beat in enumerate(script.beats)
            if index and beat.role != "hook"]


def _lines_block(beats) -> str:
    return "\n\n".join(
        f"{b.beat_id}  ({_words(b.voice_text)} words)\n"
        f"  voice_text   : {b.voice_text}\n"
        f"  caption_text : {b.caption_text}"
        for b in beats)


def _sync_seconds(beat, words_per_second: float) -> None:
    """target_seconds is derived from the words, never supplied, so it has
    to follow them through an edit (see the note in ``run_script``)."""
    beat.target_seconds = round(
        max(_words(beat.voice_text), 1) / words_per_second, 2)


def _vet_row(beat, voice: str, caption: str) -> str | None:
    """Why this replacement must not be applied, or None if it is fine.

    Each rule is one of the protected properties, checked against what came
    back rather than trusted to the prompt. A refused row simply keeps its
    original text: the round then removes fewer words than asked, the loop
    notices and goes again, and the cap turns a persistent refusal into a
    legible failure instead of a damaged script.
    """
    if not voice.strip() or not caption.strip():
        return f"{beat.beat_id}: an empty line came back"
    if beat.role == "twist" and not voice.lstrip().startswith(TWIST_PREFIX):
        return (f"{beat.beat_id}: the twist no longer begins with "
                f"\"{TWIST_PREFIX}\"")

    was, now = _words(beat.voice_text), _words(voice)
    if now < was:
        if caption.strip() == beat.caption_text.strip():
            return (f"{beat.beat_id}: voice_text lost {was - now} words and "
                    f"caption_text was returned unchanged")
        if _words(caption) > _words(beat.caption_text):
            return (f"{beat.beat_id}: voice_text got shorter and "
                    f"caption_text got longer")
    return None


def _variance(lengths) -> float:
    return statistics.pstdev(lengths) if len(lengths) >= 2 else 0.0


def _trim_round(client, script: Script, word_target: int, *, model,
                words_per_second: float, note: str) -> tuple:
    """One narrow editing call.

    Returns ``(cost, note_for_the_next_round, usable)``. ``usable`` is False
    only when the answer could not be read at all -- not when it was read
    and rejected -- because that is the one failure a different model can
    fix. Applies in place, and applies nothing if the round flattens the
    rhythm.
    """
    beats = _editable(script)
    if not beats:
        raise AgentError("script", "nothing to trim: every beat is the "
                                   "locked hook")

    locked = script_words(script) - sum(_words(b.voice_text) for b in beats)
    current = sum(_words(b.voice_text) for b in beats)
    # The hook's words are spent, so the editable lines get what is left.
    from engine.config import MIN_BEAT_WORDS

    target = max(word_target - locked, len(beats) * MIN_BEAT_WORDS)
    shorten = current > target

    extra = ""
    twist = next((b for b in beats if b.role == "twist"), None)
    if twist is not None:
        extra = (f"5. Line {twist.beat_id} must still begin with the word "
                 f"\"{TWIST_PREFIX}\". It is the turn the whole script "
                 f"pivots on. Keep it as the first word.\n")

    prompt = load_prompt("trim").format(
        line_count=len(beats),
        current_words=current,
        target_words=target,
        delta_words=abs(current - target),
        direction="shorten" if shorten else "lengthen",
        direction_caps="SHORTEN" if shorten else "LENGTHEN",
        how=(_HOW_SHORTEN if shorten else _HOW_LENGTHEN),
        extra_rules=extra,
        lines=_lines_block(beats),
        note=f"\n{note}\n" if note else "")

    print(f"[AGENT] trim {current} -> {target} words over {len(beats)} "
          f"lines model={model or 'default'} started", flush=True)

    # A round that comes back unusable is a wasted round, not a dead stage.
    # The strong model has already written the script; throwing that away
    # because the cheap editor answered in prose would be the expensive
    # mistake. The loop simply goes again, and the cap ends it cleanly.
    from engine.omniroute import CostRecord, OmniRouteError

    try:
        result = client.chat([{"role": "user", "content": prompt}],
                             model=model, want_json=True, temperature=0.3,
                             max_tokens=TRIM_MAX_TOKENS)
    except OmniRouteError as exc:
        print(f"[AGENT] trim round unusable: {exc}", flush=True)
        return CostRecord(), (
            "Your last answer was not valid JSON and was thrown away "
            f"({str(exc)[:160]}). Answer with the JSON object and nothing "
            "else: no prose, no explanation, no markdown fence."), False

    rows = result.data.get("lines") if isinstance(result.data, dict) \
        else result.data
    if not isinstance(rows, list) or not rows:
        print("[AGENT] trim round had no 'lines' array", flush=True)
        return result.cost, (
            'Your last answer had no "lines" array and was thrown away. '
            "Answer with exactly the JSON object shown below, one entry per "
            "line, and nothing else."), False

    by_id = {b.beat_id: b for b in beats}
    proposed: dict = {}
    refused: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        beat = by_id.get(str(row.get("beat_id") or ""))
        if beat is None:
            # Either a hallucinated id or the locked hook, which was never
            # sent. Both are simply not applied.
            continue
        voice = str(row.get("voice_text") or "")
        caption = str(row.get("caption_text") or "")
        complaint = _vet_row(beat, voice, caption)
        if complaint:
            refused.append(complaint)
            continue
        proposed[beat.beat_id] = (voice.strip(), caption.strip())

    # Variance is judged on the whole round, before anything is written: one
    # line matching its neighbour is fine, every line matching is the AI tell
    # the prompt exists to avoid.
    before = [_words(b.voice_text) for b in script.beats]
    after = [_words(proposed[b.beat_id][0]) if b.beat_id in proposed
             else _words(b.voice_text) for b in script.beats]
    floor = min(VARIANCE_FLOOR, VARIANCE_KEEP * _variance(before))
    if proposed and _variance(after) < floor:
        print(f"[AGENT] trim round discarded: sentence-length variance "
              f"{_variance(after):.1f} < {floor:.1f}", flush=True)
        return result.cost, (
            f"Your last attempt was thrown away: it made the lines too "
            f"even (length spread {_variance(after):.1f}, needs "
            f"{floor:.1f}). Cut the LONG lines hard and leave the short "
            f"ones short."), True

    for beat_id, (voice, caption) in proposed.items():
        beat = by_id[beat_id]
        beat.voice_text = voice
        beat.caption_text = caption
        _sync_seconds(beat, words_per_second)

    if refused:
        print(f"[AGENT] trim kept {len(refused)} original line(s): "
              + "; ".join(refused), flush=True)
        return result.cost, (
            "These lines were rejected and kept their original, longer "
            "text — fix them this time:\n- " + "\n- ".join(refused)), True
    return result.cost, "", True


_HOW_SHORTEN = """\
Cut filler, adjectives, and framing that restates what an earlier line
already said. Delete whole clauses rather than shaving a word off every
line. Never cut a fact, a name, a number or a date. Take most of the words
off the longest lines."""

_HOW_LENGTHEN = """\
Add concrete detail to the sparsest lines — a place, a time, a texture that
the line already implies. Never assert a new fact, a new name, a new number
or a new date. Leave the short punch lines short; put the words where the
line is thin."""


def _fit_to_budget(client, script: Script, word_target: int, *, model,
                   words_per_second: float, max_rounds: int,
                   fallback_model=None, stage: str = "script") -> list:
    """Edit the script until it is inside the budget, or fail loudly.

    An in-range script costs nothing: the loop never enters. Otherwise it
    trims, re-measures, and trims again up to ``max_rounds``, because one
    pass reliably moves the count but does not always land it. Past the cap
    it raises with the whole sequence of counts, so the failure says what
    actually happened rather than "script too long".

    ``fallback_model`` is the escalation. The editor is the cheap model
    because editing is cheap work, but "cheap" on this gateway also means
    a model that sometimes answers a strict-JSON request with its own
    reasoning — measured at roughly one round in three, which failed the
    whole stage twice in six real runs. A round that could not be read at
    all is therefore the last one the cheap model gets; the strong model,
    which has already been paid for once this stage, finishes the job.
    Rounds that were read and rejected do not escalate: those are content
    problems, and a bigger model is not the answer to them.
    """
    history = [script_words(script)]
    costs: list = []
    note = ""
    rounds = 0
    editor = model

    while (abs(_drift(history[-1], word_target)) > WORD_TOLERANCE
           and rounds < max_rounds):
        rounds += 1
        cost, note, usable = _trim_round(
            client, script, word_target, model=editor,
            words_per_second=words_per_second, note=note)
        costs.append(cost)
        if not usable and fallback_model and editor != fallback_model:
            print(f"[AGENT] trim escalating from {editor or 'default'} to "
                  f"{fallback_model}: the answer could not be read",
                  flush=True)
            editor = fallback_model
        history.append(script_words(script))
        print(f"[AGENT] trim round {rounds}/{max_rounds}: "
              f"{history[-2]} -> {history[-1]} words "
              f"(target {word_target})", flush=True)

    words = history[-1]
    drift = _drift(words, word_target)
    if abs(drift) > WORD_TOLERANCE:
        raise AgentError(
            stage,
            f"the script is still {'too long' if drift > 0 else 'too short'} "
            f"after {rounds} trim round{'s' if rounds != 1 else ''}: "
            f"{words} spoken words against a target of {word_target} "
            f"({drift * 100:+.0f}%). Word counts by round: "
            + " -> ".join(str(n) for n in history)
            + (f". Last round: {note.splitlines()[0]}" if note else ""))
    return costs


def _merge_costs(first, extra: list):
    """One CostRecord for the stage, however many calls it took.

    ``plan_stage`` records a single cost per stage, and the trim calls are
    part of the script stage's price — dropping them would under-report the
    run against the daily ceiling.
    """
    if not extra:
        return first
    from engine.omniroute import CostRecord

    return CostRecord(
        usd=first.usd + sum(c.usd for c in extra),
        provider=first.provider,
        model=first.model,
        fallback_attempts=first.fallback_attempts
        + sum(c.fallback_attempts for c in extra),
        latency_ms=first.latency_ms + sum(c.latency_ms for c in extra),
        cache_hit=first.cache_hit)


def run_research(client, topic: Topic, *, model: str | None = None):
    prompt = load_prompt("research").format(topic=topic.raw)
    result, provenance = _ask(client, "research", prompt, model=model,
                              temperature=0.4,
                              parse=Provenance.model_validate)
    return provenance, result.cost


def run_hooks(client, topic: Topic, provenance: Provenance, *,
              model: str | None = None):
    prompt = load_prompt("hooks").format(
        topic=topic.raw, claims=_claims_block(provenance))
    def parse(data):
        raw = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            raise AgentError("hooks", "expected a non-empty 'hooks' list",
                             data)
        return [Hook.model_validate(h) for h in raw]

    result, hooks = _ask(client, "hooks", prompt, model=model,
                         temperature=1.0, parse=parse)
    return hooks, result.cost


def run_script(client, topic: Topic, provenance: Provenance,
               hook: Hook | None, *, model: str | None = None,
               word_target: int | None = None, beats: int | None = None,
               words_per_second: float | None = None,
               trim_model: str | None = None, trim_rounds: int | None = None):
    """``word_target`` is derived from the voice engine's measured rate.

    Left unset it resolves to ``target_seconds * words_per_second`` from the
    configuration, which is the same product ``plan_stage`` passes. It is
    resolved here rather than written into the signature because a literal
    default goes stale: it said 136 for a while after that product became
    103, and re-typing the new product as a literal would only move the same
    bug one rate change further out. Nothing to hand-copy, nothing to rot.

    ``beats`` is resolved the same way and for the same reason. It said 12
    for a while after the word budget above became 103, which asked the
    model for 8.6 words/beat -- it wrote 161 words instead and failed even
    after the repair retry. ``beats`` and ``word_target`` are recalibrated
    together from here on, because it is their ratio, not either number
    alone, that the model can or cannot write.

    ``words_per_second`` is the voice's measured rate, and it is in the
    signature for the same reason the other two are: the prompt now states
    it to the model. Every seconds figure in script.txt is a word count
    converted at this rate, and the per-beat word range is derived from
    ``word_target / beats`` — the three numbers the model can read cannot
    disagree with each other because there is only one of each.

    Duration follows from word count, so the prompt is told the budget
    rather than a beat range it can satisfy at any length. It will still
    miss it — that is measured, not assumed — and ``_fit_to_budget`` below
    edits the result down afterwards. Everything above is what makes the
    overshoot small enough for an edit to close; none of it makes the model
    able to hit a number.

    ``trim_model`` is the model that does that editing, and it defaults to
    ``Settings.model_cheap``: shortening ten lines that already exist is not
    work the strong model is needed for. ``trim_rounds`` caps how many times
    it may try.
    """
    if word_target is None:
        # Imported here, not at module scope: engine.config constructs
        # Settings at import time and reads .env, and the agents module is
        # imported by tools that have no business doing either.
        from engine.config import word_budget

        word_target = word_budget()
    if beats is None:
        from engine.config import beat_count

        beats = beat_count()
    if words_per_second is None:
        from engine.config import speech_rate

        words_per_second = speech_rate()
    if trim_model is None:
        from engine.config import trim_model as _trim_model

        trim_model = _trim_model() or None
    if trim_rounds is None:
        from engine.config import trim_round_cap

        trim_rounds = trim_round_cap()

    # Every number below is derived here, from those three. The bug this
    # replaces was three literals in script.txt — "4 to 18", "4.4", "44.0" —
    # that stayed still while the budget moved.
    from engine.config import beat_word_range, words_per_beat as _per_beat

    per_beat = _per_beat(word_target, beats)
    beat_words_min, beat_words_max = beat_word_range(per_beat)
    prompt = load_prompt("script").format(
        word_target=word_target,
        beats=beats,
        words_per_beat=per_beat,
        beat_words_min=beat_words_min,
        beat_words_max=beat_words_max,
        words_per_second=f"{words_per_second:g}",
        seconds_per_beat=round(per_beat / words_per_second, 1),
        total_seconds=round(word_target / words_per_second, 1),
        topic=topic.raw,
        hook=(f"{hook.voice_text}  /  {hook.caption_text}" if hook
              else "(no hook chosen — write your own opening beat)"),
        hook_id=hook.variant_id if hook else "h1",
        claims=_claims_block(provenance))
    def parse(data):
        raw = data.get("script") if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            raise AgentError("script", "expected a 'script' object", data)

        # target_seconds is no longer asked of the model. It is a hint that
        # measured_seconds overrides everywhere downstream, and asking for
        # it invited exactly the seconds-first sizing that overshot the
        # budget: the model filled in 4.4s per beat, converted it at a
        # conversational rate, and wrote 17 words. Derived from the words
        # it actually wrote, at this voice's rate, it cannot disagree with
        # them. A stale value in a hand-written payload is overwritten for
        # the same reason.
        for beat in raw.get("beats") or []:
            if isinstance(beat, dict):
                spoken = len(str(beat.get("voice_text") or "").split())
                beat["target_seconds"] = round(
                    max(spoken, 1) / words_per_second, 2)

        script = Script.model_validate(raw)
        if not script.beats:
            raise AgentError("script", "script contained no beats", data)
        # The word budget is deliberately NOT checked here. It used to be,
        # raising ``Repairable`` into ``_ask``'s single retry — and that
        # retry's message is the schema-repair one, so an over-long script
        # was told its types were wrong and came back the same length. A
        # budget miss is not a parse failure and no longer pretends to be
        # one; ``_fit_to_budget`` handles it below, where the script exists
        # and can be edited.
        return script

    result, script = _ask(client, "script", prompt, model=model,
                          temperature=0.9, parse=parse)
    latin_cost = _fix_latin_script(client, script, model=model,
                                   words_per_second=words_per_second)
    trim_costs = _fit_to_budget(client, script, word_target,
                                model=trim_model,
                                fallback_model=model,
                                words_per_second=words_per_second,
                                max_rounds=trim_rounds)
    extra_costs = ([latin_cost] if latin_cost is not None else []) + trim_costs
    return script, _merge_costs(result.cost, extra_costs)


def run_metadata(client, topic: Topic, script: Script,
                 provenance: Provenance, *, model: str | None = None):
    script_block = "\n".join(
        f"{b.beat_id} [{b.role}] {b.caption_text}" for b in script.beats)
    sources = "\n".join(
        sorted({c.source_url for c in provenance.claims if c.source_url}))
    prompt = load_prompt("metadata").format(
        topic=topic.raw, script=script_block,
        sources=sources or "(none)")
    def parse(data):
        payload = data
        if isinstance(payload, dict) and "metadata" in payload:
            payload = payload["metadata"]
        return Metadata.model_validate(payload)

    result, metadata = _ask(client, "metadata", prompt, model=model,
                            temperature=0.8, parse=parse)
    return metadata, result.cost


def dump_prompt(stage: str, **kwargs) -> str:
    """Render a prompt without calling anything — used by the UI's debug view."""
    return load_prompt(stage).format(**kwargs)


__all__ = ["AgentError", "load_prompt", "dump_prompt", "run_research",
           "run_hooks", "run_script", "run_metadata", "script_words",
           "WORD_TOLERANCE", "json"]

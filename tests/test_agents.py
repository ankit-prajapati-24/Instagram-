import re

import pytest


# --- the repair retry -------------------------------------------------------
# Spec section 9 calls for one repair attempt on a deterministic failure. It
# was never implemented, and a single bad type threw away a whole run.

class Replaying:
    """Returns a scripted sequence of payloads, one per chat call."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.seen: list[list[dict]] = []
        self.models: list[str | None] = []
        self.max_tokens: list[int] = []
        self.calls = []
        self.total_usd = 0.0
        self.default_chat_model = "fake/model"

    def chat(self, messages, *, model=None, want_json=False,
             temperature=0.85, max_tokens=4096):
        from engine.omniroute import ChatResult, CostRecord
        self.seen.append(messages)
        self.models.append(model)
        self.max_tokens.append(max_tokens)
        if not self.payloads:
            raise AssertionError(
                f"the agent made call {len(self.seen)}; only "
                f"{len(self.seen) - 1} payloads were scripted")
        data = self.payloads.pop(0)
        if isinstance(data, Exception):
            raise data
        return ChatResult(text="", cost=CostRecord(usd=0.0002), data=data,
                          raw={})

    @property
    def prompts(self):
        return [m[0]["content"] for m in self.seen]


def test_a_shape_mismatch_is_repaired_on_the_second_attempt():
    from engine.agents import run_hooks
    from engine.contract import Provenance, Topic

    bad = {"hooks": [{"variant_id": "h1", "voice_text": "v",
                      "caption_text": "c", "style": "not-a-style"}]}
    good = {"hooks": [{"variant_id": "h1", "voice_text": "v",
                       "caption_text": "c", "style": "question"}]}

    client = Replaying(bad, good)
    hooks, _ = run_hooks(client, Topic.make("x"), Provenance())
    assert len(hooks) == 1
    assert len(client.seen) == 2, "it should have retried once"

    # the retry must actually tell the model what was wrong
    repair = client.seen[1][-1]["content"]
    assert "did not match the required schema" in repair
    assert "style" in repair


def test_two_failures_in_a_row_still_raise():
    from engine.agents import AgentError, run_hooks
    from engine.contract import Provenance, Topic

    bad = {"hooks": [{"variant_id": "h1", "voice_text": "v",
                      "caption_text": "c", "style": "nope"}]}
    client = Replaying(bad, bad)
    with pytest.raises(AgentError) as exc:
        run_hooks(client, Topic.make("x"), Provenance())
    assert exc.value.stage == "hooks"
    assert len(client.seen) == 2, "exactly one repair attempt, not more"


def test_a_clean_first_answer_makes_no_second_call():
    from engine.agents import run_research
    from engine.contract import Topic

    client = Replaying({"claims": [], "entities": ["X"]})
    provenance, _ = run_research(client, Topic.make("x"))
    assert provenance.entities == ["X"]
    assert len(client.seen) == 1


def test_the_integer_beat_id_from_the_panel_now_parses_first_time():
    """The exact payload that killed a run: claim ids as integers."""
    from engine.agents import run_research
    from engine.contract import Topic

    client = Replaying({"claims": [
        {"beat_id": i, "text": f"claim {i}",
         "source_url": "https://example.org", "confidence": "high"}
        for i in range(1, 5)], "entities": ["Taj Mahal"]})
    provenance, _ = run_research(client, Topic.make("x"))
    assert len(provenance.claims) == 4
    assert provenance.claims[0].beat_id == "1"
    assert len(client.seen) == 1, "coercion should avoid the retry entirely"


# --- the parsing each agent is responsible for ------------------------------
# These were specified in the implementation plan (Task 5) and never written.

def test_run_hooks_parses_five_variants():
    from engine.agents import run_hooks
    from engine.contract import Provenance, Topic

    styles = ["question", "claim", "number", "contradiction", "threat"]
    client = Replaying({"hooks": [
        {"variant_id": f"h{i + 1}", "voice_text": "व", "caption_text": "v",
         "style": s, "seconds": 3.0} for i, s in enumerate(styles)]})

    hooks, cost = run_hooks(client, Topic.make("x"), Provenance())
    assert [h.variant_id for h in hooks] == ["h1", "h2", "h3", "h4", "h5"]
    assert [h.style for h in hooks] == styles
    assert cost is not None


def test_run_hooks_rejects_an_empty_list():
    from engine.agents import AgentError, run_hooks
    from engine.contract import Provenance, Topic

    client = Replaying({"hooks": []}, {"hooks": []})
    with pytest.raises(AgentError) as exc:
        run_hooks(client, Topic.make("x"), Provenance())
    assert exc.value.stage == "hooks"


def test_run_script_rejects_a_bad_shape():
    from engine.agents import AgentError, run_script
    from engine.contract import Provenance, Topic

    client = Replaying({"script": "nope"}, {"script": "still nope"})
    with pytest.raises(AgentError) as exc:
        run_script(client, Topic.make("x"), Provenance(), None)
    assert exc.value.stage == "script"


def test_run_script_rejects_a_script_with_no_beats():
    from engine.agents import AgentError, run_script
    from engine.contract import Provenance, Topic

    empty = {"script": {"total_seconds": 44.0, "chosen_hook": "h1",
                        "beats": []}}
    client = Replaying(empty, empty)
    with pytest.raises(AgentError, match="no beats"):
        run_script(client, Topic.make("x"), Provenance(), None)


def test_run_metadata_unwraps_a_nested_metadata_key():
    """Models wrap the object as often as they return it bare."""
    from engine.agents import run_metadata
    from engine.contract import Provenance, Script, Topic

    payload = {"metadata": {"yt_title": "T", "pinned_comment": "A? B?",
                            "hashtags": ["#a"]}}
    client = Replaying(payload)
    metadata, _ = run_metadata(client, Topic.make("x"),
                               Script(total_seconds=44.0, chosen_hook="h1"),
                               Provenance())
    assert metadata.yt_title == "T"


def test_every_prompt_file_the_agents_load_exists():
    from engine.agents import load_prompt
    for stage in ("research", "hooks", "script", "metadata"):
        assert f"STAGE:{stage}" in load_prompt(stage)


# --- prompts and their callers must not drift apart ------------------------
# A KeyError('word_target') reached the panel: a placeholder was added to
# script.txt while a long-running process still held the old run_script. The
# process was stale, but the class of failure is real - load_prompt reads the
# file at call time, so a prompt can gain a placeholder that no caller fills.

def _placeholders(name):
    """Field names in a prompt, ignoring {{ }} escapes."""
    import re
    from engine.agents import load_prompt
    text = load_prompt(name).replace("{{", "\0").replace("}}", "\0")
    return {m.group(1).split(".")[0].split("[")[0]
            for m in re.finditer(r"\{(\w+)\}", text)}


def test_every_prompt_placeholder_is_supplied_by_its_agent():
    """Call each agent and confirm the prompt formats without a KeyError."""
    from engine.agents import (run_hooks, run_metadata, run_research,
                               run_script)
    from engine.contract import Provenance, Script, Topic

    topic = Topic.make("x")
    provenance = Provenance()

    run_research(Replaying({"claims": [], "entities": []}), topic)
    run_hooks(Replaying({"hooks": [
        {"variant_id": "h1", "voice_text": "v", "caption_text": "c",
         "style": "question"}]}), topic, provenance)
    # word_target matches this one-word payload, so the budget check does
    # not fire; this test is about the prompt formatting, not the budget.
    # voice_text is Devanagari ("व") on purpose -- this test is not about
    # the Latin-script check either, and a Latin placeholder would trip it.
    run_script(Replaying({"script": {
        "total_seconds": 45.0, "chosen_hook": "h1", "beats": [
            {"beat_id": "b1", "role": "hook", "voice_text": "व",
             "caption_text": "c", "target_seconds": 4.0,
             "visual_prompt": "p", "motion": "zoom_in",
             "transition": "fade"}]}}), topic, provenance, None,
        word_target=1)
    run_metadata(Replaying({"yt_title": "t"}), topic,
                 Script(total_seconds=45.0, chosen_hook="h1"), provenance)


def test_the_script_prompt_carries_the_word_budget():
    """Duration follows word count, so the budget must reach the model."""
    assert {"word_target", "words_per_beat"} <= _placeholders("script")


def test_every_budget_dependent_number_in_the_script_prompt_is_a_placeholder():
    """"4 to 18", "4.4" and "44.0" were literals, and they went stale.

    Anything in script.txt that moves when the budget, the beat count or
    the speech rate moves has to be fed from Settings, or the next
    recalibration leaves the model reading last month's arithmetic.
    """
    assert {"word_target", "beats", "words_per_beat", "beat_words_min",
            "beat_words_max", "words_per_second", "seconds_per_beat",
            "total_seconds"} <= _placeholders("script")


def test_prompts_declare_their_stage_marker():
    """The fake client routes on STAGE: markers; a missing one silently
    returns the wrong payload."""
    from engine.agents import load_prompt
    for stage in ("research", "hooks", "script", "metadata"):
        assert load_prompt(stage).lstrip().startswith(f"STAGE:{stage}")


# --- the word budget is enforced at the script stage -----------------------
# A 176-word script against a 136 budget rendered at 99.7s and was rejected
# by QC on duration - after a ten-minute render. One extra call here is far
# cheaper than that.
#
# BUDGET is deliberately not the configured one. These tests are about the
# +/-15% check firing and what it says, not about what the budget currently
# is: they were pinned to 136, and re-measuring the voice broke every one of
# them for no reason. The configured value has its own tests, in
# tests/test_images.py, against the rate the voice was measured at.
BUDGET = 120

def _script_payload(beats_words):
    return {"script": {"total_seconds": 45.0, "chosen_hook": "h1", "beats": [
        {"beat_id": f"b{i}", "role": "setup",
         "voice_text": " ".join(["शब्द"] * n),
         "caption_text": " ".join(["shabd"] * n),
         "target_seconds": 4.0, "visual_prompt": "p",
         "motion": "zoom_in", "transition": "fade"}
        for i, n in enumerate(beats_words)]}}


# --- the word budget is no longer a repair; it is a trim pass -------------
# Measured over five real-model runs of the same task: 122, 123, 136, 152,
# 160 words against budgets of 103-114. The model does not control its own
# output length to a target, however the target is phrased. Four prompt and
# calibration fixes took the overshoot from +70% to ~+25%, and none of them
# made it able to hit a number.
#
# The old repair did not help, because the message it sent was written for
# pydantic errors -- "That did not match the required schema ... keep every
# id a quoted string" -- so a model that wrote 122 words was told its types
# were wrong and returned the same content at 123. It was never asked to
# shorten anything.
#
# Writing exactly N words is a planning problem the model is bad at.
# Shortening existing text to N words is an editing problem it is good at.
# These tests pin the second one as a separate, narrow call.

TWIST_WORD = "\u0932\u0947\u0915\u093f\u0928"        # लेकिन
FILLER = "\u0936\u092c\u094d\u0926"                   # शब्द

ROLES = ["hook", "setup", "setup", "escalation", "escalation", "reveal",
         "twist", "cliffhanger", "cliffhanger", "cta"]

# An uneven, real-shaped script: 150 words against a 120 budget (+25%,
# which is roughly what the real model actually does), a hook at beat 1 and
# a twist at beat 7.
OVER = [14, 18, 9, 20, 6, 17, 22, 8, 21, 15]            # 150
# The same ten lines edited down: 119 words, still uneven.
TRIMMED = [14, 14, 7, 16, 5, 13, 17, 6, 16, 11]         # 119


def _voice(n, twist=False):
    words = [FILLER] * n
    if twist and n:
        words[0] = TWIST_WORD
    return " ".join(words)


def _caption(n):
    return " ".join(["shabd"] * n)


def _story_payload(counts, roles=None):
    """A script payload shaped like a real one: hook first, twist inside."""
    roles = roles or ROLES[:len(counts)]
    return {"script": {"total_seconds": 50.0, "chosen_hook": "h1", "beats": [
        {"beat_id": f"b{i + 1}", "role": roles[i],
         "voice_text": _voice(n, roles[i] == "twist"),
         "caption_text": _caption(n),
         "on_screen_text": "PUNCH CARD" if i == 0 else None,
         "target_seconds": 4.0, "visual_prompt": f"frame {i + 1}",
         "motion": "zoom_in", "transition": "fade"}
        for i, n in enumerate(counts)]}}


def _lines_payload(counts, roles=None, *, caption_counts=None,
                   twist_prefix=True, include_hook=False):
    """What the trim agent returns: ids and the two texts, nothing else."""
    roles = roles or ROLES[:len(counts)]
    caption_counts = caption_counts or counts
    return {"lines": [
        {"beat_id": f"b{i + 1}",
         "voice_text": _voice(n, twist_prefix and roles[i] == "twist"),
         "caption_text": _caption(caption_counts[i])}
        for i, n in enumerate(counts) if include_hook or i > 0]}


def _count(script):
    return sum(len(b.voice_text.split()) for b in script.beats)


def _run(*payloads, **kwargs):
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    client = Replaying(*payloads)
    kwargs.setdefault("word_target", BUDGET)
    script, cost = run_script(client, Topic.make("x"), Provenance(), None,
                              **kwargs)
    return client, script, cost


def test_an_over_budget_script_is_trimmed_not_told_its_schema_was_wrong():
    client, script, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))

    assert _count(script) == sum(TRIMMED)
    assert len(client.seen) == 2, "one script call, one trim call"

    trim = client.prompts[1]
    assert "did not match the required schema" not in trim
    assert "quoted string" not in trim
    assert "STAGE:trim" in trim


def test_the_trim_call_is_told_the_two_numbers_and_the_size_of_the_cut():
    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))
    trim = client.prompts[1]

    # It is an edit of 9 lines (the hook is locked): 136 words down to the
    # 106 that leaves room for the hook's 14 inside a 120-word budget.
    assert "9 lines" in trim
    assert "136" in trim and "106" in trim
    assert "30" in trim, "it should say how many words to remove"
    assert "shorten" in trim.lower()


def test_the_trim_call_sees_only_the_ids_and_the_two_texts():
    """The full script prompt juggles seven beat roles, banned phrases,
    visual prompts, motion, transition and a JSON schema. Under all that,
    word count loses. The trim call must not carry any of it."""
    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))
    script_prompt, trim = client.prompts

    for leaked in ("frame 3", "zoom_in", "slide_left", "escalation",
                   "cliffhanger", "visual_prompt", "on_screen_text",
                   "retention", "aaj hum baat karenge"):
        assert leaked not in trim, f"the trim call should not carry {leaked!r}"

    assert "b3" in trim and "b7" in trim

    # Everything above the lines themselves is the instruction, and that is
    # what the word count has to compete with. In the script prompt it lost.
    instructions = trim.split("LINES:")[0]
    assert len(instructions) < len(script_prompt) * 0.6, (
        f"the trim instructions are {len(instructions)} chars against the "
        f"script prompt's {len(script_prompt)}; word count will lose again")
    assert "106" in trim[:600], "the target is buried, not the headline"


def test_the_hook_beat_is_never_sent_to_the_trim_call_and_never_changes():
    """Beat 1 reuses the chosen hook verbatim, so it is not up for edit."""
    import re

    # the model is handed a replacement for b1 as well; it must be ignored
    client, script, _ = _run(_story_payload(OVER),
                             _lines_payload(TRIMMED, include_hook=True))
    trim = client.prompts[1]
    assert not re.search(r"\bb1\b", trim), "the locked hook was sent anyway"

    hook = script.beats[0]
    assert hook.voice_text == _voice(OVER[0])
    assert len(hook.voice_text.split()) == OVER[0]


def test_a_trim_that_drops_lekin_is_rejected_for_that_beat():
    """The twist beat is the pattern interrupt; it must still begin with
    लेकिन, and an edit that loses the word is not applied."""
    client, script, _ = _run(_story_payload(OVER),
                             _lines_payload(TRIMMED, twist_prefix=False))

    twist = next(b for b in script.beats if b.role == "twist")
    assert twist.voice_text.startswith(TWIST_WORD)
    # the whole replacement was refused, so that beat kept its 22 words
    assert len(twist.voice_text.split()) == OVER[6]
    assert _count(script) == sum(TRIMMED) - TRIMMED[6] + OVER[6]


def test_a_trim_that_shortens_the_voice_but_not_the_caption_is_rejected():
    """caption_text is the Roman mirror of voice_text. If one shortens the
    other must, or the burned-in captions stop matching the narration."""
    captions = list(TRIMMED)
    captions[3] = OVER[3]           # b4's caption left exactly as it was
    client, script, _ = _run(_story_payload(OVER),
                             _lines_payload(TRIMMED, caption_counts=captions))

    b4 = script.beats[3]
    assert len(b4.voice_text.split()) == OVER[3], "the mismatch was applied"
    assert len(b4.caption_text.split()) == OVER[3]
    # every other beat took its edit
    assert len(script.beats[1].voice_text.split()) == TRIMMED[1]


def test_a_trim_that_flattens_every_line_to_one_length_is_thrown_away():
    """Deliberate sentence-length variance is the point of the per-beat
    range, and a flat rhythm is the clearest AI tell there is."""
    from engine.agents import AgentError

    flat = [OVER[0]] + [11] * 9          # 113 words, and every line equal
    assert abs(sum(flat) - BUDGET) < BUDGET * 0.15, "in budget, but flat"

    with pytest.raises(AgentError) as exc:
        _run(_story_payload(OVER), _lines_payload(flat), trim_rounds=1)

    detail = exc.value.detail
    assert "150 -> 150" in detail, (
        f"the flattened round should have been reverted, not applied: "
        f"{detail}")
    assert "even" in detail or "variance" in detail or "rhythm" in detail


def test_a_trim_changes_only_the_two_texts_and_the_derived_seconds():
    from engine.contract import Script

    before = Script.model_validate(_story_payload(OVER)["script"])
    _, after, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))

    drop = {"voice_text", "caption_text", "target_seconds"}
    for old, new in zip(before.beats, after.beats):
        assert old.model_dump(exclude=drop) == new.model_dump(exclude=drop)
    # and the derived seconds followed the new word count
    edited = after.beats[1]
    assert edited.target_seconds == pytest.approx(
        len(edited.voice_text.split()) / 2.29, abs=0.02)


def test_the_trim_loop_runs_again_when_one_pass_is_not_enough():
    client, script, _ = _run(_story_payload(OVER),
                             _lines_payload([14, 17, 9, 19, 6, 16, 21, 8,
                                             20, 15]),      # 145, still over
                             _lines_payload(TRIMMED))       # 119, in range
    assert len(client.seen) == 3
    assert _count(script) == sum(TRIMMED)


def test_a_script_still_over_budget_at_the_round_cap_raises_with_the_numbers():
    """Do not silently accept an over-budget script."""
    from engine.agents import AgentError

    barely = [_lines_payload(c) for c in (
        [14, 17, 9, 19, 6, 16, 21, 8, 20, 15],     # 145
        [14, 17, 9, 19, 6, 16, 21, 8, 19, 14],     # 143
        [14, 16, 9, 19, 6, 16, 21, 8, 19, 14])]    # 142

    with pytest.raises(AgentError) as exc:
        _run(_story_payload(OVER), *barely, trim_rounds=3)

    detail = exc.value.detail
    assert exc.value.stage == "script"
    for number in ("150", "142", str(BUDGET), "3"):
        assert number in detail, f"{number} missing from {detail!r}"


def test_a_script_inside_the_tolerance_pays_for_no_trim_at_all():
    inside = [12, 15, 8, 14, 6, 13, 16, 7, 15, 11]    # 117, within 15%
    assert abs(sum(inside) - BUDGET) < BUDGET * 0.15
    client, script, _ = _run(_story_payload(inside))
    assert len(client.seen) == 1
    assert _count(script) == sum(inside)


def test_the_trim_runs_on_the_cheap_model_not_the_strong_one(monkeypatch):
    from engine.config import settings, trim_model

    # Ensure model_trim is unset, so the test is deterministic regardless of .env
    monkeypatch.setattr(settings, "model_trim", "")

    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED),
                        model="strong/one")
    assert client.models == ["strong/one", trim_model()]
    assert trim_model() == settings.model_cheap, (
        "unset, the trim editor is just the cheap model")


def test_the_trim_model_is_formed_in_one_place(monkeypatch):
    """model_trim exists because this call needs strict JSON and the
    cheap model does not always give it; unset it must not diverge."""
    from engine.config import settings, trim_model

    monkeypatch.setattr(settings, "model_trim", "no-think/cheap/one")
    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED),
                        model="strong/one")
    assert trim_model() == "no-think/cheap/one"
    assert client.models == ["strong/one", "no-think/cheap/one"]


def test_an_unreadable_trim_round_escalates_to_the_strong_model():
    """Measured: the configured cheap model answers a strict-JSON request
    with its own reasoning about one round in three, and that killed the
    whole stage twice in six real runs. The strong model has already been
    paid for once here; it finishes the job rather than losing the script."""
    from engine.omniroute import OmniRouteError

    client, script, _ = _run(
        _story_payload(OVER),
        OmniRouteError("no JSON found in response: 'Wait, what about...'"),
        _lines_payload(TRIMMED),
        model="strong/one")

    assert client.models[1] != "strong/one", "round 1 is the cheap editor"
    assert client.models[2] == "strong/one", "round 2 should have escalated"
    assert _count(script) == sum(TRIMMED)


def test_a_round_that_was_read_and_rejected_does_not_escalate():
    """A flattened or twist-breaking edit is a content problem, and a
    bigger model is not the answer to it — it is the same prompt again
    with the complaint attached."""
    from engine.config import trim_model

    flat = [OVER[0]] + [11] * 9
    client, script, _ = _run(_story_payload(OVER), _lines_payload(flat),
                             _lines_payload(TRIMMED), model="strong/one")
    assert client.models[1] == client.models[2] == trim_model()
    assert _count(script) == sum(TRIMMED)


def test_the_trim_cost_is_added_to_what_the_stage_reports():
    """Two calls were made; the pipeline records one cost for the stage."""
    _, _, cost = _run(_story_payload(OVER), _lines_payload(TRIMMED))
    assert cost.usd == pytest.approx(0.0004)


def test_a_short_script_is_edited_up_rather_than_schema_repaired():
    short = [6, 7, 4, 8, 3, 7, 9, 4, 8, 5]            # 61, far under
    assert sum(short) < BUDGET * 0.85
    client, script, _ = _run(_story_payload(short), _lines_payload(TRIMMED))

    trim = client.prompts[1]
    assert "did not match the required schema" not in trim
    assert "lengthen" in trim.lower()
    # the hook keeps its own six words; only the nine editable lines moved
    assert _count(script) == short[0] + sum(TRIMMED[1:])


def test_the_budget_check_left_the_parse_callback_with_repairable():
    """``Repairable`` existed so an off-budget script could ride the schema
    repair path. That path is exactly what made the repair useless here, so
    the check moved out of parse and the exception has no reason to exist."""
    import engine.agents as agents

    assert not hasattr(agents, "Repairable"), (
        "Repairable still exists, so a semantic failure can still reach the "
        "schema-repair message")


def test_a_real_schema_failure_still_gets_the_schema_repair_message():
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    bad = _story_payload([12] * 10)
    bad["script"]["beats"][2]["motion"] = "barrel_roll"
    good = _story_payload([12] * 10)

    client = Replaying(bad, good)
    run_script(client, Topic.make("x"), Provenance(), None,
               word_target=BUDGET)
    repair = client.seen[1][-1]["content"]
    assert "did not match the required schema" in repair
    assert "motion" in repair


# Both of these are real failures from the first three real-model runs of
# this trim pass, not hypotheticals. Run 1: the cheap model answered with a
# ```json fence that ran past max_tokens and stopped mid-string, so
# extract_json found no JSON at all. Run 3: it answered with one bare
# Devanagari sentence and no envelope. Neither is a reason to throw away a
# script the strong model already wrote -- a bad round is a wasted round,
# and the round cap is what turns a persistent one into a clean failure.

def test_a_trim_answer_that_is_not_json_costs_a_round_not_the_stage():
    from engine.omniroute import OmniRouteError

    client, script, _ = _run(
        _story_payload(OVER),
        OmniRouteError("no JSON found in response: '```json\n{\n  \"li'"),
        _lines_payload(TRIMMED))

    assert len(client.seen) == 3
    assert _count(script) == sum(TRIMMED)
    assert "JSON" in client.prompts[2], (
        "the next round should say the last answer was not JSON")


def test_a_trim_answer_with_no_lines_array_costs_a_round():
    client, script, _ = _run(_story_payload(OVER),
                             {"beats": [{"beat_id": "b2"}]},
                             _lines_payload(TRIMMED))
    assert len(client.seen) == 3
    assert _count(script) == sum(TRIMMED)


def test_every_bad_trim_answer_in_a_row_still_fails_with_the_numbers():
    from engine.agents import AgentError
    from engine.omniroute import OmniRouteError

    bad = OmniRouteError("no JSON found in response: ''")
    with pytest.raises(AgentError) as exc:
        _run(_story_payload(OVER), bad, bad, bad, trim_rounds=3)
    assert "150" in exc.value.detail and "3 trim rounds" in exc.value.detail


def test_the_trim_call_is_given_room_for_thinking_and_a_devanagari_answer():
    """Two of the first six real runs died on a truncated answer.

    Measured on the gateway: prompt 3,478 + completion 737 came back as
    total 6,466, so ~2,250 reasoning tokens were charged against the output
    budget. The answer itself is only ~750 tokens; the ceiling is for what
    the model spends before it starts writing."""
    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))
    assert client.max_tokens[1] >= 16384, (
        f"the trim call asked for {client.max_tokens[1]} tokens")


def test_the_trim_prompt_exists_and_declares_its_stage():
    from engine.agents import load_prompt
    assert load_prompt("trim").lstrip().startswith("STAGE:trim")


def test_every_trim_prompt_placeholder_is_supplied_by_the_trim_call():
    """A placeholder added to trim.txt with no caller is a KeyError in
    production and nowhere else -- the bug script.txt already had once."""
    import re

    client, _, _ = _run(_story_payload(OVER), _lines_payload(TRIMMED))
    left = re.findall(r"\{[a-z_]+\}", client.prompts[1])
    assert not left, f"unfilled placeholders in the trim prompt: {left}"


# --- the default budget must not be a hand-copied number -------------------
# The signature said 136 for a while after target_seconds * words_per_second
# became 103, which is the same stale-number problem the budget exists to
# prevent. Re-typing int(45 * 2.29) as 103 re-introduced it one rate change
# later, so the default is resolved from Settings at call time instead.

def test_the_word_target_default_is_resolved_from_configuration():
    import inspect

    from engine.agents import run_script

    default = inspect.signature(run_script).parameters["word_target"].default
    assert default is None, (
        f"word_target defaults to the literal {default!r}; it goes stale the "
        f"next time the speech rate is re-measured")


def test_the_resolved_budget_follows_the_configured_rate(monkeypatch):
    """Change the configuration, and the prompt the model sees changes."""
    from engine.agents import run_script
    from engine.config import settings
    from engine.contract import Provenance, Topic

    monkeypatch.setattr(settings, "target_seconds", 50.0)
    monkeypatch.setattr(settings, "words_per_second", 2.0)
    expected = int(50.0 * 2.0)

    client = Replaying(_script_payload([expected]))
    run_script(client, Topic.make("x"), Provenance(), None)

    prompt = client.seen[0][0]["content"]
    assert f"{expected}" in prompt
    # and the +/-15% check ran against the resolved number, not a literal:
    # a single beat of exactly `expected` words was accepted first time.
    assert len(client.seen) == 1


def test_the_resolved_budget_is_what_the_pipeline_would_have_passed():
    """plan_stage computes the same product; the two must not disagree."""
    from engine.agents import run_script
    from engine.config import settings
    from engine.contract import Provenance, Topic

    expected = int(settings.target_seconds * settings.words_per_second)
    client = Replaying(_script_payload([expected]))
    run_script(client, Topic.make("x"), Provenance(), None)
    assert f"{expected}" in client.seen[0][0]["content"]


def test_the_budget_product_is_formed_in_one_place():
    """Two copies of target_seconds * words_per_second is how it rotted."""
    import engine.pipeline as pipeline
    from engine.config import settings, word_budget

    assert word_budget() == int(settings.target_seconds *
                                settings.words_per_second)
    assert pipeline.word_budget is word_budget


# --- the beat count must not be a hand-copied number either ----------------
# 2026-09-21: pipeline.py passed beats=12 and run_script's own signature
# defaulted to beats=12 -- two literal copies of the same number, which is
# exactly the failure mode word_target above already had. A recalibration
# from 136 to a 103-word budget left both untouched, so the model was asked
# for 8.6 words/beat and wrote 161 words instead of 103, failing even after
# the repair retry. These tests pin the fix the same way the word_target
# ones do: resolved from configuration, in one place, at call time.

def test_the_beats_default_is_resolved_from_configuration():
    import inspect

    from engine.agents import run_script

    default = inspect.signature(run_script).parameters["beats"].default
    assert default is None, (
        f"beats defaults to the literal {default!r}; it goes stale the next "
        f"time the word budget is recalibrated")


def test_the_resolved_beats_follows_the_configured_count(monkeypatch):
    from engine.agents import run_script
    from engine.config import settings
    from engine.contract import Provenance, Topic

    monkeypatch.setattr(settings, "script_beats", 5)
    expected_words_per_beat = max(round(BUDGET / 5), 4)

    client = Replaying(_script_payload([expected_words_per_beat] * 5))
    run_script(client, Topic.make("x"), Provenance(), None,
              word_target=BUDGET)

    prompt = client.seen[0][0]["content"]
    assert f"{expected_words_per_beat}" in prompt


def test_the_resolved_beats_is_what_the_pipeline_would_have_passed():
    """plan_stage's beats= must not disagree with run_script's own default."""
    from engine.agents import run_script
    from engine.config import beat_count
    from engine.contract import Provenance, Topic

    expected = beat_count()
    words_per_beat = max(round(BUDGET / expected), 4)
    client = Replaying(_script_payload([words_per_beat] * expected))
    run_script(client, Topic.make("x"), Provenance(), None,
              word_target=BUDGET)
    assert f"{words_per_beat}" in client.seen[0][0]["content"]


def test_the_beat_count_is_formed_in_one_place():
    """Two copies of the beat count is how word_target's default rotted."""
    import engine.pipeline as pipeline
    from engine.config import beat_count, settings

    assert beat_count() == settings.script_beats
    assert pipeline.beat_count is beat_count


def test_words_per_beat_stays_in_a_band_the_model_will_write():
    """The 2026-09-21 regression, pinned as an invariant.

    12 beats against a 103-word budget asked for 8.6 words/beat, and the
    model wrote 161 words instead -- 56% over, and it failed again after
    the repair retry. The old world -- 12 beats x 11.3 words/beat, a
    136-word budget -- is the last ratio known to work.

    Bounds: 9.0 is set just above the 8.6 that failed; 14.0 leaves headroom
    above the 11.3 that worked (and above 9 beats' 11.4) without being so
    loose it would wave through another 8-point ratio. Both ends are drawn
    from this evidence, not from taste.
    """
    from engine.config import beat_count, word_budget
    from tests.factories import shipped_settings

    settings = shipped_settings()
    words_per_beat = word_budget(settings) / beat_count(settings)

    assert 9.0 <= words_per_beat <= 14.0, (
        f"{words_per_beat:.1f} words/beat ({word_budget(settings)} words "
        f"over {beat_count(settings)} beats) is outside the band the "
        f"model can actually write to")


# --- the prompt must not contradict its own configuration ------------------
# 2026-09-22: the budget was 103 words over 10 beats (10.3 a beat) while
# HARD RULE 1 still read "4 to 18 spoken words per beat" and the JSON
# example still showed total_seconds 44.0 / target_seconds 4.4. Those
# literals were calibrated against 136 words over 12 beats and never moved.
# 4.4 seconds at natural Hindi speech (~3.8 w/s) is 17 words, which is also
# the top of the stated range, so every instinct the model had pointed at
# ~17 words a beat -- and it wrote 17.5, twice, +56% and +70%. The word
# target was the only thing saying otherwise and it lost.
#
# This test exercises no model. It asserts that the numbers in the rendered
# prompt agree with word_budget / beat_count at the configured speech rate.

def _rendered_script_prompt(config):
    """The script prompt as ``config`` would render it."""
    from engine.agents import run_script
    from engine.config import beat_count, speech_rate, word_budget
    from engine.contract import Provenance, Topic

    target, beats = word_budget(config), beat_count(config)
    per_beat = [target // beats] * beats
    per_beat[0] += target - sum(per_beat)      # on budget, so no repair call
    client = Replaying(_script_payload(per_beat))
    run_script(client, Topic.make("x"), Provenance(), None,
               word_target=target, beats=beats,
               words_per_second=speech_rate(config))
    return client.seen[0][0]["content"]


def test_the_prompt_states_the_speech_rate_it_reckons_seconds_at():
    """The model cannot know this voice is slower than conversational."""
    from engine.config import speech_rate
    from tests.factories import shipped_settings

    config = shipped_settings()
    prompt = _rendered_script_prompt(config)
    assert f"{speech_rate(config):g}" in prompt, (
        "the prompt never tells the model the words-per-second it must "
        "reckon seconds at, so it will use a conversational rate")


def test_no_number_in_the_script_prompt_contradicts_the_budget():
    import re

    from engine.config import beat_count, speech_rate, word_budget
    from tests.factories import shipped_settings

    config = shipped_settings()
    budget, beats = word_budget(config), beat_count(config)
    average, rate = budget / beats, speech_rate(config)
    prompt = _rendered_script_prompt(config)

    bands = re.findall(r"(\d+) to (\d+) spoken words per beat", prompt)
    assert bands, "the prompt no longer states a per-beat word range"
    for low, high in ((int(a), int(b)) for a, b in bands):
        assert low <= average <= high, (
            f"the per-beat range {low}-{high} does not even contain the "
            f"{average:.1f} words/beat the budget implies")
        midpoint = (low + high) / 2
        assert abs(midpoint - average) <= 0.6, (
            f"the per-beat range {low}-{high} is centred on {midpoint}, not "
            f"on the {average:.1f} words/beat the budget implies; a model "
            f"writing to the middle of the range misses the budget")
        assert high <= 1.65 * average, (
            f"the per-beat range tops out at {high}, {high / average:.2f}x "
            f"the {average:.1f} words/beat the budget implies; a model "
            f"drifting to the top of the range overshoots by "
            f"{(high / average - 1) * 100:.0f}%")

    # Every seconds figure must be a word count at THIS voice's rate --
    # either one beat's worth or the whole script's. A figure that only
    # works at a conversational rate is the contradiction that shipped.
    figures = {float(s) for s in
               re.findall(r"(\d+(?:\.\d+)?)[\s-]*second", prompt)}
    figures |= {float(s) for s in re.findall(
        r'"(?:target|total)_seconds"\s*:\s*(\d+(?:\.\d+)?)', prompt)}
    for value in figures:
        words = value * rate
        assert (abs(words - average) <= 1.0
                or abs(words - budget) <= 1.5), (
            f"{value} seconds is {words:.0f} words at {rate:g} w/s, which is "
            f"neither the {average:.1f} words/beat nor the {budget}-word "
            f"budget the configuration implies")


# --- Latin script must not reach the Hindi TTS ------------------------------
# A user listened to a rendered video and heard mispronounced words. Cause:
# script.txt already says "No English words in Latin script -- transliterate
# them", and the model broke the rule inconsistently within one script --
# "fog" and "magnetic anomaly" left in Latin in beats 1 and 8, the very same
# words correctly transliterated to Devanagari in beat 7. Piper does not
# skip Latin text, it speaks it -- measured durations for both forms were
# within 2% of each other -- so this is a spelling bug, not a dropped beat,
# and the user confirmed by ear that the transliterated version is correct.
#
# The rule was prose only, nothing enforced it, and that is the actual
# defect: a deterministic, cheaply checkable property left to the model's
# goodwill. These tests pin the fix the same way the word-budget one was
# pinned above: caught after the shape check succeeds (never inside
# ``parse``, for the same reason the budget check was moved out of it -- see
# that section), and repaired with a message that says plainly which beats
# and which words, not that the JSON was malformed.

def _script_with_voice(rows):
    """rows: iterable of (beat_id, role, voice_text, caption_text)."""
    return {"script": {"total_seconds": 45.0, "chosen_hook": "h1", "beats": [
        {"beat_id": beat_id, "role": role, "voice_text": voice,
         "caption_text": caption, "target_seconds": 4.0,
         "visual_prompt": "p", "motion": "zoom_in", "transition": "fade"}
        for beat_id, role, voice, caption in rows]}}


def test_latin_script_in_voice_text_is_caught_and_repaired():
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    dirty = _script_with_voice([
        ("b1", "hook",
         "Scientists कहते हैं fog, कहते हैं magnetic anomaly",
         "Scientists kehte hain fog, kehte hain magnetic anomaly"),
        ("b2", "cta",
         "लेकिन कोई एक थ्योरी आज तक confirm नहीं हुई",
         "lekin koi ek theory aaj tak confirm nahi hui"),
    ])
    fixed = {"lines": [
        {"beat_id": "b1",
         "voice_text": "वैज्ञानिक कहते हैं फॉग, कहते हैं मैग्नेटिक एनॉमली"},
        {"beat_id": "b2",
         "voice_text": "लेकिन कोई एक थ्योरी आज तक कन्फर्म नहीं हुई"},
    ]}
    # word_target set to the exact post-repair count so the trim pass never
    # fires -- these tests are about the Latin-script fix, not the budget.
    target = 8 + 9

    client = Replaying(dirty, fixed)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=target)

    assert len(client.seen) == 2, "one script call, one Latin-script repair"
    for beat in script.beats:
        assert not re.search(r"[A-Za-z]", beat.voice_text), (
            f"{beat.beat_id} still has Latin script: {beat.voice_text!r}")

    # caption_text is Roman Hinglish on purpose and must not be touched
    assert script.beats[0].caption_text == (
        "Scientists kehte hain fog, kehte hain magnetic anomaly")

    repair = client.prompts[1]
    assert "did not match the required schema" not in repair
    assert "quoted string" not in repair
    for needle in ("b1", "b2", "fog", "magnetic", "confirm", "Devanagari"):
        assert needle in repair, f"{needle!r} missing from the repair prompt"


def test_a_clean_devanagari_script_makes_no_latin_repair_call():
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    clean = _script_with_voice([
        ("b1", "hook", "वैज्ञानिक कहते हैं फॉग", "Scientists kehte hain fog"),
        ("b2", "cta", "लेकिन कोई थ्योरी कन्फर्म नहीं हुई",
         "lekin koi theory confirm nahi hui"),
    ])
    client = Replaying(clean)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=4 + 6)

    assert len(client.seen) == 1, "a clean script must not trigger a repair"
    assert script.beats[0].voice_text == "वैज्ञानिक कहते हैं फॉग"
    assert script.beats[1].voice_text == "लेकिन कोई थ्योरी कन्फर्म नहीं हुई"


def test_caption_text_in_roman_hinglish_never_triggers_the_latin_check():
    """caption_text is Roman Hinglish by design (script.txt: 'Keep
    well-known English words in Latin') -- only voice_text feeds the TTS."""
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    clean_voice_english_caption = _script_with_voice([
        ("b1", "hook", "वैज्ञानिक कहते हैं फॉग",
         "DNA test confirms the fog theory was wrong"),
    ])
    client = Replaying(clean_voice_english_caption)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=4)

    assert len(client.seen) == 1, (
        "English in caption_text must never trigger the Latin-script check")
    assert script.beats[0].caption_text == (
        "DNA test confirms the fog theory was wrong")


def test_ascii_digits_in_voice_text_are_not_a_latin_script_violation():
    """script.txt asks for numbers as digits; phonemizing '1965' against the
    Devanagari-digit form produces identical phonemes under this voice's
    espeak frontend (see the fix report) -- the digit's script does not
    change how it is read, so ASCII digits must never trip this check."""
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    payload = _script_with_voice([
        ("b1", "hook", "अक्टूबर 1965 में तूफान आया", "october 1965 mein toofan aaya"),
    ])
    client = Replaying(payload)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=5)

    assert len(client.seen) == 1, "ASCII digits must not trigger a repair"
    assert script.beats[0].voice_text == "अक्टूबर 1965 में तूफान आया"


def test_devanagari_punctuation_and_en_dash_are_not_latin_script_violations():
    """The en dash and the Devanagari abbreviation sign (॰, U+0970 -- beat 4
    of the reported run used 'ई॰पी॰ गी') are not Latin letters and must not
    trip this check."""
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    text = "ई॰पी॰ गी – यह घटना 1965 में हुई।"
    payload = _script_with_voice([("b1", "hook", text, "caption")])
    client = Replaying(payload)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=len(text.split()))

    assert len(client.seen) == 1
    assert script.beats[0].voice_text == text


def test_latin_script_still_present_after_one_repair_attempt_raises():
    """One repair attempt, matching the schema-repair convention elsewhere
    in this module. A script still mispronouncing words after being told
    exactly which ones is a real failure, not something to ship."""
    from engine.agents import AgentError, run_script
    from engine.contract import Provenance, Topic

    dirty = _script_with_voice([
        ("b1", "hook", "कहते हैं fog", "kehte hain fog"),
    ])
    # The model answers the repair with the same Latin word still in place.
    still_dirty = {"lines": [{"beat_id": "b1", "voice_text": "कहते हैं fog"}]}

    client = Replaying(dirty, still_dirty)
    with pytest.raises(AgentError) as exc:
        run_script(client, Topic.make("x"), Provenance(), None,
                   word_target=3)

    assert exc.value.stage == "script"
    assert "b1" in exc.value.detail and "fog" in exc.value.detail
    assert len(client.seen) == 2, "exactly one repair attempt, not more"


def test_a_stray_latin_letter_inside_a_devanagari_word_is_caught():
    """Any Latin letter is a violation, even one character inside an
    otherwise-Devanagari word -- Piper does not partially mispronounce a
    word, and there is no safe amount of Latin script to allow."""
    from engine.agents import run_script
    from engine.contract import Provenance, Topic

    dirty = _script_with_voice([("b1", "hook", "मैग्नेटिकA एनॉमली", "caption")])
    fixed = {"lines": [{"beat_id": "b1", "voice_text": "मैग्नेटिक एनॉमली"}]}
    client = Replaying(dirty, fixed)
    script, _ = run_script(client, Topic.make("x"), Provenance(), None,
                           word_target=2)

    assert len(client.seen) == 2
    assert not re.search(r"[A-Za-z]", script.beats[0].voice_text)

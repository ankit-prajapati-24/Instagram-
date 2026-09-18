import pytest


# --- the repair retry -------------------------------------------------------
# Spec section 9 calls for one repair attempt on a deterministic failure. It
# was never implemented, and a single bad type threw away a whole run.

class Replaying:
    """Returns a scripted sequence of payloads, one per chat call."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.seen: list[list[dict]] = []
        self.calls = []
        self.total_usd = 0.0
        self.default_chat_model = "fake/model"

    def chat(self, messages, *, model=None, want_json=False,
             temperature=0.85, max_tokens=4096):
        from engine.omniroute import ChatResult, CostRecord
        self.seen.append(messages)
        data = self.payloads.pop(0)
        return ChatResult(text="", cost=CostRecord(), data=data, raw={})


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
    run_script(Replaying({"script": {
        "total_seconds": 45.0, "chosen_hook": "h1", "beats": [
            {"beat_id": "b1", "role": "hook", "voice_text": "v",
             "caption_text": "c", "target_seconds": 4.0,
             "visual_prompt": "p", "motion": "zoom_in",
             "transition": "fade"}]}}), topic, provenance, None)
    run_metadata(Replaying({"yt_title": "t"}), topic,
                 Script(total_seconds=45.0, chosen_hook="h1"), provenance)


def test_the_script_prompt_carries_the_word_budget():
    """Duration follows word count, so the budget must reach the model."""
    assert {"word_target", "words_per_beat"} <= _placeholders("script")


def test_prompts_declare_their_stage_marker():
    """The fake client routes on STAGE: markers; a missing one silently
    returns the wrong payload."""
    from engine.agents import load_prompt
    for stage in ("research", "hooks", "script", "metadata"):
        assert load_prompt(stage).lstrip().startswith(f"STAGE:{stage}")

"""Writing the script somewhere else and bringing it back as JSON.

Typing ten beats into the panel by hand is the slowest part of the manual
path. This lets a script be drafted in whatever tool the user likes and
imported in one paste — without the panel ever calling a model, which is
the property the manual path exists for.

Two things these tests are really guarding:

**The prompt cannot carry a stale number.** Every figure in it — how many
beats, the word budget and its band, the per-beat range, the roles — comes
from ``authoring_facts``, the same dict the blank form is rendered from.
A prompt that said "ten beats" while ``RAHASYA_SCRIPT_BEATS`` said twelve
would waste a whole round trip through another tool, and this codebase has
already shipped two bugs from copies of exactly these numbers drifting.

**An import never quietly fixes anything.** It fills the fields another
tool cannot reasonably be asked for — the beat ids, the role arc, the
motion and transition — and says so; everything else it refuses and
explains. A silent repair here would put text into the pipeline that
nobody read.
"""

from __future__ import annotations

import json

import pytest

from engine.authoring import authoring_facts, build_prompt, read_script_json
from engine.config import Settings

DEVA = "जैसलमेर से अठारह किलोमीटर दूर एक गाँव है जो कभी नहीं बसा"
ROMAN = "Jaisalmer se 18 kilometre door, ek gaon jo kabhi nahi basa"
SCENE = "Wide shot of a ruined sandstone village at dusk in the desert"


@pytest.fixture()
def settings():
    return Settings()


@pytest.fixture()
def facts(settings):
    return authoring_facts(settings, topic_max=160)


def _beats(n, *, voice=DEVA, roman=ROMAN, scene=SCENE, **extra):
    return [{"voice_text": voice, "caption_text": roman,
             "visual_prompt": scene, **extra} for _ in range(n)]


def _payload(n, **extra):
    return json.dumps({"topic": "Kuldhara", "beats": _beats(n), **extra},
                      ensure_ascii=False)


# --- the prompt -------------------------------------------------------------


def test_the_prompt_carries_the_topic(facts):
    assert "Kuldhara" in build_prompt("Kuldhara", facts)


def test_every_number_in_the_prompt_comes_from_the_facts(facts):
    """Change the facts, the prompt changes with them.

    Checked by building the same prompt against two different fact sets
    rather than by reading the numbers out of one — a hardcoded figure
    would survive the first check and fail this one.
    """
    small = {**facts, "beat_count": 7, "word_budget": 99,
             "beat_words_min": 3, "beat_words_max": 9,
             "default_roles": ["hook"] * 7}
    text = build_prompt("x", small)

    assert "7" in text and "99" in text
    assert str(facts["beat_count"]) not in text.replace("7", ""), \
        "the real beat count leaked in as a literal"


def test_the_prompt_refuses_to_ask_for_source_urls(facts):
    """A model asked for sources invents plausible ones, and an import
    would file them as provenance nothing can check."""
    text = build_prompt("x", facts).lower()
    assert "source" in text
    assert "do not" in text or "don't" in text


def test_the_prompt_states_the_devanagari_rule(facts):
    text = build_prompt("x", facts)
    assert "Devanagari" in text
    assert "caption_text" in text and "voice_text" in text


def test_the_prompt_asks_for_the_three_fields_the_import_requires(facts):
    text = build_prompt("x", facts)
    for field in ("voice_text", "caption_text", "visual_prompt"):
        assert field in text


# --- a minimal import -------------------------------------------------------


def test_three_fields_a_beat_is_enough(facts, settings):
    result = read_script_json(_payload(facts["beat_count"]), facts, settings)

    assert result.problems == []
    assert len(result.beats) == facts["beat_count"]
    first = result.beats[0]
    assert first["voice_text"] == DEVA
    assert first["caption_text"] == ROMAN
    assert first["visual_prompt"] == SCENE


def test_the_fields_another_tool_cannot_be_asked_for_are_filled(
        facts, settings):
    result = read_script_json(_payload(facts["beat_count"]), facts, settings)

    ids = [b["beat_id"] for b in result.beats]
    assert ids == [f"b{i + 1}" for i in range(facts["beat_count"])]
    assert [b["role"] for b in result.beats] == facts["default_roles"]
    assert all(b["motion"] in facts["motions"] for b in result.beats)
    assert all(b["transition"] in facts["transitions"] for b in result.beats)


def test_what_was_filled_is_reported_not_silent(facts, settings):
    result = read_script_json(_payload(facts["beat_count"]), facts, settings)

    filled = " ".join(result.filled)
    for field in ("role", "motion", "transition", "beat_id"):
        assert field in filled, f"{field} was filled without saying so"


def test_a_field_that_was_supplied_is_kept(facts, settings):
    raw = json.dumps({"topic": "Kuldhara",
                      "beats": _beats(facts["beat_count"], role="twist",
                                      motion="zoom_out",
                                      transition="slide_left",
                                      on_screen_text="18 KM")},
                     ensure_ascii=False)

    result = read_script_json(raw, facts, settings)

    assert result.problems == []
    assert {b["role"] for b in result.beats} == {"twist"}
    assert {b["motion"] for b in result.beats} == {"zoom_out"}
    assert {b["transition"] for b in result.beats} == {"slide_left"}
    assert result.beats[0]["on_screen_text"] == "18 KM"


# --- what it refuses --------------------------------------------------------


def test_the_wrong_number_of_beats_is_named_with_both_numbers(
        facts, settings):
    result = read_script_json(_payload(facts["beat_count"] - 2), facts,
                              settings)

    assert result.problems
    joined = " ".join(result.problems)
    assert str(facts["beat_count"] - 2) in joined
    assert str(facts["beat_count"]) in joined


def test_latin_in_the_spoken_line_is_refused_per_beat(facts, settings):
    beats = _beats(facts["beat_count"])
    beats[1]["voice_text"] = "meri fees maaf kardo"
    raw = json.dumps({"topic": "Kuldhara", "beats": beats},
                     ensure_ascii=False)

    result = read_script_json(raw, facts, settings)

    joined = " ".join(result.problems)
    assert "b2" in joined
    assert "meri" in joined and "fees" in joined


def test_the_devanagari_rule_is_the_shared_one_not_a_copy():
    from engine.agents import latin_words as agents_rule
    from engine.authoring import latin_words as authoring_rule

    assert authoring_rule is agents_rule


def test_a_script_outside_the_word_budget_is_refused(facts, settings):
    long_line = " ".join(["शब्द"] * 60)
    raw = json.dumps(
        {"topic": "Kuldhara",
         "beats": _beats(facts["beat_count"], voice=long_line)},
        ensure_ascii=False)

    result = read_script_json(raw, facts, settings)

    joined = " ".join(result.problems)
    assert str(facts["word_max"]) in joined


def test_a_missing_required_field_names_the_beat(facts, settings):
    beats = _beats(facts["beat_count"])
    del beats[2]["visual_prompt"]
    raw = json.dumps({"topic": "Kuldhara", "beats": beats},
                     ensure_ascii=False)

    result = read_script_json(raw, facts, settings)

    joined = " ".join(result.problems)
    assert "b3" in joined and "visual_prompt" in joined


def test_an_empty_required_field_is_refused_like_a_missing_one(
        facts, settings):
    beats = _beats(facts["beat_count"])
    beats[0]["caption_text"] = "   "
    raw = json.dumps({"topic": "Kuldhara", "beats": beats},
                     ensure_ascii=False)

    assert "b1" in " ".join(
        read_script_json(raw, facts, settings).problems)


def test_broken_json_says_where_it_broke(facts, settings):
    result = read_script_json('{"topic": "x", "beats": [ }', facts, settings)

    assert result.problems
    assert any("line" in p.lower() or "char" in p.lower()
               for p in result.problems), result.problems
    assert result.beats == []


def test_something_that_is_not_an_object_is_refused_clearly(facts, settings):
    for raw in ("[]", '"just a string"', "42"):
        result = read_script_json(raw, facts, settings)
        assert result.problems, raw
        assert result.beats == []


def test_beats_missing_entirely_is_refused_clearly(facts, settings):
    result = read_script_json('{"topic": "x"}', facts, settings)
    assert any("beats" in p for p in result.problems)


def test_an_unknown_role_is_refused_rather_than_replaced(facts, settings):
    raw = json.dumps(
        {"topic": "Kuldhara",
         "beats": _beats(facts["beat_count"], role="spooky")},
        ensure_ascii=False)

    joined = " ".join(read_script_json(raw, facts, settings).problems)
    assert "spooky" in joined


# --- sources ----------------------------------------------------------------


def test_sources_in_the_json_are_dropped_and_the_drop_is_reported(
        facts, settings):
    """The prompt tells the other tool not to supply them. If they arrive
    anyway they are not filed as provenance — a model's invented URL
    looks exactly like a real one, and nothing downstream can tell."""
    raw = json.dumps(
        {"topic": "Kuldhara", "beats": _beats(facts["beat_count"]),
         "sources": [{"beat_id": "b1", "text": "1235 mein basa",
                      "source_url": "https://example.invalid/kuldhara"}]},
        ensure_ascii=False)

    result = read_script_json(raw, facts, settings)

    assert result.dropped_sources == 1
    assert any("source" in p.lower() for p in result.notes)
    assert all("example.invalid" not in json.dumps(b)
               for b in result.beats)


def test_no_sources_means_nothing_to_report(facts, settings):
    result = read_script_json(_payload(facts["beat_count"]), facts, settings)
    assert result.dropped_sources == 0


# --- the routes -------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from engine.app import create_app

    s = Settings()
    s.db_path = tmp_path / "t.db"
    s.out_dir = tmp_path / "out"
    s.work_dir = tmp_path / "work"
    s.omniroute_base = "http://127.0.0.1:1/v1"
    return TestClient(create_app(settings=s))


def test_the_form_and_the_prompt_are_built_from_one_dict(client):
    """The whole reason the prompt is generated rather than written: a
    brief asking for a different script than the form accepts would only
    show up after a round trip through another tool."""
    form = client.get("/api/authoring").json()
    prompt = client.get("/api/authoring/prompt", params={"topic": "x"}).json()

    assert str(form["beat_count"]) in prompt["prompt"]
    assert str(form["word_budget"]) in prompt["prompt"]
    assert str(form["word_min"]) in prompt["prompt"]
    assert str(form["word_max"]) in prompt["prompt"]


def test_the_prompt_route_carries_the_topic_back(client):
    body = client.get("/api/authoring/prompt",
                      params={"topic": "  Kuldhara  "}).json()
    assert body["topic"] == "Kuldhara"
    assert "Kuldhara" in body["prompt"]


def test_an_over_long_topic_is_cut_to_the_limit(client):
    limit = client.get("/api/authoring").json()["topic_max"]
    body = client.get("/api/authoring/prompt",
                      params={"topic": "z" * (limit + 50)}).json()
    assert len(body["topic"]) == limit


def test_importing_a_good_script_returns_beats_ready_for_the_form(client):
    count = client.get("/api/authoring").json()["beat_count"]
    payload = json.dumps({"topic": "Kuldhara", "beats": _beats(count)},
                         ensure_ascii=False)

    body = client.post("/api/authoring/import", content=payload.encode(),
                       headers={"Content-Type": "text/plain"}).json()

    assert body["ok"] is True
    assert body["problems"] == []
    assert len(body["beats"]) == count
    assert body["beats"][0]["voice_text"] == DEVA
    assert body["filled"], "fills must be reported, not silent"


def test_a_broken_paste_is_a_200_that_explains_itself(client):
    """Not a 422: malformed input is the expected case at this door, and
    FastAPI's own error would say nothing a writer can act on."""
    response = client.post("/api/authoring/import",
                           content=b'{"beats": [ }',
                           headers={"Content-Type": "text/plain"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["problems"]
    assert body["beats"] == []


def test_every_problem_is_reported_not_just_the_first(client):
    count = client.get("/api/authoring").json()["beat_count"]
    beats = _beats(count - 1)
    beats[0]["voice_text"] = "this is latin"
    del beats[1]["visual_prompt"]
    payload = json.dumps({"topic": "x", "beats": beats}, ensure_ascii=False)

    body = client.post("/api/authoring/import", content=payload.encode(),
                       headers={"Content-Type": "text/plain"}).json()

    assert len(body["problems"]) >= 3, body["problems"]


def test_a_paste_past_the_cap_is_refused(client):
    client.app.state.settings.upload_max_mb = 0.001
    response = client.post("/api/authoring/import",
                           content=b"x" * 40000,
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 413


def test_the_import_route_reaches_no_model(client, monkeypatch):
    """The manual path's guarantee, asserted at this door too: the user
    runs the other tool themselves, and nothing here calls out."""
    import engine.app as app_mod

    def explode(*a, **k):
        raise AssertionError("the import route built a client")

    monkeypatch.setattr(app_mod, "OmniRouteClient", explode)
    count = client.get("/api/authoring").json()["beat_count"]
    payload = json.dumps({"topic": "x", "beats": _beats(count)},
                         ensure_ascii=False)

    assert client.post("/api/authoring/import", content=payload.encode(),
                       headers={"Content-Type": "text/plain"}
                       ).json()["ok"] is True
    assert client.get("/api/authoring/prompt").status_code == 200

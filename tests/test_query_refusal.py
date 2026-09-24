"""A model that did not answer in JSON must not be read as if it had.

``_parse_json_response`` ended in a fallback that took the model's prose
line by line, stripped punctuation off each, and handed whatever was left
to Pexels as a search query. On the run this was found in, the model
replied conversationally and the board filled with footage found for:

    here s a 4 1
    that s a strong establishing
    timing check the current line
    here s your segment timed
    voiceover 3 0s

Every one of those is a real Pexels search that returns real video, so
nothing anywhere reported a failure -- the clips were simply unrelated to
the story, and the only clue was the query printed on the card.

The fallback was built to keep a beat from failing. It does not: a beat
whose agent raises is caught in ``generate_plan_clips`` and falls back to
the generated-still chain, which is a picture chosen for *this* beat. So
the fallback never bought a beat anything; it only traded a visibly
missing clip for an invisibly wrong one.

The two real recoveries stay. Fenced JSON is still unwrapped, and a
reply with clip objects loose in prose is still mined for them -- both
are cases where the model did answer in JSON.
"""

from __future__ import annotations

import json

from unittest.mock import patch

import pytest

from stock_agent import VisualQueryGenerator


@pytest.fixture()
def generator():
    with patch("stock_agent.OpenAI"):
        return VisualQueryGenerator(base_url="http://mock/v1",
                                    api_key="mock-key", model="mock-model")


PROSE = """Here's a 4:1 breakdown for your segment.

That's a strong establishing beat, so I'd open wide.
Timing check: the current line runs about 3.0s.
"""


def test_prose_is_refused_rather_than_mined_for_queries(generator):
    with pytest.raises(ValueError) as caught:
        generator._parse_json_response(PROSE, expected_count=3)

    assert "JSON" in str(caught.value)


def test_the_refusal_quotes_what_came_back(generator):
    """A refusal that does not show the reply cannot be acted on: the
    whole point is to see that the model chatted instead of answering."""
    with pytest.raises(ValueError) as caught:
        generator._parse_json_response(PROSE, expected_count=3)

    assert "Here's a 4:1" in str(caught.value)


def test_an_empty_reply_is_refused_too(generator):
    with pytest.raises(ValueError):
        generator._parse_json_response("   ", expected_count=2)


def test_fenced_json_still_parses(generator):
    """The recovery that works is kept."""
    raw = ("Sure!\n```json\n" + json.dumps({"clips": [
        {"clip_index": 1, "search_query": "empty wooden chair dark room",
         "shot_intent": "The chair alone"}]}) + "\n```\nHope that helps!")

    clips = generator._parse_json_response(raw, expected_count=1)

    assert clips[0]["search_query"] == "empty wooden chair dark room"


def test_clip_objects_loose_in_prose_are_still_mined(generator):
    """Also a real answer -- just one wrapped in chatter."""
    raw = ('Here you go: {"clip_index": 1, "search_query": "rain on window '
           'at night", "shot_intent": "Mood"} and that should do it.')

    clips = generator._parse_json_response(raw, expected_count=1)

    assert clips[0]["search_query"] == "rain on window at night"


def test_a_refused_beat_falls_back_to_a_still_instead_of_wrong_footage(
        tmp_path):
    """The behaviour the fallback was protecting, shown to be already
    covered: generate_plan_clips catches the raise per beat."""
    from engine.media import clips as clips_mod

    class Refuses:
        def match(self, **kwargs):
            raise ValueError("model did not answer in JSON")

    from tests.factories import make_plan
    plan = make_plan(plan_id="p1", beats=1)
    beat = plan.script.beats[0]

    with pytest.raises(ValueError):
        clips_mod.beat_clips(Refuses(), beat, tmp_path)

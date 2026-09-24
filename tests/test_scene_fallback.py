"""When the model will not write queries, use the scene the script wrote.

The query model is asked to turn a beat's scene description into three
to five word Pexels searches. When it answers with prose instead, there
are no queries -- and until now that meant the beat fell all the way
through to a generated still, because the only thing between the two was
a fallback that searched the model's own small talk.

That throws away something usable. ``beat.visual_prompt`` is already an
English shot description, written by the script agent for exactly this
purpose, and it reaches this function as ``script_segment``:

    "A content creator checking a YouTube Studio revenue dashboard on a
     laptop."

Stripped to its content words that is "content creator checking youtube
studio" -- a real query for real, relevant footage. Worse than what a
working model would write, far better than a still.

The same fallback covers the gateway being down, which is the case that
needs it most: no model at all, and still a usable description in hand.

It is deliberately visible rather than silent. Every query carries a
``shot_intent`` saying where it came from, so the review board shows
"derived from the beat's scene" on the card instead of quietly looking
like a normal result -- which is the exact failure the prose fallback
had.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from stock_agent import VisualQueryGenerator

SCENE = ("A close-up of a smartphone playing a YouTube video with an "
         "advertisement appearing on screen.")
PROSE = "Here's a 4:1 breakdown for your segment. Timing check: 3.0s."


@pytest.fixture()
def generator():
    with patch("stock_agent.OpenAI"):
        return VisualQueryGenerator(base_url="http://mock/v1",
                                    api_key="mock-key", model="mock-model")


def _answers(generator, content):
    choice = MagicMock()
    choice.message.content = content
    generator.client.chat.completions.create = MagicMock(
        return_value=MagicMock(choices=[choice]))


def test_prose_falls_back_to_the_scene_instead_of_nothing(generator):
    _answers(generator, PROSE)

    queries, count = generator.generate_queries(SCENE, duration_seconds=5.0)

    assert count == 2
    assert len(queries) == 2
    assert all(q.search_query for q in queries)


def test_the_fallback_query_is_the_scenes_content_words(generator):
    """Not the scene verbatim: "a close up of a" is what the first five
    words give, and it finds nothing."""
    _answers(generator, PROSE)

    queries, _ = generator.generate_queries(SCENE, duration_seconds=5.0)

    assert queries[0].search_query == "close up smartphone playing youtube"


def test_the_fallback_obeys_the_three_to_five_word_rule(generator):
    _answers(generator, PROSE)

    for scene in [SCENE, "A laptop.", "Rain."]:
        queries, _ = generator.generate_queries(scene, duration_seconds=3.0)
        assert 3 <= len(queries[0].search_query.split()) <= 5, scene


def test_every_query_says_it_came_from_the_scene(generator):
    """A fallback that looks like a normal result is how the old one hid
    for so long."""
    _answers(generator, PROSE)

    queries, _ = generator.generate_queries(SCENE, duration_seconds=5.0)

    assert all("scene" in q.shot_intent.lower() for q in queries)


def test_a_dead_gateway_falls_back_the_same_way(generator):
    """The case that needs it most: no model at all, and a perfectly
    good description still in hand."""
    from openai import APIConnectionError

    generator.client.chat.completions.create = MagicMock(
        side_effect=APIConnectionError(request=MagicMock()))

    queries, count = generator.generate_queries(SCENE, duration_seconds=5.0)

    assert len(queries) == count == 2
    assert queries[0].search_query == "close up smartphone playing youtube"


def test_a_scene_of_nothing_but_stopwords_still_yields_a_query(generator):
    """Degenerate, but it must not crash the beat."""
    _answers(generator, PROSE)

    queries, _ = generator.generate_queries("of the a an", duration_seconds=3.0)

    assert len(queries[0].search_query.split()) >= 3


def test_a_working_model_is_untouched(generator):
    """The fallback must not fire when there is a real answer."""
    import json
    _answers(generator, json.dumps({"clips": [
        {"clip_index": 1, "search_query": "empty wooden chair room",
         "shot_intent": "The chair, alone"},
        {"clip_index": 2, "search_query": "name carved into wood",
         "shot_intent": "Push in on the name"}]}))

    queries, _ = generator.generate_queries(SCENE, duration_seconds=5.0)

    assert queries[0].search_query == "empty wooden chair room"
    assert queries[0].shot_intent == "The chair, alone"

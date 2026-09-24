"""Checks on the prompt text files themselves -- what the model is told,
not what it does with it (that is `tests/test_sticker_vocabulary.py` and
the Step 7 measurement in the Task 7 report)."""

from __future__ import annotations

from pathlib import Path


def test_the_script_prompt_asks_for_concrete_sticker_terms():
    """The catalogue is named for objects, not ideas -- see
    test_the_catalogue_is_named_for_things_not_for_ideas. A prompt that
    does not say so gets abstract nouns and empty candidate lists."""
    text = Path("engine/prompts/script.txt").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "sticker" in lowered
    assert "caption_text" in lowered
    for word in ("concrete", "abstract"):
        assert word in lowered, f"the prompt never says {word}"
    # The example is what a model actually copies.
    assert '"terms"' in text and '"word"' in text

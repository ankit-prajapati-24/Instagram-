"""Choosing which voice reads the reel, from the board.

Piper ships one model on disk and the project had been treating that as
"the voice". There are three Hindi Piper models and two Hindi edge-tts
ones, and which one reads a mystery channel is a taste decision that
belongs with the person listening, not in an env var they have to find.

Discovery rather than a hardcoded list: a model someone drops into the
models directory should appear without a code change, and a list that can
go stale is a list that will.
"""

from __future__ import annotations

import pytest

from engine.config import Settings
from engine.media.voice import available_voices, split_voice_id


@pytest.fixture()
def models(tmp_path):
    for name in ("pratham", "priyamvada"):
        (tmp_path / f"{name}.onnx").write_bytes(b"not a real model")
        (tmp_path / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_every_piper_model_on_disk_is_offered(models):
    s = Settings()
    s.piper_models_dir = models
    ids = [v["id"] for v in available_voices(s, include_edge=False)]
    assert "piper:pratham" in ids
    assert "piper:priyamvada" in ids


def test_a_model_dropped_in_later_appears_without_a_code_change(models):
    s = Settings()
    s.piper_models_dir = models
    (models / "rohan.onnx").write_bytes(b"not a real model")
    ids = [v["id"] for v in available_voices(s, include_edge=False)]
    assert "piper:rohan" in ids


def test_a_missing_models_directory_is_not_an_error(tmp_path):
    """It used to assert the list came back empty. That was written before
    I had read `ensure_model`, which downloads a voice on first use -- so
    an empty directory means "nothing downloaded yet", not "nothing
    available", and returning [] there is what hid Piper from a fresh
    clone."""
    s = Settings()
    s.piper_models_dir = tmp_path / "nothing-here"
    found = available_voices(s, include_edge=False)
    assert found
    assert all(v["local"] is False for v in found)


def test_the_edge_voices_are_offered_but_marked_as_needing_the_network(
        models, monkeypatch):
    """Piper runs on this machine; edge-tts goes to Microsoft for every
    beat. Someone picking a voice should be able to see which is which."""
    from engine.media import voice as voice_mod

    s = Settings()
    s.piper_models_dir = models
    monkeypatch.setattr(voice_mod, "_edge_hindi_voices",
                        lambda: [{"ShortName": "hi-IN-SwaraNeural",
                                  "Gender": "Female", "Locale": "hi-IN"}])

    found = available_voices(s)
    piper = next(v for v in found if v["engine"] == "piper")
    edge = next(v for v in found if v["engine"] == "edge")
    assert piper["local"] is True
    assert edge["local"] is False
    assert edge["id"] == "edge:hi-IN-SwaraNeural"


def test_edge_being_unreachable_does_not_hide_the_local_voices(models,
                                                               monkeypatch):
    """The list is what someone chooses from. Losing the network should
    cost the edge voices, not the whole control."""
    from engine.media import voice as voice_mod

    s = Settings()
    s.piper_models_dir = models

    def boom():
        raise OSError("no route to host")

    monkeypatch.setattr(voice_mod, "_edge_hindi_voices", boom)
    found = available_voices(s)
    assert found, "an unreachable edge took the local voices with it"
    assert all(v["engine"] == "piper" for v in found)


def test_a_voice_id_names_its_engine_and_its_voice():
    assert split_voice_id("piper:priyamvada") == ("piper", "priyamvada")
    assert split_voice_id("edge:hi-IN-SwaraNeural") == (
        "edge", "hi-IN-SwaraNeural")


def test_a_voice_id_that_is_not_one_is_refused():
    for bad in ("", "piper", "elsewhere:x", "piper:", ":x",
                "piper:../../etc/passwd", "piper:a b"):
        with pytest.raises(ValueError):
            split_voice_id(bad)


def test_a_fresh_clone_is_still_offered_the_piper_voices(tmp_path):
    """models/ is gitignored, so a clone has no .onnx at all -- and
    `ensure_model` downloads one on first use anyway.

    Listing only what is on disk meant a fresh checkout showed no Piper
    voices in the control while Piper worked perfectly, which reads as
    "this install cannot do Piper".
    """
    s = Settings()
    s.piper_models_dir = tmp_path / "empty"
    found = available_voices(s, include_edge=False)
    assert found, "a fresh clone was offered no Piper voice at all"
    assert {"piper:pratham", "piper:priyamvada"} <= {v["id"] for v in found}
    assert all(v["local"] is False for v in found), \
        "nothing is downloaded yet, so nothing is local"


def test_a_downloaded_voice_is_marked_as_already_here(models):
    s = Settings()
    s.piper_models_dir = models
    by_id = {v["id"]: v for v in available_voices(s, include_edge=False)}
    assert by_id["piper:pratham"]["local"] is True
    # rohan is fetchable but not downloaded in this fixture.
    assert by_id["piper:rohan"]["local"] is False


def test_a_model_not_in_the_known_list_is_still_discovered(models):
    """Someone who drops a voice in by hand should see it, whatever it is
    called -- discovery is what keeps the list from going stale."""
    (models / "someone-elses.onnx").write_bytes(b"x")
    ids = {v["id"] for v in available_voices(models_settings(models),
                                             include_edge=False)}
    assert "piper:someone-elses" in ids


def models_settings(models):
    s = Settings()
    s.piper_models_dir = models
    return s

import io

import pytest
from PIL import Image

from engine.media.images import (STYLE_SUFFIX, cover_fit, generate_beat_image,
                                 keyless_url, placeholder_image)
from tests.factories import make_beat


def _png(width=580, height=1015, colour=(40, 80, 90)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, "PNG")
    return buffer.getvalue()


# --- placeholder ------------------------------------------------------------

def test_placeholder_is_vertical_1080x1920(tmp_path):
    path = placeholder_image(tmp_path / "a.png", "", seed=1)
    assert Image.open(path).size == (1080, 1920)


def test_placeholder_is_deterministic_per_seed(tmp_path):
    a = placeholder_image(tmp_path / "a.png", "", seed=7)
    b = placeholder_image(tmp_path / "b.png", "", seed=7)
    assert open(a, "rb").read() == open(b, "rb").read()


def test_placeholder_differs_between_seeds(tmp_path):
    a = placeholder_image(tmp_path / "a.png", "", seed=1)
    b = placeholder_image(tmp_path / "b.png", "", seed=2)
    assert open(a, "rb").read() != open(b, "rb").read()


def test_style_suffix_pins_a_consistent_look():
    lowered = STYLE_SUFFIX.lower()
    assert "cinematic" in lowered
    assert "vertical" in lowered
    assert "no text" in lowered


# --- cover fit and watermark crop -------------------------------------------

def test_cover_fit_always_returns_the_frame_size(tmp_path):
    for size in [(580, 1015), (1024, 1792), (1920, 1080), (512, 512)]:
        out = cover_fit(_png(*size), tmp_path / f"o{size[0]}.png")
        assert Image.open(out).size == (1080, 1920)


def test_cover_fit_crops_rather_than_stretching(tmp_path):
    """A wide source must be cropped to vertical, not squashed into it."""
    source = Image.new("RGB", (2000, 500), (10, 10, 10))
    # a red stripe down the middle stays a stripe if we crop, and becomes a
    # wide band if we stretch
    for x in range(990, 1010):
        for y in range(500):
            source.putpixel((x, y), (255, 0, 0))
    buffer = io.BytesIO()
    source.save(buffer, "PNG")

    out = Image.open(cover_fit(buffer.getvalue(), tmp_path / "o.png"))
    row = [out.getpixel((x, 960)) for x in range(1080)]
    red = [i for i, px in enumerate(row) if px[0] > 150]
    # 20px of 2000 scaled to cover 1920 height => ~4% of width, not 100%
    assert 0 < len(red) < 200


def test_bottom_crop_removes_a_corner_watermark(tmp_path):
    """The keyless source stamps its name bottom-right despite nologo."""
    source = Image.new("RGB", (580, 1015), (20, 40, 50))
    for x in range(400, 580):
        for y in range(975, 1015):
            source.putpixel((x, y), (255, 255, 255))
    buffer = io.BytesIO()
    source.save(buffer, "PNG")

    out = Image.open(cover_fit(buffer.getvalue(), tmp_path / "o.png",
                               crop_bottom=0.07))
    bottom = [out.getpixel((x, 1919)) for x in range(600, 1080, 20)]
    assert all(px[0] < 200 for px in bottom), "watermark survived the crop"


def test_no_crop_keeps_the_full_frame(tmp_path):
    out = cover_fit(_png(1080, 1920), tmp_path / "o.png", crop_bottom=0.0)
    assert Image.open(out).size == (1080, 1920)


# --- keyless url ------------------------------------------------------------

def test_keyless_url_encodes_the_prompt_and_pins_a_seed():
    url = keyless_url("a dark lake, misty", seed=4)
    assert "a%20dark%20lake" in url
    assert "seed=4" in url
    assert "width=" in url and "height=" in url


def test_keyless_url_is_stable_for_the_same_seed():
    assert keyless_url("x", seed=1) == keyless_url("x", seed=1)
    assert keyless_url("x", seed=1) != keyless_url("x", seed=2)


# --- the three-tier chain ---------------------------------------------------

class GatewayOK:
    calls: list = []

    def image(self, prompt, *, model=None, size="1024x1792", n=1):
        GatewayOK.calls.append(prompt)
        return [_png(1024, 1792)]


class GatewayDead:
    def image(self, prompt, *, model=None, size="1024x1792", n=1):
        raise RuntimeError("no image provider configured")


def test_tier_one_is_used_when_the_gateway_works(tmp_path):
    GatewayOK.calls = []
    path, provider = generate_beat_image(
        GatewayOK(), make_beat(0), tmp_path / "a.png", seed=0)
    assert provider == "omniroute"
    assert Image.open(path).size == (1080, 1920)
    # the shared style suffix must reach the model
    assert STYLE_SUFFIX.strip(" ,") in GatewayOK.calls[0]


def test_tier_two_is_used_when_the_gateway_is_dead(tmp_path):
    def keyless(prompt, seed, timeout):
        return _png(580, 1015)

    path, provider = generate_beat_image(
        GatewayDead(), make_beat(0), tmp_path / "a.png", seed=0,
        keyless_fetch=keyless)
    assert provider == "keyless"
    assert Image.open(path).size == (1080, 1920)


def test_tier_three_is_used_when_both_fail(tmp_path):
    def keyless(prompt, seed, timeout):
        raise RuntimeError("service down")

    path, provider = generate_beat_image(
        GatewayDead(), make_beat(0), tmp_path / "a.png", seed=0,
        keyless_fetch=keyless)
    assert provider == "placeholder"
    assert Image.open(path).size == (1080, 1920)


def test_keyless_can_be_turned_off(tmp_path):
    called = {"n": 0}

    def keyless(prompt, seed, timeout):
        called["n"] += 1
        return _png()

    path, provider = generate_beat_image(
        GatewayDead(), make_beat(0), tmp_path / "a.png", seed=0,
        keyless_fetch=keyless, use_keyless=False)
    assert provider == "placeholder"
    assert called["n"] == 0


def test_a_junk_payload_falls_through_instead_of_being_saved(tmp_path):
    """A 200 carrying an HTML error page must not become a beat's visual."""
    def keyless(prompt, seed, timeout):
        return b"<html>rate limited</html>"

    _, provider = generate_beat_image(
        GatewayDead(), make_beat(0), tmp_path / "a.png", seed=0,
        keyless_fetch=keyless)
    assert provider == "placeholder"


def test_chain_never_raises_so_a_render_is_never_lost(tmp_path):
    class Exploding:
        def image(self, *a, **k):
            raise KeyboardInterrupt  # the nastiest thing we can throw

    def keyless(prompt, seed, timeout):
        raise MemoryError

    with pytest.raises(KeyboardInterrupt):
        # KeyboardInterrupt must still propagate; it is not a provider fault
        generate_beat_image(Exploding(), make_beat(0), tmp_path / "a.png",
                            seed=0, keyless_fetch=keyless)


# --- payload sniffing -------------------------------------------------------
# Regression: the check compared payload[:4] against a tuple that included the
# two-byte JPEG marker, so every JPEG was judged "not an image" and the whole
# chain fell through to the placeholder while the provider was working fine.

@pytest.mark.parametrize("magic,label", [
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "jpeg jfif"),
    (b"\xff\xd8\xff\xe1\x00\x10Exif", "jpeg exif"),
    (b"\xff\xd8\xff\xdb\x00C\x00", "jpeg raw"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF\x00\x00\x00\x00WEBP", "webp"),
    (b"GIF89a", "gif"),
])
def test_real_image_payloads_are_accepted(magic, label):
    from engine.media.images import _looks_like_an_image
    assert _looks_like_an_image(magic + b"rest of the file"), label


@pytest.mark.parametrize("payload", [
    b"", b"<html>rate limited</html>", b"{\"error\": \"busy\"}", b"not image",
])
def test_non_image_payloads_are_rejected(payload):
    from engine.media.images import _looks_like_an_image
    assert not _looks_like_an_image(payload)


def test_a_jpeg_from_tier_two_is_accepted_end_to_end(tmp_path):
    """The exact shape the keyless endpoint returns: a JPEG, not a PNG."""
    buffer = io.BytesIO()
    Image.new("RGB", (580, 1015), (30, 60, 70)).save(buffer, "JPEG")

    def keyless(prompt, seed, timeout):
        return buffer.getvalue()

    path, provider = generate_beat_image(
        GatewayDead(), make_beat(0), tmp_path / "a.png", seed=0,
        keyless_fetch=keyless)
    assert provider == "keyless"
    assert Image.open(path).size == (1080, 1920)


# --- tier 2 retry -----------------------------------------------------------
# Measured: one attempt per beat landed 3 of 10 images; with retries it landed
# 5 of 5. The endpoint is free and under load, so it 500s or times out and
# then succeeds moments later.

def test_keyless_retries_then_succeeds(monkeypatch):
    import httpx as real_httpx
    from engine.media import images as mod

    calls = {"n": 0}
    jpeg = b"\xff\xd8\xff\xe0" + b"body"

    class Response:
        def __init__(self, status, content):
            self.status_code = status
            self.content = content

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return Response(500, b"")
        return Response(200, jpeg)

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod.fetch_keyless("a lake", 0) == jpeg
    assert calls["n"] == 3


def test_keyless_gives_up_after_the_attempt_limit(monkeypatch):
    from engine.media import images as mod
    calls = {"n": 0}

    class Response:
        status_code = 500
        content = b""

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return Response()

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        mod.fetch_keyless("a lake", 0)
    assert calls["n"] == 3


def test_keyless_retries_on_a_network_error_too(monkeypatch):
    import httpx as real_httpx
    from engine.media import images as mod
    calls = {"n": 0}
    jpeg = b"\xff\xd8\xff\xe0body"

    class Response:
        status_code = 200
        content = jpeg

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise real_httpx.ReadTimeout("slow")
        return Response()

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod.fetch_keyless("a lake", 0) == jpeg
    assert calls["n"] == 2


# --- the word budget has to match the voice that speaks it -----------------
# The first fully real run: 152 Devanagari words of script came out as 66.5s
# of Piper audio -- 2.29 words/sec, against a configured 3.03. The budget is
# target_seconds * words_per_second, so a rate set 32% high asks for a third
# more words than the voice can say inside the window, and the over-length is
# only discovered after the render.
#
# scripts/measure_speech_rate.py reports ~2.94 w/s, but its five sample lines
# carry no numerals, no dates and no acronyms. "1965" is one word and eleven
# syllables; real scripts are full of them, and the per-beat rates on the
# failing run ran from 1.17 to 2.83 w/s. The production number is the honest
# one.
MEASURED_WORDS_PER_SEC = 2.29


def test_the_word_budget_fits_the_window_at_the_measured_speech_rate():
    from tests.factories import shipped_settings

    settings = shipped_settings()
    budget = settings.target_seconds * settings.words_per_second
    spoken = budget / MEASURED_WORDS_PER_SEC
    assert settings.duration_min <= spoken <= settings.duration_max, (
        f"a {budget:.0f}-word budget speaks for {spoken:.1f}s at the "
        f"measured {MEASURED_WORDS_PER_SEC} w/s")


def test_the_budget_holds_at_both_ends_of_the_drift_tolerance():
    """run_script accepts +/-15% around the budget, so both ends must land
    inside the duration window too -- otherwise a script the agent passes is
    a video QC rejects."""
    from tests.factories import shipped_settings

    settings = shipped_settings()
    budget = settings.target_seconds * settings.words_per_second
    for drift in (-0.15, 0.15):
        spoken = budget * (1 + drift) / MEASURED_WORDS_PER_SEC
        assert settings.duration_min <= spoken <= settings.duration_max, (
            f"{drift:+.0%} off budget speaks for {spoken:.1f}s")


# --- the sample script is a fixture the offline harness depends on ---------
# It drifted out of range once: written for edge-tts's slower pace, it made a
# 34.7s video once Piper became the engine, and verify_e2e.py failed on the
# duration gate every run.

def test_sample_script_hits_the_word_budget():
    from engine.fake_client import SAMPLE_BEATS
    from tests.factories import shipped_settings

    settings = shipped_settings()
    budget = settings.target_seconds * settings.words_per_second
    words = sum(len(beat[1].split()) for beat in SAMPLE_BEATS)

    # Within 15% of the budget, so the rendered video lands inside 38-52s.
    assert abs(words - budget) / budget < 0.15, (
        f"sample script is {words} words against a {budget:.0f} budget; "
        f"that renders at ~{words / settings.words_per_second:.0f}s")


def test_sample_script_predicts_a_duration_inside_the_qc_window():
    from engine.fake_client import SAMPLE_BEATS
    from tests.factories import shipped_settings

    settings = shipped_settings()
    words = sum(len(beat[1].split()) for beat in SAMPLE_BEATS)
    predicted = words / settings.words_per_second
    assert settings.duration_min <= predicted <= settings.duration_max


def test_sample_script_varies_its_sentence_lengths():
    """An even rhythm is the clearest AI tell, and QC warns on it. The
    sample is the only worked example of a good script in the codebase, so
    it should not model the thing the prompt forbids."""
    import statistics
    from engine.fake_client import SAMPLE_BEATS

    lengths = [len(beat[1].split()) for beat in SAMPLE_BEATS]
    assert statistics.pstdev(lengths) > 4.0, lengths
    assert min(lengths) <= 6, "no short punch beats"
    assert max(lengths) >= 15, "no long beats"


def test_sample_script_beat_count_is_in_the_contract_range():
    from engine.fake_client import SAMPLE_BEATS
    assert 9 <= len(SAMPLE_BEATS) <= 13

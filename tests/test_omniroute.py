import json

import httpx
import pytest

from engine.omniroute import OmniRouteClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    return OmniRouteClient(base="http://localhost:20128/v1", key="k",
                           transport=transport)


def test_chat_returns_text_and_records_cost():
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
        }, headers={
            "X-OmniRoute-Response-Cost": "0.0000123400",
            "X-OmniRoute-Provider": "groq",
            "X-OmniRoute-Model": "llama-3.3-70b",
            "X-OmniRoute-Fallback-Attempts": "2",
            "X-OmniRoute-Latency-Ms": "812",
        })

    c = _client(handler)
    result = c.chat([{"role": "user", "content": "hi"}])
    assert result.text == "hello"
    assert result.cost.usd == pytest.approx(0.00001234)
    assert result.cost.provider == "groq"
    assert result.cost.fallback_attempts == 2
    assert c.total_usd == pytest.approx(0.00001234)


def test_chat_extracts_json_from_fenced_block():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "```json\n{\"a\": 1}\n```"}}]})

    c = _client(handler)
    assert c.chat([], want_json=True).data == {"a": 1}


def test_chat_extracts_json_with_surrounding_prose():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "Sure! {\"a\": [1, 2]} Hope that helps."}}]})

    assert _client(handler).chat([], want_json=True).data == {"a": [1, 2]}


def test_missing_cost_headers_default_to_zero():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}}]})

    c = _client(handler)
    r = c.chat([])
    assert r.cost.usd == 0.0
    assert r.cost.fallback_attempts == 0


def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]})

    c = _client(handler)
    c.backoff_base = 0.0
    assert c.chat([]).text == "ok"
    assert calls["n"] == 3


def test_image_decodes_b64_payload():
    import base64
    png = b"\x89PNG\r\n\x1a\nFAKE"

    def handler(request):
        assert request.url.path == "/v1/images/generations"
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(png).decode()}]})

    assert _client(handler).image("a dark lake")[0] == png


def test_embed_returns_vectors_in_order():
    def handler(request):
        return httpx.Response(200, json={
            "data": [{"embedding": [0.1, 0.2], "index": 0},
                     {"embedding": [0.3, 0.4], "index": 1}]})

    assert _client(handler).embed(["a", "b"])[0] == [0.1, 0.2]


def test_moderate_reports_flagged_and_categories():
    def handler(request):
        return httpx.Response(200, json={
            "results": [{"flagged": True,
                         "categories": {"violence": True, "hate": False}}]})

    r = _client(handler).moderate("something")
    assert r.flagged is True
    assert "violence" in r.flags


# --- the "gateway is up but has no provider" family -------------------------
# All verified against a real clean OmniRoute install on 2026-09-17, where
# `omniroute nodes list` reported {"nodes": []}.

def test_no_provider_503_is_not_retried():
    from engine.omniroute import NoProviderError
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={
            "error": {"message": "Maximum combo retry limit reached",
                      "code": "service_unavailable"},
            "diagnostics": {"poolSize": 54, "attempted": 29}})

    c = _client(handler)
    c.backoff_base = 0.0
    with pytest.raises(NoProviderError):
        c.chat([])
    assert calls["n"] == 1, "a missing provider key must not burn retries"


def test_missing_moderation_credentials_reports_unavailable(self=None):
    """Moderation routes to its own provider, so it can be missing while
    chat works. That is a setup gap, not a content verdict — it must not
    raise (which would stop every plan) and must not read as "clean"."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {
            "message": "No credentials for provider: openai"}})

    c = _client(handler)
    c.backoff_base = 0.0
    result = c.moderate("anything")
    assert calls["n"] == 1, "a credential gap must not burn retries"
    assert result.checked is False
    assert result.flagged is False
    assert "no upstream provider" in result.unavailable.lower()


def test_moderation_reports_a_real_flag_as_checked():
    def handler(request):
        return httpx.Response(200, json={"results": [
            {"flagged": True, "categories": {"violence": True}}]})

    result = _client(handler).moderate("something")
    assert result.checked is True
    assert result.flagged is True
    assert result.flags == ["violence"]


def test_empty_200_body_is_reported_as_no_provider():
    """A provider-less gateway answers small requests 200 with no body."""
    from engine.omniroute import NoProviderError

    def handler(request):
        return httpx.Response(200, content=b"")

    with pytest.raises(NoProviderError, match="empty body"):
        _client(handler).chat([])


def test_chat_always_sends_a_model():
    """OmniRoute rejects a request with no model field: "Missing model"."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}}]})

    _client(handler).chat([])
    assert seen["model"] == "auto/best-chat"


def test_embed_and_image_also_send_a_model():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content).get("model"))
        if "embeddings" in str(request.url):
            return httpx.Response(200, json={
                "data": [{"embedding": [0.1], "index": 0}]})
        return httpx.Response(200, json={
            "data": [{"b64_json": "aGk="}]})

    c = _client(handler)
    c.embed(["a"])
    c.image("a lake")
    assert all(model for model in seen)


def test_health_rejects_a_200_with_an_empty_completion():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "auto/x"}]})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "   "}}]})

    state = _client(handler).health()
    assert state["state"] == "no_provider"
    assert "empty completion" in state["detail"]


def test_health_reports_ready_only_on_real_content():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "auto/x"}]})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}]},
            headers={"X-OmniRoute-Provider": "groq"})

    state = _client(handler).health()
    assert state["state"] == "ready"
    assert "groq" in state["detail"]
    assert state["models"] == 1


def test_health_reports_down_when_nothing_answers():
    def handler(request):
        raise httpx.ConnectError("refused")

    assert _client(handler).health()["state"] == "down"


# --- the gateway answers SSE even when asked not to -------------------------
# Measured against a live provider: a plain request came back as
# "data: {...}" lines, and response.json() fails on that.

def test_chat_always_asks_for_a_non_streaming_response():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]})

    _client(handler).chat([])
    assert seen["stream"] is False


def test_an_sse_body_is_reassembled_instead_of_crashing():
    body = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"Hello "}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"world"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request):
        return httpx.Response(200, content=body.encode(),
                              headers={"content-type": "text/event-stream"})

    assert _client(handler).chat([]).text == "Hello world"


def test_an_sse_body_still_yields_json_for_want_json():
    body = ('data: {"choices":[{"index":0,"delta":{"content":"{\\"a\\": 1}"}}]}'
            '\n\ndata: [DONE]\n\n')

    def handler(request):
        return httpx.Response(200, content=body.encode())

    assert _client(handler).chat([], want_json=True).data == {"a": 1}


def test_sse_message_shaped_chunks_are_handled_too():
    """Some providers send full messages per chunk rather than deltas."""
    body = ('data: {"choices":[{"index":0,"message":{"content":"done"}}]}\n\n'
            'data: [DONE]\n\n')

    def handler(request):
        return httpx.Response(200, content=body.encode())

    assert _client(handler).chat([]).text == "done"


# --- what the server says the bytes are, versus what they are --------------
#
# Found on a real run, 2026-09-24: a whole plan came back from the `cw`
# (claude-web) provider with every Devanagari character replaced by
# mojibake -- "1815 में 800 लोग थे" stored as
# "1815 à¤®à¥‡à¤‚ 800 à¤²à¥‹à¤— à¤¥à¥‡". The corruption was already in the
# database, so it happened on the way in, and it was lossy: `्` (U+094D,
# UTF-8 E0 A5 8D) came back as "à¥�" because 0x8D is an undefined slot
# in cp1252. Round-tripping the original through
# `.encode("utf-8").decode("cp1252", errors="replace")` reproduces the
# stored string exactly, character for character.
#
# httpx only decodes as cp1252 when the response's Content-Type says so --
# with no charset it defaults to utf-8. So the gateway declared a charset
# its bytes were not in.
#
# Both payloads this client reads with `.text` are UTF-8 by specification:
# RFC 8259 requires JSON exchanged between systems to be UTF-8, and the SSE
# spec requires event streams to be UTF-8 as well. A server claiming
# otherwise about such a body is wrong, and honouring the claim turns a
# wrong header into destroyed text. So the declared charset is ignored for
# these two.
#
# `.json()` was never affected -- httpx hands the raw bytes to json.loads,
# which assumes UTF-8 -- which is why the model listing looked fine
# throughout and only the completions were wrecked.

DEVANAGARI = "1815 में 800 लोग थे, 1890 में सिर्फ 37"


def _mislabelled(body: str, content_type: str) -> httpx.Response:
    """A response whose bytes are UTF-8 and whose header disagrees."""
    return httpx.Response(200, content=body.encode("utf-8"),
                          headers={"content-type": content_type})


@pytest.mark.parametrize("content_type", [
    "application/json; charset=windows-1252",
    "application/json; charset=iso-8859-1",
    "application/json; charset=us-ascii",
])
def test_a_json_body_is_read_as_utf8_whatever_the_header_claims(content_type):
    def handler(request):
        return _mislabelled(
            json.dumps({"choices": [{"message": {"content": DEVANAGARI}}]},
                       ensure_ascii=False),
            content_type)

    assert _client(handler).chat([{"role": "user", "content": "x"}]).text \
        == DEVANAGARI


@pytest.mark.parametrize("content_type", [
    "text/event-stream; charset=windows-1252",
    "text/event-stream; charset=iso-8859-1",
])
def test_an_sse_body_is_read_as_utf8_whatever_the_header_claims(content_type):
    def handler(request):
        chunk = json.dumps({"choices": [{"delta": {"content": DEVANAGARI}}]},
                           ensure_ascii=False)
        return _mislabelled(f"data: {chunk}\n\ndata: [DONE]\n\n",
                            content_type)

    assert _client(handler).chat([{"role": "user", "content": "x"}]).text \
        == DEVANAGARI


def test_the_exact_corruption_seen_on_the_real_run_no_longer_happens():
    """Pinned to the real failure, not an invented one.

    The stored text is what `cp1252` with `errors="replace"` does to this
    signature, including the U+FFFD where the virama's third byte fell in
    an undefined slot. If the client ever honours a declared charset again,
    this is the string it will produce.
    """
    wrecked = DEVANAGARI.encode("utf-8").decode("cp1252", errors="replace")
    assert "�" in wrecked, "the fixture must reproduce the lossy decode"

    def handler(request):
        return _mislabelled(
            json.dumps({"choices": [{"message": {"content": DEVANAGARI}}]},
                       ensure_ascii=False),
            "application/json; charset=cp1252")

    got = _client(handler).chat([{"role": "user", "content": "x"}]).text
    assert got == DEVANAGARI
    assert got != wrecked
    assert "�" not in got


def test_a_correctly_labelled_utf8_body_still_works():
    def handler(request):
        return _mislabelled(
            json.dumps({"choices": [{"message": {"content": DEVANAGARI}}]},
                       ensure_ascii=False),
            "application/json; charset=utf-8")

    assert _client(handler).chat([{"role": "user", "content": "x"}]).text \
        == DEVANAGARI


# --- text the gateway already wrecked before we saw it ---------------------
#
# Measured on the wire, 2026-09-24, provider `cw`. Asked for "में", the
# gateway returned the bytes
#
#     c3 a0  c2 a4  c2 ae   ...
#
# which is *valid UTF-8* — encoding the string "à¤®", which is itself what
# `म` (e0 a4 ae) looks like read as cp1252. So the corruption happened
# inside the gateway: it read Claude's UTF-8 as cp1252 and re-encoded the
# result. Our client decodes correctly and gets mojibake, because mojibake
# is what was sent.
#
# It cannot be repaired here. Reversing the round trip recovers most
# characters but not all: cp1252 leaves 0x81, 0x8d, 0x8f, 0x90 and 0x9d
# undefined, and `्` (U+094D) ends in 0x8d — so every conjunct loses its
# halant. "सिर्फ" comes back as "सिर�?फ". A script repaired that way
# would look mended and be full of holes, which is worse than one that
# never arrived.
#
# So this is detected and refused, loudly, before anything is stored.

from engine.omniroute import NoProviderError, looks_double_encoded


def test_double_encoded_devanagari_is_recognised():
    assert looks_double_encoded("1815 à¤®à¥‡à¤‚ 800 à¤²à¥‹à¤—")


def test_double_encoded_latin_one_is_recognised():
    """The same wreck hits accented Latin: this run produced
    "Julie von GÃ¼ldenstubbe"."""
    assert looks_double_encoded("Julie von GÃ¼ldenstubbe")


def test_the_lossy_case_is_recognised_too():
    """Where cp1252 had no mapping the character is already gone, so the
    round trip cannot be run — but the signature is still there."""
    wrecked = "सिर्फ".encode("utf-8").decode("cp1252", errors="replace")
    assert "�" in wrecked
    assert looks_double_encoded(wrecked)


def test_clean_devanagari_is_not_flagged():
    assert not looks_double_encoded("1815 में 800 लोग थे, सिर्फ ज्ञान")


def test_clean_english_is_not_flagged():
    assert not looks_double_encoded(
        "Jaisalmer se 18 kilometre door, ek gaon jo kabhi nahi basa.")


def test_real_accented_text_is_not_flagged():
    """A genuine umlaut or accent must not read as mojibake — those bytes
    are not valid UTF-8 on their own, which is what separates the two."""
    for text in ("Julie von Güldenstubbe", "café", "Ångström", "naïve"):
        assert not looks_double_encoded(text), text


def test_empty_and_ascii_are_not_flagged():
    for text in ("", "   ", "plain ascii only"):
        assert not looks_double_encoded(text)


def test_a_wrecked_completion_is_refused_instead_of_returned():
    """Refused at the door. Left to run, this becomes a stored plan, a
    voice track reading gibberish, and twelve minutes of render."""
    wrecked = "1815 में 800 लोग".encode("utf-8").decode("cp1252",
                                                        errors="replace")

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": wrecked}}]})

    with pytest.raises(NoProviderError) as caught:
        _client(handler).chat([{"role": "user", "content": "x"}])

    message = str(caught.value).lower()
    assert "mojibake" in message or "double" in message or "utf-8" in message


def test_a_clean_completion_still_comes_back():
    clean = "1815 में 800 लोग थे"

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": clean}}]})

    assert _client(handler).chat([{"role": "user", "content": "x"}]).text \
        == clean

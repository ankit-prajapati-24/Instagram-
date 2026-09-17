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


def test_missing_openai_credentials_is_not_retried():
    from engine.omniroute import NoProviderError
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {
            "message": "No credentials for provider: openai"}})

    c = _client(handler)
    c.backoff_base = 0.0
    with pytest.raises(NoProviderError):
        c.moderate("anything")
    assert calls["n"] == 1


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

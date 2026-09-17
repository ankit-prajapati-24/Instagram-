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

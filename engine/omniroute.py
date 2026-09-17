"""OmniRoute HTTP client.

One gateway, one base URL, one key. Every LLM / image / embedding / moderation
call in this project goes through here so that provider fallback, free-tier
stacking and cost telemetry all come for free.

The client captures the ``X-OmniRoute-*`` response headers on every call, which
is how per-video true cost lands in the ``costs`` table without separate
accounting.
"""

from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OmniRouteError(RuntimeError):
    """Raised when a call fails after every retry."""

    def __init__(self, message: str, status: int | None = None,
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class NoProviderError(OmniRouteError):
    """The gateway is up but could not route to any upstream provider.

    Verified against a clean install: OmniRoute answers /v1/models with ~99
    ``auto/*`` aliases while ``nodes list`` reports ``{"nodes": []}``, so every
    chat call comes back "Maximum combo retry limit reached". That is a setup
    gap, not a bug, and it must never be retried — it needs a provider key.
    """


# Gateway defaults. OmniRoute rejects a request with no model field at all
# ("Missing model"), so a model is always sent.
DEFAULT_CHAT_MODEL = "auto/best-chat"
DEFAULT_EMBED_MODEL = "auto/best-embedding"
DEFAULT_IMAGE_MODEL = "auto/best-image"
DEFAULT_MODERATION_MODEL = "omni-moderation-latest"


@dataclass
class CostRecord:
    usd: float = 0.0
    provider: str = ""
    model: str = ""
    fallback_attempts: int = 0
    latency_ms: int = 0
    cache_hit: bool = False

    @classmethod
    def from_headers(cls, headers: Any) -> "CostRecord":
        def num(name: str, cast, default):
            raw = headers.get(name)
            if raw in (None, ""):
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            usd=num("X-OmniRoute-Response-Cost", float, 0.0),
            provider=headers.get("X-OmniRoute-Provider", "") or "",
            model=headers.get("X-OmniRoute-Model", "") or "",
            fallback_attempts=num("X-OmniRoute-Fallback-Attempts", int, 0),
            latency_ms=num("X-OmniRoute-Latency-Ms", int, 0),
            cache_hit=str(headers.get("X-OmniRoute-Cache-Hit", "")).lower()
            == "true",
        )


@dataclass
class ChatResult:
    text: str
    cost: CostRecord
    data: Any = None
    raw: dict = field(default_factory=dict)


@dataclass
class ModerationResult:
    flagged: bool
    flags: list[str]
    cost: CostRecord


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Models wrap JSON in ``` fences, prepend "Sure!", or append a closing
    remark. Rather than fight that with prompt engineering alone, slice from
    the first brace/bracket to its matching last one.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise OmniRouteError(f"no JSON found in response: {text[:200]!r}")


class OmniRouteClient:
    def __init__(self, base: str, key: str, *, timeout: float = 180.0,
                 transport: httpx.BaseTransport | None = None,
                 max_attempts: int = 5, backoff_base: float = 0.8,
                 chat_model: str = DEFAULT_CHAT_MODEL,
                 embed_model: str = DEFAULT_EMBED_MODEL,
                 image_model: str = DEFAULT_IMAGE_MODEL):
        self.base = base.rstrip("/")
        self.key = key
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.default_chat_model = chat_model
        self.default_embed_model = embed_model
        self.default_image_model = image_model
        self.calls: list[CostRecord] = []
        self.total_usd = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OmniRouteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _record(self, headers: Any) -> CostRecord:
        cost = CostRecord.from_headers(headers)
        self.calls.append(cost)
        self.total_usd += cost.usd
        return cost

    @staticmethod
    def _is_no_provider(body: str) -> bool:
        needles = ("maximum combo retry limit", "no credentials for provider",
                   "no matching combo")
        low = body.lower()
        return any(n in low for n in needles)

    def _post(self, path: str, payload: dict) -> httpx.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last = exc
            else:
                body = response.text[:600]
                # Never burn retries on a missing provider key.
                if response.status_code >= 400 and self._is_no_provider(body):
                    raise NoProviderError(
                        "OmniRoute is running but no upstream provider is "
                        "configured. Add one provider key, then retry "
                        "(see docs/omniroute-setup.md).",
                        status=response.status_code, body=body)

                if response.status_code not in RETRY_STATUS:
                    if response.status_code >= 400:
                        raise OmniRouteError(
                            f"{path} failed with {response.status_code}",
                            status=response.status_code, body=body)
                    return response
                last = OmniRouteError(f"{path} got {response.status_code}",
                                      status=response.status_code, body=body)

            if attempt < self.max_attempts - 1 and self.backoff_base:
                sleep = self.backoff_base * (2 ** attempt)
                time.sleep(sleep + random.uniform(0, sleep * 0.25))

        raise OmniRouteError(f"{path} failed after {self.max_attempts} "
                             f"attempts: {last}")

    # -- endpoints --------------------------------------------------------
    def chat(self, messages: list[dict], *, model: str | None = None,
             want_json: bool = False, temperature: float = 0.85,
             max_tokens: int = 4096) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model or self.default_chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if want_json:
            payload["response_format"] = {"type": "json_object"}

        response = self._post("chat/completions", payload)
        cost = self._record(response.headers)

        # An empty 200 is what a provider-less gateway returns for small
        # requests. Name it here, or it surfaces later as a confusing
        # "no JSON found in response: ''" from the agent layer.
        if not response.text.strip():
            raise NoProviderError(
                "gateway returned 200 with an empty body — no upstream "
                "provider completed the request",
                status=response.status_code)

        body = response.json()
        text = (body.get("choices") or [{}])[0].get(
            "message", {}).get("content") or ""
        data = extract_json(text) if want_json else None
        return ChatResult(text=text, cost=cost, data=data, raw=body)

    def image(self, prompt: str, *, model: str | None = None,
              size: str = "1024x1792", n: int = 1) -> list[bytes]:
        payload: dict[str, Any] = {
            "model": model or self.default_image_model,
            "prompt": prompt, "size": size, "n": n}

        response = self._post("images/generations", payload)
        self._record(response.headers)
        images: list[bytes] = []
        for entry in response.json().get("data", []):
            if entry.get("b64_json"):
                images.append(base64.b64decode(entry["b64_json"]))
            elif entry.get("url"):
                fetched = self._client.get(entry["url"])
                fetched.raise_for_status()
                images.append(fetched.content)
        if not images:
            raise OmniRouteError("image response contained no data")
        return images

    def embed(self, texts: Iterable[str], *,
              model: str | None = None) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": model or self.default_embed_model,
            "input": list(texts)}

        response = self._post("embeddings", payload)
        self._record(response.headers)
        rows = sorted(response.json().get("data", []),
                      key=lambda r: r.get("index", 0))
        return [r["embedding"] for r in rows]

    def moderate(self, text: str, *,
                 model: str = DEFAULT_MODERATION_MODEL) -> ModerationResult:
        response = self._post("moderations", {"input": text, "model": model})
        cost = self._record(response.headers)
        result = (response.json().get("results") or [{}])[0]
        categories = result.get("categories") or {}
        return ModerationResult(
            flagged=bool(result.get("flagged")),
            flags=[k for k, v in categories.items() if v],
            cost=cost,
        )

    def ping(self, timeout: float = 2.5) -> bool:
        """Is the gateway process answering at all?"""
        try:
            r = self._client.get(f"{self.base}/models", timeout=timeout)
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    def health(self, timeout: float = 25.0) -> dict:
        """Three distinct states, because they need three different fixes.

        ``down``        -> gateway not running: start it
        ``no_provider`` -> gateway up, zero provider keys: add one
        ``ready``       -> a chat completion actually came back
        """
        if not self.ping(timeout=min(timeout, 5.0)):
            return {"state": "down", "models": 0,
                    "detail": f"no answer from {self.base}"}

        models = 0
        try:
            r = self._client.get(f"{self.base}/models", timeout=timeout)
            models = len(r.json().get("data", []))
        except Exception:
            pass

        # A single short-timeout probe, deliberately not routed through
        # ``chat``. On a gateway with no provider nodes, OmniRoute walks its
        # whole upstream pool before answering — measured at well over a
        # minute — and ``chat`` would then retry that five times. A health
        # endpoint has to answer while someone is looking at it.
        try:
            response = self._client.post(
                f"{self.base}/chat/completions",
                json={"model": self.default_chat_model,
                      "messages": [{"role": "user", "content": "ok"}],
                      "max_tokens": 4, "temperature": 0},
                timeout=timeout)
        except httpx.TimeoutException:
            return {"state": "no_provider", "models": models,
                    "detail": f"no provider answered within {timeout:.0f}s"}
        except httpx.HTTPError as exc:
            return {"state": "down", "models": models, "detail": str(exc)[:200]}

        no_provider_hint = ("no upstream provider is configured — add one key "
                            "to ~/.omniroute/.env and restart the gateway")

        if response.status_code >= 400:
            body = response.text[:300]
            return {"state": "no_provider", "models": models,
                    "detail": no_provider_hint
                    if self._is_no_provider(body) else body}

        # A 200 is not enough. With no provider configured, OmniRoute answers
        # some small requests 200 with a completely empty body, so checking
        # only the status code reports a healthy gateway that cannot actually
        # complete anything. Require real content back.
        raw = response.text.strip()
        if not raw:
            return {"state": "no_provider", "models": models,
                    "detail": f"{no_provider_hint} (gateway returned 200 with "
                              f"an empty body)"}
        try:
            content = (response.json().get("choices") or [{}])[0].get(
                "message", {}).get("content")
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return {"state": "no_provider", "models": models,
                    "detail": f"{no_provider_hint} (unparseable response: "
                              f"{raw[:120]})"}
        if not (content or "").strip():
            return {"state": "no_provider", "models": models,
                    "detail": f"{no_provider_hint} (empty completion)"}

        return {"state": "ready", "models": models,
                "detail": f"completion ok via "
                          f"{response.headers.get('X-OmniRoute-Provider', '?')}"}

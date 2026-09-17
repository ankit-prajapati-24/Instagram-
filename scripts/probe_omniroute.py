"""Probe the live OmniRoute gateway and report what actually works.

This exists because the whole design hangs on which endpoints answer. Run it
whenever the gateway is restarted or providers change:

    python scripts/probe_omniroute.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from engine.config import settings

BASE = settings.omniroute_base
KEY = settings.omniroute_key
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def line(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label:<26} {detail}")


def probe_models(client: httpx.Client) -> list[str]:
    try:
        r = client.get(f"{BASE}/models", headers=H, timeout=20)
        ids = [m["id"] for m in r.json().get("data", [])]
        line("GET /models", r.status_code == 200, f"{len(ids)} models")
        return ids
    except Exception as exc:
        line("GET /models", False, repr(exc)[:120])
        return []


def probe_chat(client: httpx.Client, model: str | None) -> None:
    payload = {
        "messages": [{"role": "user",
                      "content": "Reply with exactly: OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    if model:
        payload["model"] = model
    try:
        r = client.post(f"{BASE}/chat/completions", headers=H, json=payload,
                        timeout=120)
        body = r.json()
        text = (body.get("choices") or [{}])[0].get(
            "message", {}).get("content", "")
        cost = r.headers.get("X-OmniRoute-Response-Cost", "-")
        prov = r.headers.get("X-OmniRoute-Provider", "-")
        fb = r.headers.get("X-OmniRoute-Fallback-Attempts", "0")
        line(f"POST /chat ({model or 'default'})", r.status_code == 200,
             f"text={text.strip()[:24]!r} provider={prov} cost={cost} "
             f"fallbacks={fb}")
    except Exception as exc:
        line(f"POST /chat ({model or 'default'})", False, repr(exc)[:120])


def probe_json_mode(client: httpx.Client, model: str | None) -> None:
    payload = {
        "messages": [{"role": "user", "content":
                      'Return JSON only: {"a": 1, "b": "x"}'}],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
        "temperature": 0,
    }
    if model:
        payload["model"] = model
    try:
        r = client.post(f"{BASE}/chat/completions", headers=H, json=payload,
                        timeout=120)
        text = (r.json().get("choices") or [{}])[0].get(
            "message", {}).get("content", "")
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            pass
        line("POST /chat json_object", parsed is not None,
             f"raw={text.strip()[:60]!r}")
    except Exception as exc:
        line("POST /chat json_object", False, repr(exc)[:120])


def probe_hindi(client: httpx.Client, model: str | None) -> None:
    payload = {
        "messages": [{"role": "user", "content":
                      "Ek line Hindi (Devanagari) me likho Roopkund jheel ke "
                      "baare me. Sirf wo line do."}],
        "max_tokens": 120,
        "temperature": 0.3,
    }
    if model:
        payload["model"] = model
    try:
        r = client.post(f"{BASE}/chat/completions", headers=H, json=payload,
                        timeout=120)
        text = (r.json().get("choices") or [{}])[0].get(
            "message", {}).get("content", "")
        has_deva = any("ऀ" <= ch <= "ॿ" for ch in text)
        line("Hindi Devanagari output", has_deva, text.strip()[:70])
    except Exception as exc:
        line("Hindi Devanagari output", False, repr(exc)[:120])


def probe_images(client: httpx.Client) -> None:
    try:
        r = client.get(f"{BASE}/images/generations", headers=H, timeout=30)
        data = r.json()
        models = data.get("data", data) if isinstance(data, dict) else data
        n = len(models) if isinstance(models, list) else 0
        line("GET /images/generations", r.status_code == 200,
             f"{n} image models listed")
    except Exception as exc:
        line("GET /images/generations", False, repr(exc)[:120])

    try:
        r = client.post(f"{BASE}/images/generations", headers=H, timeout=180,
                        json={"prompt": "a dark misty lake, cinematic",
                              "size": "1024x1024", "n": 1})
        ok = r.status_code == 200 and bool(r.json().get("data"))
        entry = (r.json().get("data") or [{}])[0] if ok else {}
        kind = "b64" if entry.get("b64_json") else (
            "url" if entry.get("url") else "empty")
        line("POST /images/generations", ok,
             f"status={r.status_code} payload={kind} "
             f"provider={r.headers.get('X-OmniRoute-Provider', '-')}")
        if not ok:
            print("        body:", r.text[:300])
    except Exception as exc:
        line("POST /images/generations", False, repr(exc)[:120])


def probe_embeddings(client: httpx.Client) -> None:
    try:
        r = client.post(f"{BASE}/embeddings", headers=H, timeout=90,
                        json={"input": ["roopkund skeletons",
                                        "bhangarh fort curse"]})
        rows = r.json().get("data", [])
        dim = len(rows[0]["embedding"]) if rows else 0
        line("POST /embeddings", r.status_code == 200 and dim > 0,
             f"status={r.status_code} dim={dim} rows={len(rows)}")
        if r.status_code != 200:
            print("        body:", r.text[:300])
    except Exception as exc:
        line("POST /embeddings", False, repr(exc)[:120])


def probe_moderations(client: httpx.Client) -> None:
    try:
        r = client.post(f"{BASE}/moderations", headers=H, timeout=60,
                        json={"input": "A mysterious unsolved disappearance.",
                              "model": "omni-moderation-latest"})
        res = (r.json().get("results") or [{}])[0]
        line("POST /moderations", r.status_code == 200 and "flagged" in res,
             f"status={r.status_code} flagged={res.get('flagged')}")
        if r.status_code != 200:
            print("        body:", r.text[:300])
    except Exception as exc:
        line("POST /moderations", False, repr(exc)[:120])


def probe_tts(client: httpx.Client) -> None:
    """Expected to be weak for hi-IN. Documents WHY we use local edge-tts."""
    try:
        r = client.post(f"{BASE}/audio/speech", headers=H, timeout=120,
                        json={"model": "openai/tts-1",
                              "input": "रूपकुंड झील", "voice": "alloy"})
        line("POST /audio/speech", r.status_code == 200,
             f"status={r.status_code} bytes={len(r.content)} "
             f"ctype={r.headers.get('content-type', '-')}")
        if r.status_code != 200:
            print("        body:", r.text[:200])
    except Exception as exc:
        line("POST /audio/speech", False, repr(exc)[:120])


def main() -> int:
    print(f"OmniRoute probe -> {BASE}\n")
    with httpx.Client(follow_redirects=True) as client:
        ids = probe_models(client)

        interesting = [m for m in ids
                       if any(k in m.lower() for k in
                              ("auto/", "free", "flux", "embed", "moderation"))]
        print(f"\n  sample model ids: {ids[:6]}")
        print(f"  auto/free/flux/embed ids: {interesting[:10]}\n")

        probe_chat(client, None)
        probe_chat(client, "auto/best-coding" if "auto/best-coding" in ids
                   else None)
        probe_json_mode(client, None)
        probe_hindi(client, None)
        print()
        probe_images(client)
        probe_embeddings(client)
        probe_moderations(client)
        probe_tts(client)

    print("\nNote: /audio/speech is probed only to document the gap. Hindi "
          "voice comes from local edge-tts either way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

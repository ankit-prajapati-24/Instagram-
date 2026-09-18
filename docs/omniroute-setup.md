# OmniRoute setup — the one thing that is still missing

**Status on this machine, measured 2026-09-17:** OmniRoute v3.8.48 is
installed and running on port 20128. It cannot complete a single request,
because it has no provider keys.

This is the only blocker between the pipeline and real scripts. Everything
else — voice, captions, render, gates, inspection — is verified working.

---

## What I actually measured

| Check | Result |
|---|---|
| `GET /v1/models` | **200**, 99 models listed (all `auto/*` routing aliases) |
| `omniroute nodes list` | **`{"nodes": []}`** — zero provider nodes |
| `omniroute simulate "say OK"` | `No matching combo found. Configure one with: omniroute combo create` |
| `POST /v1/chat/completions` | **503** `Maximum combo retry limit reached`, `poolSize: 54, attempted: 29` |
| Same, with `"stream": false` | **503**, identical |
| Same, with `Accept: application/json` | **503**, identical |
| `POST /v1/chat/completions`, tiny request | **200 with an empty body** |
| `POST /v1/moderations` | **400** `No credentials for provider: openai` |
| `POST /v1/audio/speech` | **400** `No credentials for provider: openai` |
| `POST /v1/embeddings` | **400** `Invalid request` |
| `GET /v1/images/generations` | **200**, but **0 image models listed** |

The `attemptOrder` in the 503 diagnostics shows it walking its built-in
keyless pool — `auggie`, `duckduckgo-web` and others — and every one failing,
with `exhausted_connection` noted against them.

## What this means for the "150+ free tiers" claim

The claim is accurate but easy to misread, so it is worth stating plainly:

- OmniRoute **catalogues and manages** free provider tiers. It does not
  **supply** them.
- The free tiers are free tiers *at the providers*. You still register with
  each provider and paste its key into OmniRoute.
- The zero-config keyless providers that ship in the pool are reverse-engineered
  public endpoints. They are not dependable, and on this machine all 29 that
  were tried are dead.

So the gateway's real value here is exactly what it says elsewhere: one
endpoint, automatic fallback between the keys you add, free-tier accounting,
token compression, and cost telemetry. It is a very good router. It is not a
source of keys.

## Two things worth noticing, since they shaped the code

**1. `model` is required.** A request without it returns
`{"error": {"message": "Missing model"}}`. The client always sends one
(`auto/best-chat` by default) rather than treating it as optional.

**2. A 200 does not mean it worked.** With no provider, small requests come
back 200 with an empty body. An earlier version of the health check trusted
the status code and reported a healthy gateway that could not complete
anything. It now requires real content in the response before saying `ready`,
and `OmniRouteClient.chat` raises `NoProviderError` on an empty 200.

---

## Fixing it: add one provider

You already have a Groq account — `agent.py` in EDITOR-_BACKEND calls
`openai/gpt-oss-120b` through it. Groq's free tier is generous and fast, so
it is the shortest path.

### Option A — the dashboard (easiest)

```bash
node "C:/Users/ITG/AppData/Roaming/npm/node_modules/omniroute/bin/omniroute.mjs" open
```

That opens the dashboard in a browser. Add a provider key under the providers
or connections page, then restart the gateway.

### Option B — the env file

Keys live in `C:\Users\ITG\.omniroute\.env`, which OmniRoute loads at startup
(it prints `Loaded env from C:\Users\ITG\.omniroute\.env`). Add your key
there, then restart.

I did not touch that file — it holds credentials, and adding yours is your
call, not mine.

### Confirming it worked

```bash
python scripts/probe_omniroute.py
```

You want `POST /chat` to come back **PASS** with a provider name and a cost
header. Then check the panel: the gauge in the header should read
**gateway ready** instead of **no provider**.

```bash
curl -s http://127.0.0.1:8765/api/health
```

## What is still unavailable even after adding a chat provider

Adding one chat key does **not** light up everything. Each of these needs its
own provider:

| Capability | Endpoint | Used for | If missing |
|---|---|---|---|
| Chat | `/v1/chat/completions` | research, hooks, script, metadata | the panel falls back to the built-in sample script |
| Images | `/v1/images/generations` | scene visuals | falls to the keyless tier, which works but watermarks and upscales; then to placeholders |
| Embeddings | `/v1/embeddings` | semantic dedup, layer 3 | layers 1, 2 and 4 still run; layer 3 is skipped |
| Moderation | `/v1/moderations` | pre-render safety gate | the gate fails closed and stops the plan |

Moderation is worth flagging: it is documented as always costing `$0`, but it
still routes to a provider (OpenAI by default), so it needs credentials like
anything else. Until it has them, `plan_stage` will stop at the moderation
gate. If you want to get moving before sorting that out, run the panel with
the sample brain, which skips it.

## When free image tiers start throttling

At 2–3 videos a day you need roughly 40 images a day, which free image tiers
will not sustain. The fix needs no code change: run **ComfyUI** locally and
register it as an image provider in OmniRoute. It is in the documented
provider list (`SD WebUI (local)`, `ComfyUI (local)`), and
`/v1/images/generations` keeps working exactly as it does now.

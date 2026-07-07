# eBay Alerts

Polls UK eBay for saved searches, analyses new listings with a local Ollama LLM, and sends bid recommendations to your phone via Pushover. A built-in web UI (default `http://localhost:8787`) lets you monitor searches, browse hits, and edit search configs without restarting.

```
New listing found
      │
      ▼
  Ollama LLM
  (bid / maybe / skip, 0-10 score, concerns list)
      │
      ▼
  Pushover notification
  "[BID 8/10] Nikon 50mm: Clean glass, smooth focus — £45"
      │ tap
      ▼
  eBay listing opens on your phone
```

## Prerequisites

| What | Where |
|------|-------|
| eBay developer account | https://developer.ebay.com |
| Pushover app + account | https://pushover.net |
| Ollama running locally or on your server | https://ollama.com |
| Docker + Docker Compose | https://docs.docker.com/compose/ |

---

## 1. eBay API credentials

1. Log in at https://developer.ebay.com → **Hi, [name]** → **Application Keys**
2. Click **Create a keyset** → choose **Production**
3. Copy **App ID (Client ID)** and **Cert ID (Client Secret)** into your `.env`

> The Browse API is included in the free Production key tier.  
> Default quota: 5,000 calls/day. Each poll = 1 call per search.

---

## 2. Pushover credentials

1. Sign in at https://pushover.net — your **User Key** is on the dashboard
2. Go to https://pushover.net/apps/build → create an app called "eBay Alerts"
3. Copy the **API Token** into `.env`

---

## 3. Setup

```bash
cp .env.example .env
# Edit .env — fill in EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, PUSHOVER_TOKEN, PUSHOVER_USER
# Set OLLAMA_URL to wherever your Ollama instance is

mkdir -p data

# Edit searches.yaml — see docs/searches.md for the full field reference
```

### Smoke-test each component before running

```bash
# Test Pushover — sends a real notification to your phone
SEARCHES_FILE=./searches.yaml DATA_DIR=./data python -m app.main --test-pushover

# Test eBay auth — fetches a token and prints the prefix
python -m app.main --test-ebay

# Test Ollama — runs a synthetic lens listing and prints the JSON verdict
python -m app.main --test-llm
```

### Run with Docker

```bash
docker compose up --build -d
docker compose logs -f
```

### Run locally (without Docker)

```bash
pip install httpx pydantic pydantic-settings pyyaml aiosqlite tenacity structlog fastapi uvicorn
SEARCHES_FILE=./searches.yaml DATA_DIR=./data python -m app.main
```

---

## 4. Web UI

Open **http://localhost:8787** once the service is running.

- **Monitor** — each search shows its last/next poll time, result counts, new items this session, hit totals, and any polling errors.
- **Edit** — create, edit, pause/resume, and delete searches; changes apply immediately (the affected poller restarts) and are persisted back to `searches.yaml`.
- **Poll now** — trigger an immediate poll instead of waiting for the next interval.
- **Recent hits** — every new listing found, with its LLM verdict, score, and whether it was notified; links open the eBay listing.

The UI has **no authentication**. The compose file binds it to `127.0.0.1` on the host; if you change that to expose it on your LAN, make sure the network is trusted, and never expose it to the internet.

Because the UI rewrites `searches.yaml` on every change, hand-written comments in that file are lost the first time you save from the UI. Manual file edits still work — they're picked up on the next restart.

---

## 5. Configuring searches

Searches can be managed entirely from the web UI, or by editing `searches.yaml` by hand. See **[docs/searches.md](docs/searches.md)** for the full field reference, including all filter options, sort values, condition codes, LLM criteria guidance, and notification settings.

---

## 6. Architecture

```
searches.yaml ⇄ config_loader ⇄ list[Search]
                                     │
              ┌──────────────────────┼──────────────────────┐
              │ scheduler.SearchManager                     │
              │  one asyncio task per enabled Search        │◀── web.py (FastAPI)
              │  add / update / remove / pause at runtime   │    UI + REST API :8787
              └────────────┬────────────────────────────────┘
                           │ poll every N seconds
                           ▼
                   ebay/client.py
                   Browse API → list[Listing]
                           │
                           ▼
                     store.py (SQLite)
                   filter_unseen() → new items only
                           │
                    ┌──────┴──────┐
                    │ llm.py      │  (if llm: configured)
                    │ Ollama chat │
                    └──────┬──────┘
                           │ Verdict
                           ▼
                   notifier.py
                   Pushover POST
```

State file: `data/state.sqlite`
- `seen_items` — dedupe table; item_id + search_id marked on first sight
- `hits` — audit log of every new item found, with LLM verdict and notified flag (browsable in the web UI)

---

## 7. On Linux Docker hosts (Ollama access)

Docker Desktop for Mac/Windows resolves `host.docker.internal` automatically. On Linux you need to add:

```yaml
# docker-compose.yml
services:
  ebay-alerts:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Or point `OLLAMA_URL` directly at your Ollama service's IP/hostname.

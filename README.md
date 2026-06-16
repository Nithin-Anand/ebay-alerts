# eBay Alerts

Polls UK eBay for saved searches, analyses new listings with a local Ollama LLM, and sends bid recommendations to your phone via Pushover.

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
pip install httpx pydantic pydantic-settings pyyaml aiosqlite tenacity structlog
SEARCHES_FILE=./searches.yaml DATA_DIR=./data python -m app.main
```

---

## 4. Configuring searches

See **[docs/searches.md](docs/searches.md)** for the full field reference, including all filter options, sort values, condition codes, LLM criteria guidance, and notification settings.

---

## 5. Architecture

```
searches.yaml → config_loader → list[Search]
                                     │
                    ┌────────────────┼────────────────┐
                    │ scheduler (asyncio.TaskGroup)   │
                    │  one task per Search            │
                    └────────────┬───────────────────┘
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
- `hits` — audit log of every new item found, with LLM verdict and notified flag

---

## 6. On Linux Docker hosts (Ollama access)

Docker Desktop for Mac/Windows resolves `host.docker.internal` automatically. On Linux you need to add:

```yaml
# docker-compose.yml
services:
  ebay-alerts:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Or point `OLLAMA_URL` directly at your Ollama service's IP/hostname.

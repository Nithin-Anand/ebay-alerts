"""Tests for the web UI REST API and SearchManager lifecycle."""
import asyncio

import httpx
import pytest
import pytest_asyncio

from app.config_loader import load_searches
from app.models import Verdict
from app.scheduler import Deps, SearchManager
from app.store import Store
from app.web import create_app


# ── Stub dependencies (no network) ─────────────────────────────────────────

class StubEbay:
    def __init__(self):
        self.listings = []

    async def search(self, s):
        return self.listings


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, search, listing, verdict=None):
        self.sent.append((search.id, listing.item_id))


class StubOllama:
    async def analyse(self, listing, config):
        return Verdict(recommend="bid", score=8)


SEARCH_BODY = {
    "id": "pi-4",
    "name": "Raspberry Pi 4",
    "query": "raspberry pi 4 8gb",
    "poll_interval_seconds": 3600,
    "filters": {"price_max": 80},
}


@pytest_asyncio.fixture
async def env(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    await store.open()
    deps = Deps(ebay=StubEbay(), store=store, notifier=StubNotifier(), ollama=StubOllama())
    manager = SearchManager([], deps, tmp_path / "searches.yaml")
    app = create_app(manager, store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, manager, deps, tmp_path
    await manager.shutdown()
    await store.close()


# ── CRUD ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_empty(env):
    client, *_ = env
    resp = await client.get("/api/searches")
    assert resp.status_code == 200
    assert resp.json() == {"searches": []}


@pytest.mark.asyncio
async def test_create_persists_and_starts(env):
    client, manager, deps, tmp_path = env
    resp = await client.post("/api/searches", json=SEARCH_BODY)
    assert resp.status_code == 201

    # Persisted to YAML
    saved = load_searches(tmp_path / "searches.yaml")
    assert [s.id for s in saved] == ["pi-4"]

    # Listed with runtime status and hit stats
    listed = (await client.get("/api/searches")).json()["searches"]
    assert listed[0]["id"] == "pi-4"
    assert "status" in listed[0] and "hits" in listed[0]

    # Polling task actually started
    await asyncio.sleep(0.05)
    assert manager.status("pi-4").running is True


@pytest.mark.asyncio
async def test_create_duplicate_conflicts(env):
    client, *_ = env
    await client.post("/api/searches", json=SEARCH_BODY)
    resp = await client.post("/api/searches", json=SEARCH_BODY)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_invalid_body_rejected(env):
    client, *_ = env
    resp = await client.post("/api/searches", json={"id": "Bad Id!", "name": "x", "query": "y"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_persists(env):
    client, manager, deps, tmp_path = env
    await client.post("/api/searches", json=SEARCH_BODY)
    resp = await client.put(
        "/api/searches/pi-4", json={**SEARCH_BODY, "poll_interval_seconds": 900}
    )
    assert resp.status_code == 200
    assert load_searches(tmp_path / "searches.yaml")[0].poll_interval_seconds == 900


@pytest.mark.asyncio
async def test_update_cannot_rename(env):
    client, *_ = env
    await client.post("/api/searches", json=SEARCH_BODY)
    resp = await client.put("/api/searches/pi-4", json={**SEARCH_BODY, "id": "other"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_unknown_404(env):
    client, *_ = env
    resp = await client.put("/api/searches/nope", json={**SEARCH_BODY, "id": "nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_stops_and_persists(env):
    client, manager, deps, tmp_path = env
    await client.post("/api/searches", json=SEARCH_BODY)
    resp = await client.delete("/api/searches/pi-4")
    assert resp.status_code == 204
    assert load_searches(tmp_path / "searches.yaml") == []
    assert (await client.delete("/api/searches/pi-4")).status_code == 404


@pytest.mark.asyncio
async def test_disabled_search_does_not_poll(env):
    client, manager, *_ = env
    await client.post("/api/searches", json={**SEARCH_BODY, "enabled": False})
    await asyncio.sleep(0.05)
    assert manager.status("pi-4").running is False
    # Poll-now on a paused search is a conflict
    assert (await client.post("/api/searches/pi-4/poll")).status_code == 409


# ── Poll cycle through the stubs ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_listing_flows_to_hits_endpoint(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    deps.ebay.listings = [
        Listing(
            item_id="item-1",
            title="Raspberry Pi 4 8GB",
            price=Decimal("65.00"),
            item_web_url="https://www.ebay.co.uk/itm/1",
        )
    ]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)  # let the first tick run

    assert deps.notifier.sent == [("pi-4", "item-1")]
    hits = (await client.get("/api/hits")).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["item_id"] == "item-1"
    assert hits[0]["notified"] == 1

    # Scoped to an unknown search → empty
    hits = (await client.get("/api/hits", params={"search_id": "other"})).json()["hits"]
    assert hits == []


@pytest.mark.asyncio
async def test_poll_now_triggers_immediate_tick(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    await client.post("/api/searches", json=SEARCH_BODY)  # interval 3600s
    await asyncio.sleep(0.1)
    assert deps.notifier.sent == []

    # A new listing appears; without poll-now we'd wait an hour
    deps.ebay.listings = [
        Listing(
            item_id="item-2",
            title="Raspberry Pi 4 8GB boxed",
            price=Decimal("70.00"),
            item_web_url="https://www.ebay.co.uk/itm/2",
        )
    ]
    resp = await client.post("/api/searches/pi-4/poll")
    assert resp.status_code == 202
    await asyncio.sleep(0.1)
    assert deps.notifier.sent == [("pi-4", "item-2")]

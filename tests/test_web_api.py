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
        self.over_listings = []  # returned for the above-ceiling refresh query
        self.over_ceiling_calls = 0  # times the above-ceiling query was issued
        self.item = None  # returned by get_item (re-run path)
        self.items = {}   # item_id -> Listing | None, for get_items (prune path)

    async def search(self, s):
        # The over-ceiling refresh re-queries with price_max dropped and
        # price_min set to the old ceiling — serve it the out-of-range results.
        if s.filters.price_max is None and s.filters.price_min is not None:
            self.over_ceiling_calls += 1
            return self.over_listings
        return self.listings

    async def get_item(self, item_id):
        return self.item

    async def get_items(self, item_ids):
        return {iid: self.items.get(iid) for iid in item_ids}


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, search, listing, verdict=None):
        self.sent.append((search.id, listing.item_id))


class StubOllama:
    def __init__(self):
        self.verdict = Verdict(recommend="bid", score=8)

    async def analyse(self, listing, config):
        return self.verdict


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
async def test_delete_hits_endpoint(env):
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
    await asyncio.sleep(0.1)
    assert len((await client.get("/api/hits")).json()["hits"]) == 1

    resp = await client.post(
        "/api/hits/delete",
        json={"items": [{"search_id": "pi-4", "item_id": "item-1"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    assert (await client.get("/api/hits")).json()["hits"] == []


# ── Prune / archive inactive listings ──────────────────────────────────────

@pytest.mark.asyncio
async def test_prune_archives_ended_listings(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    live = Listing(
        item_id="live-1", title="Live", price=Decimal("30.00"),
        item_web_url="https://www.ebay.co.uk/itm/live",
    )
    ended = Listing(
        item_id="ended-1", title="Ended", price=Decimal("40.00"),
        item_web_url="https://www.ebay.co.uk/itm/ended",
    )
    deps.ebay.listings = [live, ended]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)  # first tick records both hits

    # Both start out active
    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert {h["item_id"] for h in active} == {"live-1", "ended-1"}

    # eBay now reports ended-1 as gone; ended-1 also drops out of search results
    deps.ebay.listings = [live]
    deps.ebay.items = {"live-1": live, "ended-1": None}

    resp = await client.post("/api/hits/prune")
    assert resp.status_code == 200
    assert resp.json()["archived"] == 1

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert [h["item_id"] for h in active] == ["live-1"]
    archived = (await client.get("/api/hits", params={"archived": "true"})).json()["hits"]
    assert [h["item_id"] for h in archived] == ["ended-1"]


@pytest.mark.asyncio
async def test_prune_reappearing_listing_is_revived(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    item = Listing(
        item_id="flaky-1", title="Flaky", price=Decimal("30.00"),
        item_web_url="https://www.ebay.co.uk/itm/flaky",
    )
    deps.ebay.listings = [item]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)

    # A transient miss archives it...
    deps.ebay.items = {"flaky-1": None}
    assert (await client.post("/api/hits/prune")).json()["archived"] == 1
    assert [h["item_id"] for h in
            (await client.get("/api/hits", params={"archived": "true"})).json()["hits"]] == ["flaky-1"]

    # ...but it reappears in the next poll and is moved back to active
    await client.post("/api/searches/pi-4/poll")
    await asyncio.sleep(0.1)
    assert (await client.get("/api/hits", params={"archived": "true"})).json()["hits"] == []
    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert [h["item_id"] for h in active] == ["flaky-1"]


@pytest.mark.asyncio
async def test_over_ceiling_refresh_updates_out_of_range_auction(env):
    """An auction whose bid climbs past price_max drops out of the capped
    results; the above-ceiling re-query catches it and refreshes its price
    (which the pruner/getItems path can't, e.g. when Buy API access is denied)."""
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    auction = Listing(
        item_id="auc-1", title="Auction", price=Decimal("30.00"),
        current_bid_price=Decimal("30.00"), buying_options=["AUCTION"],
        item_web_url="https://www.ebay.co.uk/itm/auc",
    )
    deps.ebay.listings = [auction]
    await client.post("/api/searches", json=SEARCH_BODY)  # price_max = 80
    await asyncio.sleep(0.1)  # first tick records the hit at £30

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert active[0]["price"] == 30.00

    # Bid climbs past £80: gone from the capped results, but the above-ceiling
    # re-query still sees it live at £95.
    raised = auction.model_copy(update={"current_bid_price": Decimal("95.00")})
    deps.ebay.listings = []
    deps.ebay.over_listings = [raised]
    await client.post("/api/searches/pi-4/poll")
    await asyncio.sleep(0.1)

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert active[0]["item_id"] == "auc-1"
    assert active[0]["price"] == 95.00


@pytest.mark.asyncio
async def test_over_ceiling_refresh_skipped_when_nothing_missing(env):
    """When every tracked auction is still in the capped results, the extra
    above-ceiling query must not run — it would waste an eBay API call."""
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    auction = Listing(
        item_id="auc-1", title="Auction", price=Decimal("30.00"),
        current_bid_price=Decimal("30.00"), buying_options=["AUCTION"],
        item_web_url="https://www.ebay.co.uk/itm/auc",
    )
    deps.ebay.listings = [auction]
    await client.post("/api/searches", json=SEARCH_BODY)  # price_max = 80
    await asyncio.sleep(0.1)

    deps.ebay.over_ceiling_calls = 0
    await client.post("/api/searches/pi-4/poll")  # auc-1 still present in results
    await asyncio.sleep(0.1)

    # Nothing dropped out of range, so the extra eBay query must not be issued.
    assert deps.ebay.over_ceiling_calls == 0


@pytest.mark.asyncio
async def test_ended_auction_is_auto_archived_by_end_time(env):
    """An auction is archived once its captured end time passes, without needing
    the getItems liveness check — the search-only auto-archive path."""
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    ended = Listing(
        item_id="auc-1", title="Auction", price=Decimal("30.00"),
        current_bid_price=Decimal("30.00"), buying_options=["AUCTION"],
        item_web_url="https://www.ebay.co.uk/itm/auc",
        end_time="2000-01-01T00:00:00.000Z",  # already in the past
    )
    deps.ebay.listings = [ended]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)  # tick 1 records the hit (the sweep runs before it)

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert [h["item_id"] for h in active] == ["auc-1"]

    # It ends and drops out of results; the next poll archives it from the
    # stored end time alone.
    deps.ebay.listings = []
    await client.post("/api/searches/pi-4/poll")
    await asyncio.sleep(0.1)

    assert (await client.get("/api/hits", params={"archived": "false"})).json()["hits"] == []
    archived = (await client.get("/api/hits", params={"archived": "true"})).json()["hits"]
    assert [h["item_id"] for h in archived] == ["auc-1"]


@pytest.mark.asyncio
async def test_live_auction_not_archived_before_end_time(env):
    """A still-running auction (end time in the future) must never be archived."""
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    live = Listing(
        item_id="auc-1", title="Auction", price=Decimal("30.00"),
        current_bid_price=Decimal("30.00"), buying_options=["AUCTION"],
        item_web_url="https://www.ebay.co.uk/itm/auc",
        end_time="2999-01-01T00:00:00.000Z",  # far in the future
    )
    deps.ebay.listings = [live]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)
    await client.post("/api/searches/pi-4/poll")
    await asyncio.sleep(0.1)

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert [h["item_id"] for h in active] == ["auc-1"]
    assert (await client.get("/api/hits", params={"archived": "true"})).json()["hits"] == []


@pytest.mark.asyncio
async def test_prune_refreshes_price_of_live_out_of_range_auction(env):
    """An auction whose bid climbs past the search's price_max drops out of
    search results but is still live on eBay — the pruner is the only path that
    still sees it, so it must refresh the stored price from the live bid."""
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    auction = Listing(
        item_id="auc-1", title="Auction", price=Decimal("30.00"),
        current_bid_price=Decimal("30.00"), buying_options=["AUCTION"],
        item_web_url="https://www.ebay.co.uk/itm/auc",
    )
    deps.ebay.listings = [auction]
    await client.post("/api/searches", json=SEARCH_BODY)  # price_max = 80
    await asyncio.sleep(0.1)  # first tick records the hit at £30

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert active[0]["price"] == 30.00

    # Bid climbs past the £80 max: gone from search results, still live at £95.
    raised = auction.model_copy(update={"current_bid_price": Decimal("95.00")})
    deps.ebay.listings = []
    deps.ebay.items = {"auc-1": raised}

    resp = await client.post("/api/hits/prune")
    assert resp.json()["archived"] == 0  # still live — not archived

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert active[0]["item_id"] == "auc-1"
    assert active[0]["price"] == 95.00  # price refreshed by the pruner


@pytest.mark.asyncio
async def test_unarchive_endpoint_restores_hit(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    item = Listing(
        item_id="item-1", title="X", price=Decimal("20.00"),
        item_web_url="https://www.ebay.co.uk/itm/1",
    )
    deps.ebay.listings = [item]
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)

    # Archive it (eBay reports it gone; it also leaves search results)
    deps.ebay.listings = []
    deps.ebay.items = {"item-1": None}
    assert (await client.post("/api/hits/prune")).json()["archived"] == 1

    # Manual restore moves it back to active
    resp = await client.post(
        "/api/hits/unarchive",
        json={"items": [{"search_id": "pi-4", "item_id": "item-1"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["unarchived"] == 1

    active = (await client.get("/api/hits", params={"archived": "false"})).json()["hits"]
    assert [h["item_id"] for h in active] == ["item-1"]
    assert (await client.get("/api/hits", params={"archived": "true"})).json()["hits"] == []


# ── Re-run LLM analysis ────────────────────────────────────────────────────

LLM_SEARCH_BODY = {
    **SEARCH_BODY,
    "id": "pi-llm",
    "llm": {"enabled": True, "criteria": "good condition only"},
}


@pytest.mark.asyncio
async def test_rerun_updates_verdict(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    listing = Listing(
        item_id="item-x",
        title="Raspberry Pi 4",
        price=Decimal("60.00"),
        item_web_url="https://www.ebay.co.uk/itm/x",
    )
    deps.ebay.listings = [listing]
    deps.ebay.item = listing

    # First analysis "fails" → stored as the maybe/5 fallback
    deps.ollama.verdict = Verdict(recommend="maybe", score=5, concerns=["LLM unavailable"])
    await client.post("/api/searches", json=LLM_SEARCH_BODY)
    await asyncio.sleep(0.1)
    hit = (await client.get("/api/hits")).json()["hits"][0]
    assert (hit["verdict"], hit["score"]) == ("maybe", 5)

    # Re-run now succeeds with a real verdict
    deps.ollama.verdict = Verdict(recommend="bid", score=9)
    resp = await client.post(
        "/api/hits/rerun", json={"search_id": "pi-llm", "item_id": "item-x"}
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "bid" and resp.json()["score"] == 9

    hit = (await client.get("/api/hits")).json()["hits"][0]
    assert (hit["verdict"], hit["score"]) == ("bid", 9)


@pytest.mark.asyncio
async def test_rerun_requires_llm_enabled(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    deps.ebay.listings = [
        Listing(
            item_id="item-1",
            title="Raspberry Pi 4",
            price=Decimal("60.00"),
            item_web_url="https://www.ebay.co.uk/itm/1",
        )
    ]
    # SEARCH_BODY has no LLM config
    await client.post("/api/searches", json=SEARCH_BODY)
    await asyncio.sleep(0.1)
    resp = await client.post(
        "/api/hits/rerun", json={"search_id": "pi-4", "item_id": "item-1"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_rerun_unknown_hit_404(env):
    client, *_ = env
    await client.post("/api/searches", json=LLM_SEARCH_BODY)
    resp = await client.post(
        "/api/hits/rerun", json={"search_id": "pi-llm", "item_id": "nope"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rerun_listing_gone_409(env):
    from decimal import Decimal
    from app.models import Listing

    client, manager, deps, _ = env
    listing = Listing(
        item_id="item-x",
        title="Raspberry Pi 4",
        price=Decimal("60.00"),
        item_web_url="https://www.ebay.co.uk/itm/x",
    )
    deps.ebay.listings = [listing]
    deps.ebay.item = None  # eBay says the item is gone
    await client.post("/api/searches", json=LLM_SEARCH_BODY)
    await asyncio.sleep(0.1)
    resp = await client.post(
        "/api/hits/rerun", json={"search_id": "pi-llm", "item_id": "item-x"}
    )
    assert resp.status_code == 409


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

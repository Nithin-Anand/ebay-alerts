"""Tests for the SQLite dedupe store."""
import pytest
import pytest_asyncio

from app.store import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    s = Store(tmp_path / "test.sqlite")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_all_ids_new_on_first_call(store):
    new = await store.filter_unseen("search1", ["a", "b", "c"])
    assert set(new) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_second_call_returns_only_fresh_ids(store):
    await store.filter_unseen("search1", ["a", "b"])
    new = await store.filter_unseen("search1", ["a", "b", "c"])
    assert new == ["c"]


@pytest.mark.asyncio
async def test_idempotent_repeated_call(store):
    await store.filter_unseen("search1", ["a"])
    new = await store.filter_unseen("search1", ["a"])
    assert new == []


@pytest.mark.asyncio
async def test_different_searches_are_isolated(store):
    await store.filter_unseen("search1", ["a"])
    # The same item_id is new in a different search namespace
    new = await store.filter_unseen("search2", ["a"])
    assert new == ["a"]


@pytest.mark.asyncio
async def test_empty_input_returns_empty(store):
    new = await store.filter_unseen("search1", [])
    assert new == []


@pytest.mark.asyncio
async def test_record_hit_stores_data(store):
    await store.filter_unseen("search1", ["item-1"])
    await store.record_hit(
        search_id="search1",
        item_id="item-1",
        title="Test Item",
        price=45.00,
        url="https://www.ebay.co.uk/itm/1",
        verdict="bid",
        score=8,
        notified=True,
    )
    async with store._db.execute(
        "SELECT verdict, score, notified FROM hits WHERE search_id=? AND item_id=?",
        ("search1", "item-1"),
    ) as cursor:
        row = await cursor.fetchone()
    assert row == ("bid", 8, 1)


@pytest.mark.asyncio
async def test_record_hit_without_verdict(store):
    await store.filter_unseen("search1", ["item-2"])
    await store.record_hit(
        search_id="search1",
        item_id="item-2",
        title="No LLM Item",
        price=10.00,
        url="https://www.ebay.co.uk/itm/2",
        notified=True,
    )
    async with store._db.execute(
        "SELECT verdict, score FROM hits WHERE item_id=?", ("item-2",)
    ) as cursor:
        row = await cursor.fetchone()
    assert row == (None, None)


@pytest.mark.asyncio
async def test_record_hit_stores_buying_options(store):
    await store.record_hit(
        search_id="search1",
        item_id="item-3",
        title="Auction Item",
        price=12.50,
        url="https://www.ebay.co.uk/itm/3",
        buying_options=["AUCTION", "BEST_OFFER"],
    )
    hits = await store.recent_hits("search1")
    assert hits[0]["buying_options"] == "AUCTION,BEST_OFFER"


@pytest.mark.asyncio
async def test_raise_auction_prices_bumps_only_when_higher(store):
    await store.record_hit(
        search_id="search1",
        item_id="auction-1",
        title="Live Auction",
        price=20.00,
        url="https://www.ebay.co.uk/itm/a1",
        buying_options=["AUCTION"],
    )
    # Higher bid raises the stored price
    await store.raise_auction_prices("search1", [("auction-1", 25.00)])
    hits = await store.recent_hits("search1")
    assert hits[0]["price"] == 25.00

    # A lower or equal bid is a no-op (bids never go down)
    await store.raise_auction_prices("search1", [("auction-1", 22.00)])
    hits = await store.recent_hits("search1")
    assert hits[0]["price"] == 25.00


@pytest.mark.asyncio
async def test_raise_auction_prices_ignores_unknown_items(store):
    # Updating an item that was never recorded must not create a row
    await store.raise_auction_prices("search1", [("ghost", 99.00)])
    assert await store.recent_hits("search1") == []


@pytest.mark.asyncio
async def test_delete_hits_removes_selected_rows(store):
    for iid in ("a", "b", "c"):
        await store.record_hit(
            search_id="search1", item_id=iid, title="t", price=1.0, url="u"
        )
    deleted = await store.delete_hits([("search1", "a"), ("search1", "c")])
    assert deleted == 2
    remaining = [h["item_id"] for h in await store.recent_hits("search1")]
    assert remaining == ["b"]


@pytest.mark.asyncio
async def test_delete_hits_empty_is_noop(store):
    assert await store.delete_hits([]) == 0


@pytest.mark.asyncio
async def test_get_hit_returns_row_or_none(store):
    await store.record_hit(
        search_id="s", item_id="i", title="t", price=3.0, url="u",
        verdict="maybe", score=5,
    )
    hit = await store.get_hit("s", "i")
    assert hit["verdict"] == "maybe" and hit["score"] == 5
    assert await store.get_hit("s", "missing") is None


@pytest.mark.asyncio
async def test_update_verdict_overwrites(store):
    await store.record_hit(
        search_id="s", item_id="i", title="t", price=3.0, url="u",
        verdict="maybe", score=5, notified=False,
    )
    updated = await store.update_verdict(
        search_id="s", item_id="i", verdict="bid", score=9, notified=True,
    )
    assert updated is True
    hit = await store.get_hit("s", "i")
    assert (hit["verdict"], hit["score"], hit["notified"]) == ("bid", 9, 1)

    # No matching row → False
    assert await store.update_verdict(
        search_id="s", item_id="ghost", verdict="bid", score=9, notified=True,
    ) is False

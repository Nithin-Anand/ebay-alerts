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

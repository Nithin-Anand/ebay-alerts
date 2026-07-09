"""Tests for the eBay client's batched liveness check (getItems)."""
from decimal import Decimal

import httpx
import pytest

from app.ebay.client import EbayClient, _has_ended
from app.models import Listing


class StubAuth:
    async def get_token(self):
        return "token"

    async def refresh(self):
        return "token"


def _item(item_id, **extra):
    base = {
        "itemId": item_id,
        "title": "T",
        "price": {"value": "10.00", "currency": "GBP"},
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "buyingOptions": ["FIXED_PRICE"],
    }
    base.update(extra)
    return base


def _make_client(handler) -> EbayClient:
    client = EbayClient(StubAuth())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _listing(end_time):
    return Listing(
        item_id="i", title="t", price=Decimal("1"),
        item_web_url="u", end_time=end_time,
    )


def test_has_ended():
    assert _has_ended(_listing("2000-01-01T00:00:00.000Z")) is True
    assert _has_ended(_listing("2999-01-01T00:00:00.000Z")) is False
    assert _has_ended(_listing(None)) is False
    assert _has_ended(_listing("not-a-date")) is False


@pytest.mark.asyncio
async def test_get_items_maps_gone_and_ended_to_none():
    def handler(request):
        ids = request.url.params.get("item_ids").split(",")
        items = []
        for iid in ids:
            if iid == "gone":
                items.append(None)  # eBay returns null for ids it can't resolve
            elif iid == "ended":
                items.append(_item(iid, itemEndDate="2000-01-01T00:00:00.000Z",
                                   buyingOptions=["AUCTION"]))
            else:
                items.append(_item(iid))
        return httpx.Response(200, json={"items": items})

    client = _make_client(handler)
    try:
        result = await client.get_items(["live-1", "gone", "ended"])
    finally:
        await client.aclose()

    assert result["live-1"] is not None and result["live-1"].item_id == "live-1"
    assert result["gone"] is None
    assert result["ended"] is None


@pytest.mark.asyncio
async def test_get_items_batches_in_chunks_of_20():
    calls = []

    def handler(request):
        ids = request.url.params.get("item_ids").split(",")
        calls.append(len(ids))
        return httpx.Response(200, json={"items": [_item(i) for i in ids]})

    client = _make_client(handler)
    ids = [f"id-{n}" for n in range(25)]
    try:
        result = await client.get_items(ids)
    finally:
        await client.aclose()

    assert len(result) == 25
    assert all(result[i] is not None for i in ids)
    assert calls == [20, 5]  # 20 + 5, never more than the getItems cap

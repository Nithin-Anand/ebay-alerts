"""Tests for Browse API item → Listing parsing and price display."""
from decimal import Decimal

from app.ebay.client import _parse_listing


def _item(**overrides) -> dict:
    base = {
        "itemId": "v1|123|0",
        "title": "Thing",
        "price": {"value": "40.00", "currency": "GBP"},
        "itemWebUrl": "https://www.ebay.co.uk/itm/123",
        "buyingOptions": ["FIXED_PRICE"],
    }
    base.update(overrides)
    return base


def test_fixed_price_listing_uses_price():
    listing = _parse_listing(_item())
    assert listing.current_bid_price is None
    assert listing.is_auction is False
    assert listing.display_price == Decimal("40.00")


def test_auction_display_price_is_current_bid():
    listing = _parse_listing(_item(
        buyingOptions=["AUCTION"],
        currentBidPrice={"value": "55.00", "currency": "GBP"},
    ))
    assert listing.is_auction is True
    assert listing.current_bid_price == Decimal("55.00")
    assert listing.display_price == Decimal("55.00")


def test_auction_without_bids_falls_back_to_price():
    # An auction with no bids may omit currentBidPrice
    listing = _parse_listing(_item(buyingOptions=["AUCTION"], price={"value": "5.00"}))
    assert listing.current_bid_price is None
    assert listing.display_price == Decimal("5.00")


def test_best_offer_detected():
    listing = _parse_listing(_item(buyingOptions=["FIXED_PRICE", "BEST_OFFER"]))
    assert listing.has_best_offer is True


def test_auction_with_buy_it_now():
    listing = _parse_listing(_item(
        buyingOptions=["AUCTION", "FIXED_PRICE"],
        currentBidPrice={"value": "30.00", "currency": "GBP"},
    ))
    assert listing.is_auction is True
    assert "FIXED_PRICE" in listing.buying_options
    assert listing.display_price == Decimal("30.00")

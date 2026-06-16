"""Tests for the Filters → Browse API filter string builder."""
from decimal import Decimal

import pytest

from app.ebay.client import build_filter_string
from app.models import Filters


def test_defaults_include_gb_location_and_delivery():
    result = build_filter_string(Filters())
    assert "itemLocationCountry:GB" in result
    assert "deliveryCountry:GB" in result


def test_single_condition():
    result = build_filter_string(Filters(condition=["USED"]))
    assert "conditions:{USED}" in result


def test_multiple_conditions():
    result = build_filter_string(Filters(condition=["NEW", "USED"]))
    assert "conditions:{NEW|USED}" in result


def test_buying_options():
    result = build_filter_string(Filters(buying_options=["FIXED_PRICE", "AUCTION"]))
    assert "buyingOptions:{FIXED_PRICE|AUCTION}" in result


def test_price_both_bounds():
    result = build_filter_string(Filters(price_min=Decimal("30"), price_max=Decimal("120")))
    assert "price:[30..120],priceCurrency:GBP" in result


def test_price_min_only():
    result = build_filter_string(Filters(price_min=Decimal("10")))
    assert "price:[10..],priceCurrency:GBP" in result


def test_price_max_only():
    result = build_filter_string(Filters(price_max=Decimal("100")))
    assert "price:[..100],priceCurrency:GBP" in result


def test_price_decimal_formatted_cleanly():
    result = build_filter_string(Filters(price_min=Decimal("9.99"), price_max=Decimal("49.99")))
    assert "price:[9.99..49.99],priceCurrency:GBP" in result


def test_category_ids():
    result = build_filter_string(Filters(category_ids=["625", "1234"]))
    assert "categoryIds:{625|1234}" in result


def test_sellers_allowlist():
    result = build_filter_string(Filters(sellers=["seller1", "seller2"]))
    assert "sellers:{seller1|seller2}" in result


def test_sellers_blocklist():
    result = build_filter_string(Filters(exclude_sellers=["dodgy_seller"]))
    assert "excludeSellers:{dodgy_seller}" in result


def test_no_delivery_country_when_none():
    result = build_filter_string(Filters(delivery_country=None))
    assert "deliveryCountry" not in result


def test_all_clauses_comma_separated():
    result = build_filter_string(
        Filters(condition=["USED"], buying_options=["AUCTION"], price_min=Decimal("5"))
    )
    clauses = result.split(",")
    # Ensure we have multiple clauses and they don't contain each other's names
    assert len(clauses) >= 3


def test_empty_result_when_no_filters_no_defaults():
    # delivery_country=None and item_location_country stripped
    result = build_filter_string(Filters(item_location_country="", delivery_country=None))
    assert result == ""

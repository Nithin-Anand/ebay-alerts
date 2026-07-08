from decimal import Decimal

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..models import Filters, Listing, Search
from .auth import EbayAuth

log = structlog.get_logger()

_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# Friendly sort names → Browse API sort param values.
# best_match is the API default so we omit the param rather than sending a value.
_SORT_MAP: dict[str, str | None] = {
    "best_match": None,
    "newly_listed": "newlyListed",
    "ending_soonest": "endingSoonest",
    "price_asc": "price",
    "price_desc": "-price",
}


def build_filter_string(filters: Filters) -> str:
    """
    Convert a Filters model into the eBay Browse API filter= query string.

    Format rules:
      Single value:  filterName:value
      Multiple:      filterName:{val1|val2}
      Range:         filterName:[min..max]   (empty string for open end)
      Currency must accompany price: price:[10..50],priceCurrency:GBP
    """
    parts: list[str] = []

    if filters.condition:
        parts.append(f"conditions:{{{('|').join(filters.condition)}}}")

    if filters.buying_options:
        parts.append(f"buyingOptions:{{{('|').join(filters.buying_options)}}}")

    if filters.price_min is not None or filters.price_max is not None:
        lo = str(filters.price_min) if filters.price_min is not None else ""
        hi = str(filters.price_max) if filters.price_max is not None else ""
        parts.append(f"price:[{lo}..{hi}],priceCurrency:GBP")

    if filters.item_location_country:
        parts.append(f"itemLocationCountry:{filters.item_location_country}")

    if filters.delivery_country:
        parts.append(f"deliveryCountry:{filters.delivery_country}")

    if filters.category_ids:
        parts.append(f"categoryIds:{{{('|').join(filters.category_ids)}}}")

    if filters.sellers:
        parts.append(f"sellers:{{{('|').join(filters.sellers)}}}")

    if filters.exclude_sellers:
        parts.append(f"excludeSellers:{{{('|').join(filters.exclude_sellers)}}}")

    return ",".join(parts)


def _parse_listing(item: dict) -> Listing:
    price_block = item.get("price", {})
    price = Decimal(str(price_block.get("value", "0")))
    currency = price_block.get("currency", "GBP")

    # Auction listings carry the live high bid in currentBidPrice; it is
    # absent for pure fixed-price listings.
    bid_block = item.get("currentBidPrice")
    current_bid_price = (
        Decimal(str(bid_block["value"]))
        if bid_block and bid_block.get("value") is not None
        else None
    )

    primary_image = item.get("image", {}).get("imageUrl")
    extra_images = [
        img["imageUrl"]
        for img in item.get("additionalImages", [])
        if img.get("imageUrl")
    ]

    seller = item.get("seller", {})

    return Listing(
        item_id=item["itemId"],
        title=item.get("title", ""),
        price=price,
        current_bid_price=current_bid_price,
        currency=currency,
        condition=item.get("condition"),
        buying_options=item.get("buyingOptions", []),
        item_web_url=item.get("itemWebUrl", ""),
        image_url=primary_image,
        additional_image_urls=extra_images,
        seller_username=seller.get("username"),
        seller_feedback_percentage=seller.get("feedbackPercentage"),
        short_description=item.get("shortDescription"),
        end_time=item.get("itemEndDate"),
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


class EbayClient:
    def __init__(self, auth: EbayAuth) -> None:
        self._auth = auth
        self._http = httpx.AsyncClient(timeout=30)

    async def search(self, s: Search) -> list[Listing]:
        token = await self._auth.get_token()
        try:
            return await self._do_search(s, token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                log.warning("ebay 401 — refreshing token", search_id=s.id)
                token = await self._auth.refresh()
                return await self._do_search(s, token)
            raise

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def _do_search(self, s: Search, token: str) -> list[Listing]:
        params: dict[str, str | int] = {
            "q": s.query,
            "limit": s.limit,
        }

        sort_val = _SORT_MAP.get(s.sort)
        if sort_val:
            params["sort"] = sort_val

        filter_str = build_filter_string(s.filters)
        if filter_str:
            params["filter"] = filter_str

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            # Tells eBay the browsing context is a UK user
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DGB",
        }

        log.debug("ebay search", search_id=s.id, query=s.query, filter=filter_str)
        resp = await self._http.get(_BROWSE_URL, params=params, headers=headers)
        resp.raise_for_status()

        data = resp.json()
        items = data.get("itemSummaries", [])
        listings = [_parse_listing(item) for item in items]
        log.debug("ebay results", search_id=s.id, found=len(listings))
        return listings

    async def aclose(self) -> None:
        await self._http.aclose()

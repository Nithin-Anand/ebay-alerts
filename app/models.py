"""
Pydantic models for searches.yaml config and runtime data.

For the full searches.yaml field reference, see docs/searches.md.
"""

from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field


# ── eBay enumerated values ─────────────────────────────────────────────────────

ConditionValue = Literal[
    "NEW",
    "USED",
    "UNSPECIFIED",
    "CERTIFIED_REFURBISHED",
    "SELLER_REFURBISHED",
    "FOR_PARTS_OR_NOT_WORKING",
]

BuyingOptionValue = Literal[
    "FIXED_PRICE",  # Buy It Now
    "AUCTION",      # Bid-style auction
    "BEST_OFFER",   # Make an offer
]

SortOrder = Literal[
    "best_match",      # eBay relevance ranking (default; omits the sort param)
    "newly_listed",    # Most recently listed first — best for alert use cases
    "ending_soonest",  # Auctions ending soonest — useful for last-minute sniping
    "price_asc",       # Lowest total price (item + postage) first
    "price_desc",      # Highest total price first
]


# ── Sub-models ─────────────────────────────────────────────────────────────────

class Filters(BaseModel):
    """
    eBay search filter options. All fields are optional.

    Each field maps directly to a Browse API filter= clause:
      condition              → conditions:{NEW|USED|...}
      buying_options         → buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}
      price_min / price_max  → price:[min..max],priceCurrency:GBP
      item_location_country  → itemLocationCountry:GB
      delivery_country       → deliveryCountry:GB
      category_ids           → categoryIds:{id1|id2}
      sellers                → sellers:{name1|name2}
      exclude_sellers        → excludeSellers:{name1|name2}

    eBay condition values and what they mean:
      NEW                    New, unopened, unused
      USED                   Standard pre-owned
      UNSPECIFIED            Seller did not specify
      CERTIFIED_REFURBISHED  Manufacturer-certified refurbished
      SELLER_REFURBISHED     Seller-refurbished
      FOR_PARTS_OR_NOT_WORKING  Sold as-is / for spares
    """

    condition: list[ConditionValue] | None = Field(
        default=None,
        description=(
            "Item conditions to include. Leave unset for all conditions. "
            "Example: [USED, SELLER_REFURBISHED]"
        ),
    )
    buying_options: list[BuyingOptionValue] | None = Field(
        default=None,
        description=(
            "Listing types to include. Leave unset for all types. "
            "FIXED_PRICE = Buy It Now. Example: [AUCTION, FIXED_PRICE]"
        ),
    )
    price_min: Decimal | None = Field(
        default=None,
        description="Minimum price in GBP, inclusive (item + postage).",
    )
    price_max: Decimal | None = Field(
        default=None,
        description="Maximum price in GBP, inclusive (item + postage).",
    )
    item_location_country: str = Field(
        default="GB",
        description=(
            "ISO 3166-1 alpha-2 country code restricting the seller's location. "
            "'GB' limits results to UK-based sellers."
        ),
    )
    delivery_country: str | None = Field(
        default="GB",
        description=(
            "Restrict to items that can be delivered to this country. "
            "Set to null/~ to remove the restriction (shows worldwide sellers)."
        ),
    )
    category_ids: list[str] | None = Field(
        default=None,
        description=(
            "eBay UK leaf-category IDs to restrict the search. "
            "Find IDs via the eBay Category Tree API or by inspecting "
            "category URLs on ebay.co.uk (the number in the URL path)."
        ),
    )
    sellers: list[str] | None = Field(
        default=None,
        description=(
            "Allowlist of seller usernames. Only results from these sellers "
            "are returned. Useful for watching specific trusted sources."
        ),
    )
    exclude_sellers: list[str] | None = Field(
        default=None,
        description="Blocklist of seller usernames. Items from these sellers are hidden.",
    )


class LlmConfig(BaseModel):
    """
    Configuration for per-listing Ollama LLM analysis.

    When enabled, each new listing is passed to the Ollama model together
    with the criteria prompt. The model returns a structured verdict:
    bid / maybe / skip, a 0-10 score, a list of concerns, and brief notes.

    Use min_score_to_notify and skip_verdicts to suppress noisy alerts:
      min_score_to_notify: 6   → only ping Pushover for scores >= 6
      skip_verdicts: [skip]    → never notify for 'skip' verdicts

    All hits (including suppressed ones) are written to the SQLite hits
    table with notified=0 so you can query them later.

    Image URLs are always included in the payload sent to the model.
    Text-only models ignore them. To enable image analysis, set a
    vision-capable model here (e.g. llava, qwen2-vl) — no other changes needed.
    """

    enabled: bool = Field(
        default=True,
        description="Set to false to temporarily disable LLM analysis without removing the config.",
    )
    criteria: str = Field(
        description=(
            "Natural-language instructions for the LLM describing what makes a "
            "good or bad buy for this specific search. Be specific — mention "
            "defects, keywords, or conditions to look for or avoid.\n\n"
            "Example for a camera lens:\n"
            "  'This is a vintage manual-focus lens. Recommend BID only if the\n"
            "   listing clearly states clean optics with no fungus, haze, or\n"
            "   scratches. Flag any of: as-is, for parts, untested, haze,\n"
            "   fungus, fog, scratches, stiff focus, oily blades.'"
        ),
    )
    min_score_to_notify: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "Only send a Pushover notification when the LLM score >= this value. "
            "0 = always notify regardless of score. "
            "7 = only alert on listings the LLM rates as good buys."
        ),
    )
    skip_verdicts: list[Literal["bid", "maybe", "skip"]] = Field(
        default=[],
        description=(
            "Suppress Pushover notifications for these verdict values, even if "
            "the score passes min_score_to_notify. "
            "Example: ['skip'] — never send a notification for 'skip' verdicts."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Ollama model name for this search. Overrides the global OLLAMA_MODEL "
            "environment variable. Use a vision model (e.g. 'llava', 'qwen2-vl') "
            "here to enable image-based analysis on a per-search basis."
        ),
    )


class Notification(BaseModel):
    """Pushover notification settings."""

    pushover_priority: int = Field(
        default=0,
        ge=-2,
        le=2,
        description=(
            "Pushover message priority:\n"
            "  -2  No notification at all (silent store only)\n"
            "  -1  Quiet — always delivers but no sound or vibration\n"
            "   0  Normal\n"
            "   1  High priority — bypasses device quiet hours\n"
            "   2  Emergency — repeats every 30 s until acknowledged (use sparingly)"
        ),
    )
    pushover_sound: str | None = Field(
        default=None,
        description=(
            "Pushover notification sound. Leave unset to use the device default. "
            "Valid values: pushover, bike, bugle, cashregister, classical, cosmic, "
            "falling, gamelan, incoming, intermission, magic, mechanical, pianobar, "
            "siren, spacealarm, tugboat, alien, climb, persistent, echo, updown, "
            "vibrate, none."
        ),
    )


# ── Top-level search model ─────────────────────────────────────────────────────

class Search(BaseModel):
    """
    A single eBay alert search. Each entry in searches.yaml becomes one Search.

    IMPORTANT: the `id` field is the stable dedupe namespace in SQLite.
    Changing a search's id resets its seen-items history, so you'll be
    re-alerted for any listings already in the database for that search.
    """

    id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description=(
            "Stable slug used as the dedupe namespace in SQLite. "
            "Use lowercase letters, numbers, and hyphens only. "
            "Example: 'nikon-50mm-ai-s'"
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Set to false to pause this search without deleting it. "
            "Paused searches keep their seen-items history, so re-enabling "
            "only alerts on listings that appeared while paused."
        ),
    )
    name: str = Field(
        description="Human-readable label shown in Pushover notification titles.",
    )
    query: str = Field(
        description="eBay keyword search string, exactly as you'd type it in the eBay search bar.",
    )
    poll_interval_seconds: int = Field(
        default=600,
        ge=60,
        description=(
            "How often to poll eBay for this search, in seconds. "
            "Minimum 60. Default 600 (10 minutes). "
            "High-value / time-sensitive searches may warrant 120-300 s."
        ),
    )
    sort: SortOrder = Field(
        default="newly_listed",
        description=(
            "'newly_listed' is recommended for alert use cases: new items appear "
            "at the top, so the first page always contains the freshest listings. "
            "'ending_soonest' is useful when you want to snipe ending auctions."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description=(
            "Maximum number of listings to fetch per poll. "
            "The Browse API allows up to 200. Higher limits help avoid missing "
            "items during busy periods but consume more API quota."
        ),
    )
    filters: Filters = Field(
        default_factory=Filters,
        description="eBay search filters. All sub-fields are optional.",
    )
    llm: LlmConfig | None = Field(
        default=None,
        description=(
            "LLM analysis config. Omit this field entirely to disable LLM analysis "
            "and send a Pushover notification for every new listing unconditionally."
        ),
    )
    notification: Notification = Field(
        default_factory=Notification,
        description="Pushover notification settings.",
    )


# ── Runtime models (populated by the poller, not user-configured) ──────────────

class Listing(BaseModel):
    """A single eBay listing returned by the Browse API."""

    item_id: str
    title: str
    price: Decimal
    currency: str = "GBP"
    condition: str | None = None
    buying_options: list[str] = []
    item_web_url: str
    image_url: str | None = None
    additional_image_urls: list[str] = []
    seller_username: str | None = None
    seller_feedback_percentage: str | None = None
    short_description: str | None = None
    end_time: str | None = None  # ISO 8601; only present for auction listings

    @property
    def image_urls(self) -> list[str]:
        """All image URLs, primary first. Passed to LLM for vision-model analysis."""
        urls = []
        if self.image_url:
            urls.append(self.image_url)
        urls.extend(self.additional_image_urls)
        return urls


class Verdict(BaseModel):
    """Structured LLM verdict returned by the Ollama analysis step."""

    recommend: Literal["bid", "maybe", "skip"]
    score: int = Field(ge=0, le=10, description="0 = definite skip, 10 = excellent buy")
    concerns: list[str] = Field(default=[], description="Specific issues found in the listing")
    notes: str = Field(default="", description="Brief 1-2 sentence summary")

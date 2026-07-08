import httpx
import structlog

from .models import Listing, Search, Verdict

log = structlog.get_logger()

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Pushover title has a 250-char limit; keep it short so it fits on a phone screen
_TITLE_ITEM_MAX = 55


def _format_title(search: Search, listing: Listing, verdict: Verdict | None) -> str:
    item = listing.title[:_TITLE_ITEM_MAX]
    if verdict:
        tag = f"{verdict.recommend.upper()} {verdict.score}/10"
        return f"[{tag}] {search.name}: {item}"
    return f"[eBay] {search.name}: {item}"


def _format_message(listing: Listing, verdict: Verdict | None) -> str:
    price = f"£{listing.display_price}"
    if listing.is_auction:
        price += " (current bid)"
    lines: list[str] = [
        f"{price} • {listing.condition or 'Condition unknown'}",
    ]

    if listing.buying_options:
        lines.append(" / ".join(o.replace("_", " ").title() for o in listing.buying_options))

    if listing.end_time:
        lines.append(f"Ends: {listing.end_time}")

    if listing.seller_username:
        seller = listing.seller_username
        if listing.seller_feedback_percentage:
            seller += f" ({listing.seller_feedback_percentage}% feedback)"
        lines.append(f"Seller: {seller}")

    if verdict:
        lines.append("")
        if verdict.concerns:
            lines.append("Concerns:")
            for concern in verdict.concerns[:3]:
                lines.append(f"  • {concern}")
        if verdict.notes:
            lines.append(verdict.notes)

    return "\n".join(lines)


class PushoverNotifier:
    def __init__(self, token: str, user: str) -> None:
        self._token = token
        self._user = user
        self._http = httpx.AsyncClient(timeout=15)

    async def send(
        self,
        search: Search,
        listing: Listing,
        verdict: Verdict | None = None,
    ) -> None:
        payload: dict = {
            "token": self._token,
            "user": self._user,
            "title": _format_title(search, listing, verdict),
            "message": _format_message(listing, verdict),
            "url": listing.item_web_url,
            "url_title": "View on eBay",
            "priority": search.notification.pushover_priority,
        }

        if search.notification.pushover_sound:
            payload["sound"] = search.notification.pushover_sound

        # Emergency priority (2) requires retry + expire parameters
        if search.notification.pushover_priority == 2:
            payload.setdefault("retry", 60)   # retry every 60 s
            payload.setdefault("expire", 3600)  # stop after 1 hour

        try:
            resp = await self._http.post(_PUSHOVER_URL, data=payload)
            resp.raise_for_status()
            log.info("pushover sent", search_id=search.id, item_id=listing.item_id)
        except Exception as exc:
            log.error(
                "pushover send failed",
                search_id=search.id,
                item_id=listing.item_id,
                error=str(exc),
            )

    async def send_test(self) -> None:
        """Send a test notification to verify credentials. Used by --test-pushover."""
        payload = {
            "token": self._token,
            "user": self._user,
            "title": "[eBay Alerts] Test notification",
            "message": "eBay Alerts is configured correctly and can reach Pushover.",
            "priority": 0,
        }
        resp = await self._http.post(_PUSHOVER_URL, data=payload)
        resp.raise_for_status()
        log.info("pushover test notification sent")

    async def aclose(self) -> None:
        await self._http.aclose()

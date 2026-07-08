import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from .config_loader import save_searches
from .ebay.client import EbayClient
from .llm import OllamaClient
from .models import Search, Verdict
from .notifier import PushoverNotifier
from .store import Store

log = structlog.get_logger()


@dataclasses.dataclass
class Deps:
    ebay: EbayClient
    store: Store
    notifier: PushoverNotifier
    ollama: OllamaClient


@dataclasses.dataclass
class SearchStatus:
    """Runtime state for one search loop, reset on process restart."""

    running: bool = False
    last_poll_at: str | None = None
    next_poll_at: str | None = None
    last_result_count: int | None = None
    new_items_total: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _should_notify(search: Search, verdict: Verdict | None) -> bool:
    """Decide whether to push a Pushover notification for this hit."""
    if verdict is None:
        # No LLM configured — always notify
        return True
    if not search.llm or not search.llm.enabled:
        return True
    if verdict.recommend in search.llm.skip_verdicts:
        return False
    if verdict.score < search.llm.min_score_to_notify:
        return False
    return True


async def _tick(search: Search, deps: Deps, status: SearchStatus) -> None:
    """Run one poll cycle for a search."""
    status.last_poll_at = datetime.now(timezone.utc).isoformat()
    try:
        listings = await deps.ebay.search(search)
    except Exception as exc:
        log.error("ebay search failed", search_id=search.id, error=str(exc))
        status.last_error = str(exc)
        return

    status.last_error = None
    status.last_result_count = len(listings)

    if not listings:
        log.debug("no results", search_id=search.id)
        return

    item_ids = [listing.item_id for listing in listings]
    new_ids = await deps.store.filter_unseen(search.id, item_ids)

    # Keep the stored price of previously-seen auctions in step with their
    # live high bid, even when no new items showed up this poll.
    auction_prices = [
        (l.item_id, float(l.display_price)) for l in listings if l.is_auction
    ]
    await deps.store.raise_auction_prices(search.id, auction_prices)

    if not new_ids:
        log.debug("no new items", search_id=search.id, checked=len(listings))
        return

    new_set = set(new_ids)
    new_listings = [l for l in listings if l.item_id in new_set]
    status.new_items_total += len(new_listings)
    log.info("new items found", search_id=search.id, count=len(new_listings))

    for listing in new_listings:
        verdict: Verdict | None = None

        if search.llm and search.llm.enabled:
            verdict = await deps.ollama.analyse(listing, search.llm)

        notify = _should_notify(search, verdict)

        if notify:
            await deps.notifier.send(search, listing, verdict)

        await deps.store.record_hit(
            search_id=search.id,
            item_id=listing.item_id,
            title=listing.title,
            price=float(listing.display_price),
            url=listing.item_web_url,
            buying_options=listing.buying_options,
            verdict=verdict.recommend if verdict else None,
            score=verdict.score if verdict else None,
            notified=notify,
        )


class SearchManager:
    """
    Owns the search list and one polling task per enabled search.

    All mutations (add/update/remove) persist the full list back to
    searches.yaml and reconcile the running task for that search, so the
    web UI can change searches without a restart. Single-threaded within
    the event loop — no locking needed as long as mutators are awaited.
    """

    def __init__(self, searches: list[Search], deps: Deps, searches_file: str | Path):
        self._searches: dict[str, Search] = {s.id: s for s in searches}
        self._deps = deps
        self._searches_file = searches_file
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._status: dict[str, SearchStatus] = {
            s.id: SearchStatus() for s in searches
        }

    # ── Read side ──────────────────────────────────────────────────────────

    def searches(self) -> list[Search]:
        return list(self._searches.values())

    def get(self, search_id: str) -> Search | None:
        return self._searches.get(search_id)

    def status(self, search_id: str) -> SearchStatus:
        return self._status.setdefault(search_id, SearchStatus())

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start_all(self) -> None:
        for search in self._searches.values():
            self._start_task(search)
        log.info(
            "scheduler started",
            search_count=len(self._searches),
            active=len(self._tasks),
        )

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── Mutations (persist + reconcile task) ───────────────────────────────

    async def add(self, search: Search) -> None:
        if search.id in self._searches:
            raise ValueError(f"Search id '{search.id}' already exists")
        self._searches[search.id] = search
        self._status[search.id] = SearchStatus()
        self._persist()
        self._start_task(search)

    async def update(self, search: Search) -> None:
        if search.id not in self._searches:
            raise KeyError(search.id)
        await self._stop_task(search.id)
        self._searches[search.id] = search
        self._persist()
        self._start_task(search)

    async def remove(self, search_id: str) -> None:
        if search_id not in self._searches:
            raise KeyError(search_id)
        await self._stop_task(search_id)
        del self._searches[search_id]
        self._status.pop(search_id, None)
        self._persist()

    def trigger(self, search_id: str) -> None:
        """Wake the search's loop for an immediate poll."""
        if search_id not in self._searches:
            raise KeyError(search_id)
        if search_id not in self._tasks:
            raise ValueError(f"Search '{search_id}' is not running (disabled?)")
        self._wake[search_id].set()

    # ── Internals ──────────────────────────────────────────────────────────

    def _persist(self) -> None:
        save_searches(self._searches_file, self.searches())

    def _start_task(self, search: Search) -> None:
        if not search.enabled or search.id in self._tasks:
            return
        self._wake[search.id] = asyncio.Event()
        self._tasks[search.id] = asyncio.create_task(
            self._search_loop(search), name=f"search-{search.id}"
        )

    async def _stop_task(self, search_id: str) -> None:
        task = self._tasks.pop(search_id, None)
        self._wake.pop(search_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.status(search_id).running = False

    async def _search_loop(self, search: Search) -> None:
        status = self.status(search.id)
        status.running = True
        wake = self._wake[search.id]
        log.info(
            "search loop started",
            search_id=search.id,
            name=search.name,
            interval_s=search.poll_interval_seconds,
        )
        while True:
            await _tick(search, self._deps, status)
            status.next_poll_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=search.poll_interval_seconds)
            ).isoformat()
            try:
                async with asyncio.timeout(search.poll_interval_seconds):
                    await wake.wait()
            except TimeoutError:
                pass
            wake.clear()

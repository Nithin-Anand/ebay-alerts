import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from .config_loader import save_searches
from .ebay.client import EbayClient, end_time_passed
from .llm import OllamaClient
from .models import Listing, Search, Verdict
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


async def _fetch_over_ceiling(
    search: Search, deps: Deps, present_ids: set[str]
) -> list[Listing]:
    """
    Re-query the price band above a search's price_max to catch auctions whose
    bid has climbed out of the capped results but are still live on eBay.

    To avoid wasting API calls, this only fires when the search has a ceiling AND
    one of its still-active auction hits is missing from the normal results
    (`present_ids`) — i.e. something may actually have dropped out of range. When
    every tracked auction is still present there is nothing to refresh, so we
    skip the extra call.

    Returns [] when there's nothing to do, or on error — this is best-effort
    price upkeep, so a failure here must never disrupt the main poll. Sorted
    price-ascending so items just over the ceiling (the ones most likely to have
    only recently departed the results) come first within the fetch limit.
    """
    if search.filters.price_max is None:
        return []
    tracked = await deps.store.active_auction_item_ids(search.id)
    if not tracked - present_ids:
        return []  # every tracked auction is still in the capped results
    over = search.model_copy(
        update={
            "filters": search.filters.model_copy(
                update={"price_min": search.filters.price_max, "price_max": None}
            ),
            "sort": "price_asc",
        }
    )
    try:
        return await deps.ebay.search(over)
    except Exception as exc:
        log.warning("over-ceiling refresh failed", search_id=search.id, error=str(exc))
        return []


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

    # An auction whose bid climbs above the search's price_max drops out of the
    # price-capped results, so eBay stops returning it here. Re-query the band
    # above the ceiling to catch such items while they're still live and keep
    # their stored price / liveness in step. This supplementary search only ever
    # updates already-recorded hits — it never creates hits or notifications.
    over_listings = await _fetch_over_ceiling(
        search, deps, present_ids={l.item_id for l in listings}
    )
    live_listings = listings + over_listings

    if live_listings:
        # Keep the stored price of previously-seen auctions in step with their
        # live high bid, even when no new items showed up this poll.
        auction_prices = [
            (search.id, l.item_id, float(l.display_price))
            for l in live_listings
            if l.is_auction
        ]
        await deps.store.raise_auction_prices(auction_prices)

        # Everything returned is provably live: refresh check time (so the
        # pruner deprioritises them) and revive any that were wrongly archived.
        present_keys = [(search.id, l.item_id) for l in live_listings]
        await deps.store.mark_checked(present_keys)
        await deps.store.unarchive_hits(present_keys)

        # Capture/refresh each auction's end time while we can see it, so it can
        # still be archived after it ends and disappears from every result set.
        await deps.store.set_end_times(
            [(search.id, l.item_id, l.end_time)
             for l in live_listings if l.is_auction and l.end_time]
        )

    # Archive auctions whose recorded end time has passed. eBay stops returning
    # ended auctions, so this is the search-only stand-in for the (unavailable)
    # getItems liveness check. Runs every poll, even when there are no results.
    ended = [
        (search.id, iid)
        for iid, end in await deps.store.auction_end_times(search.id)
        if end_time_passed(end)
    ]
    if ended:
        archived = await deps.store.archive_hits(ended)
        if archived:
            log.info("archived ended auctions", search_id=search.id, count=archived)

    if not listings:
        log.debug("no results", search_id=search.id)
        return

    item_ids = [listing.item_id for listing in listings]
    new_ids = await deps.store.filter_unseen(search.id, item_ids)

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
            end_time=listing.end_time,
        )


class SearchManager:
    """
    Owns the search list and one polling task per enabled search.

    All mutations (add/update/remove) persist the full list back to
    searches.yaml and reconcile the running task for that search, so the
    web UI can change searches without a restart. Single-threaded within
    the event loop — no locking needed as long as mutators are awaited.
    """

    def __init__(
        self,
        searches: list[Search],
        deps: Deps,
        searches_file: str | Path,
        *,
        prune_enabled: bool = True,
        prune_interval_seconds: int = 3600,
        prune_batch_size: int = 200,
    ):
        self._searches: dict[str, Search] = {s.id: s for s in searches}
        self._deps = deps
        self._searches_file = searches_file
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._status: dict[str, SearchStatus] = {
            s.id: SearchStatus() for s in searches
        }
        self._prune_enabled = prune_enabled
        self._prune_interval = prune_interval_seconds
        self._prune_batch = prune_batch_size
        self._prune_task: asyncio.Task | None = None

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
        if self._prune_enabled:
            self._prune_task = asyncio.create_task(self._prune_loop(), name="prune")
        log.info(
            "scheduler started",
            search_count=len(self._searches),
            active=len(self._tasks),
            pruning=self._prune_enabled,
        )

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        if self._prune_task:
            tasks.append(self._prune_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._prune_task = None

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

    async def rerun_analysis(self, search_id: str, item_id: str) -> dict:
        """
        Re-fetch a previously seen listing and run LLM analysis on it again,
        overwriting the stored verdict. Useful when the first analysis failed
        (e.g. Ollama was unreachable). Sends a notification only if the item
        was not already notified and the fresh verdict now warrants one.

        Raises KeyError if the search or hit is unknown, ValueError if the
        search has no active LLM config or the listing is no longer on eBay.
        """
        search = self._searches.get(search_id)
        if search is None:
            raise KeyError(search_id)
        if not (search.llm and search.llm.enabled):
            raise ValueError("LLM analysis is not enabled for this search")

        hit = await self._deps.store.get_hit(search_id, item_id)
        if hit is None:
            raise KeyError(item_id)

        listing = await self._deps.ebay.get_item(item_id)
        if listing is None:
            raise ValueError("Listing is no longer available on eBay")

        verdict = await self._deps.ollama.analyse(listing, search.llm)

        already_notified = bool(hit["notified"])
        notify = not already_notified and _should_notify(search, verdict)
        if notify:
            await self._deps.notifier.send(search, listing, verdict)

        await self._deps.store.update_verdict(
            search_id=search_id,
            item_id=item_id,
            verdict=verdict.recommend,
            score=verdict.score,
            notified=already_notified or notify,
        )
        log.info(
            "reanalysed hit",
            search_id=search_id,
            item_id=item_id,
            recommend=verdict.recommend,
            score=verdict.score,
            notified=already_notified or notify,
        )
        return {
            "verdict": verdict.recommend,
            "score": verdict.score,
            "notified": already_notified or notify,
        }

    async def prune_now(self) -> int:
        """Run one liveness sweep immediately and return how many were archived."""
        return await self._prune_once()

    # ── Internals ──────────────────────────────────────────────────────────

    async def _prune_loop(self) -> None:
        log.info(
            "prune loop started",
            interval_s=self._prune_interval,
            batch=self._prune_batch,
        )
        while True:
            try:
                await self._prune_once()
            except Exception as exc:
                log.error("prune failed", error=str(exc))
            await asyncio.sleep(self._prune_interval)

    async def _prune_once(self) -> int:
        """
        Verify the least-recently-checked hits against eBay and archive the ones
        that are gone or ended. Live ones just have their check time refreshed.
        """
        keys = await self._deps.store.hits_to_check(self._prune_batch)
        if not keys:
            return 0

        # The same item can be shared by two searches — verify each id once.
        statuses = await self._deps.ebay.get_items(
            list({iid for _, iid in keys})
        )
        gone = [(sid, iid) for sid, iid in keys if statuses.get(iid) is None]
        alive = [(sid, iid) for sid, iid in keys if statuses.get(iid) is not None]

        if alive:
            await self._deps.store.mark_checked(alive)
            # The item is still live but may have dropped out of its search
            # results (e.g. an auction bid climbed past the search's price_max).
            # The pruner is the only place that still sees its price, so bump it
            # here to keep the stored/displayed figure in step with the bid.
            alive_prices = [
                (sid, iid, float(statuses[iid].display_price))
                for sid, iid in alive
                if statuses[iid].is_auction
            ]
            await self._deps.store.raise_auction_prices(alive_prices)
        archived = await self._deps.store.archive_hits(gone) if gone else 0
        log.info("prune complete", checked=len(keys), archived=archived)
        return archived

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

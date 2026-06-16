import asyncio
import dataclasses

import structlog

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


async def _tick(search: Search, deps: Deps) -> None:
    """Run one poll cycle for a search."""
    try:
        listings = await deps.ebay.search(search)
    except Exception as exc:
        log.error("ebay search failed", search_id=search.id, error=str(exc))
        return

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
            price=float(listing.price),
            url=listing.item_web_url,
            verdict=verdict.recommend if verdict else None,
            score=verdict.score if verdict else None,
            notified=notify,
        )


async def _search_loop(search: Search, deps: Deps) -> None:
    log.info(
        "search loop started",
        search_id=search.id,
        name=search.name,
        interval_s=search.poll_interval_seconds,
    )
    while True:
        await _tick(search, deps)
        await asyncio.sleep(search.poll_interval_seconds)


async def run_all(searches: list[Search], deps: Deps) -> None:
    """Run all search loops concurrently. Blocks until cancelled."""
    async with asyncio.TaskGroup() as tg:
        for search in searches:
            tg.create_task(_search_loop(search, deps), name=f"search-{search.id}")

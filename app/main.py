"""
eBay Alerts — main entrypoint.

Normal run:
    python -m app.main

Smoke-test modes (each exits after completing):
    python -m app.main --test-pushover   Send a test Pushover notification
    python -m app.main --test-ebay       Fetch an eBay OAuth token and print the prefix
    python -m app.main --test-llm        Run a synthetic listing through Ollama and print the verdict
"""

import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

import structlog

from .config_loader import load_searches
from .ebay.auth import EbayAuth
from .ebay.client import EbayClient
from .llm import OllamaClient
from .notifier import PushoverNotifier
from .scheduler import Deps, run_all
from .settings import Settings
from .store import Store


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()


async def main() -> None:
    settings = Settings()
    _configure_logging(settings.log_level)

    if len(sys.argv) > 1:
        await _handle_cli(sys.argv[1], settings)
        return

    log.info("ebay-alerts starting", searches_file=settings.searches_file)

    searches = load_searches(settings.searches_file)
    if not searches:
        log.error("no searches defined — add at least one entry to searches.yaml")
        sys.exit(1)

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir / "state.sqlite")
    await store.open()

    auth = EbayAuth(settings.ebay_client_id, settings.ebay_client_secret)
    ebay = EbayClient(auth)
    ollama = OllamaClient(settings.ollama_url, settings.ollama_model)
    notifier = PushoverNotifier(settings.pushover_token, settings.pushover_user)

    deps = Deps(ebay=ebay, store=store, notifier=notifier, ollama=ollama)

    log.info("scheduler starting", search_count=len(searches))
    try:
        await run_all(searches, deps)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("shutting down")
    finally:
        await ebay.aclose()
        await ollama.aclose()
        await notifier.aclose()
        await store.close()


async def _handle_cli(command: str, settings: Settings) -> None:
    _configure_logging(settings.log_level)

    if command == "--test-pushover":
        notifier = PushoverNotifier(settings.pushover_token, settings.pushover_user)
        try:
            await notifier.send_test()
            print("Pushover test notification sent successfully.")
        finally:
            await notifier.aclose()

    elif command == "--test-ebay":
        auth = EbayAuth(settings.ebay_client_id, settings.ebay_client_secret)
        token = await auth.get_token()
        print(f"eBay auth OK — token starts with: {token[:12]}...")

    elif command == "--test-llm":
        from .models import LlmConfig, Listing

        ollama = OllamaClient(settings.ollama_url, settings.ollama_model)
        listing = Listing(
            item_id="test-001",
            title="Canon EF 50mm f/1.8 STM Lens",
            price=Decimal("45.00"),
            condition="Used",
            buying_options=["FIXED_PRICE"],
            item_web_url="https://www.ebay.co.uk/itm/000000000001",
            short_description="Excellent condition. Clean glass, no fungus or scratches. Smooth AF.",
            seller_username="testselleruk",
            seller_feedback_percentage="99.8",
        )
        config = LlmConfig(
            criteria=(
                "This is a camera lens. Recommend BID only if the optics are described "
                "as clean with no fungus, haze, or scratches. "
                "Flag: as-is, for parts, untested, haze, fungus, fog, scratches, stiff."
            ),
        )
        try:
            verdict = await ollama.analyse(listing, config)
            print(f"Verdict: {verdict.model_dump_json(indent=2)}")
        finally:
            await ollama.aclose()

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

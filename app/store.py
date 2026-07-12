from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

log = structlog.get_logger()

_CREATE_SEEN = """
CREATE TABLE IF NOT EXISTS seen_items (
    search_id     TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (search_id, item_id)
)
"""

_CREATE_HITS = """
CREATE TABLE IF NOT EXISTS hits (
    search_id      TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    title          TEXT,
    price          REAL,
    buying_options TEXT,
    url            TEXT,
    verdict        TEXT,
    score          INTEGER,
    notified       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (search_id, item_id)
)
"""

# Columns added after the initial schema. Applied on open() so existing
# databases pick them up without a manual migration.
_MIGRATIONS = [
    ("hits", "buying_options", "TEXT"),
    # Liveness tracking: when a hit was last verified against eBay, and when it
    # was archived after being confirmed no longer active (NULL = still live).
    ("hits", "last_checked_at", "TEXT"),
    ("hits", "archived_at", "TEXT"),
    # Auction end time (ISO 8601), captured from eBay results so an ended
    # auction can be archived once it vanishes from search — the search-only
    # substitute for the getItems liveness check.
    ("hits", "end_time", "TEXT"),
]


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        # WAL mode allows concurrent readers alongside the writer
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_SEEN)
        await self._db.execute(_CREATE_HITS)
        await self._apply_migrations()
        await self._db.commit()
        log.info("store opened", path=self._path)

    async def _apply_migrations(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, column, ddl in _MIGRATIONS:
            async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] async for row in cursor}
            if column not in existing:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
                log.info("store migrated", table=table, column=column)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def filter_unseen(self, search_id: str, item_ids: list[str]) -> list[str]:
        """
        Return the subset of item_ids that have not been seen for this search,
        and atomically mark them as seen so a restart cannot double-notify.

        The insertion happens BEFORE notification so a crash between seen-insert
        and notify means one missed alert (acceptable) rather than a duplicate.
        """
        if not item_ids:
            return []

        placeholders = ",".join("?" * len(item_ids))
        async with self._db.execute(
            f"SELECT item_id FROM seen_items "
            f"WHERE search_id = ? AND item_id IN ({placeholders})",
            [search_id, *item_ids],
        ) as cursor:
            already_seen = {row[0] async for row in cursor}

        new_ids = [iid for iid in item_ids if iid not in already_seen]

        if new_ids:
            now = datetime.now(timezone.utc).isoformat()
            await self._db.executemany(
                "INSERT OR IGNORE INTO seen_items (search_id, item_id, first_seen_at) "
                "VALUES (?, ?, ?)",
                [(search_id, iid, now) for iid in new_ids],
            )
            await self._db.commit()

        return new_ids

    async def record_hit(
        self,
        *,
        search_id: str,
        item_id: str,
        title: str,
        price: float,
        url: str,
        buying_options: list[str] | None = None,
        verdict: str | None = None,
        score: int | None = None,
        notified: bool = False,
        end_time: str | None = None,
    ) -> None:
        """Write a full hit record to the hits table for audit / retrospective queries."""
        now = datetime.now(timezone.utc).isoformat()
        options = ",".join(buying_options) if buying_options else None
        # A just-recorded hit was live moments ago, so seed last_checked_at to
        # keep the pruner from immediately re-verifying it.
        await self._db.execute(
            """
            INSERT OR REPLACE INTO hits
              (search_id, item_id, title, price, buying_options, url,
               verdict, score, notified, created_at, last_checked_at, end_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (search_id, item_id, title, price, options, url,
             verdict, score, int(notified), now, now, end_time),
        )
        await self._db.commit()

    async def set_end_times(self, rows: list[tuple[str, str, str]]) -> None:
        """
        Record/refresh the auction end time on existing hits. Each row is
        (search_id, item_id, end_time). Only rows already present are touched,
        so this safely backfills end times for hits recorded before the item's
        end time was being captured.
        """
        if not rows:
            return
        await self._db.executemany(
            "UPDATE hits SET end_time = ? WHERE search_id = ? AND item_id = ?",
            [(end_time, sid, iid) for sid, iid, end_time in rows],
        )
        await self._db.commit()

    async def auction_end_times(self, search_id: str) -> list[tuple[str, str]]:
        """
        (item_id, end_time) for this search's still-active auction hits that have
        a recorded end time — the caller archives the ones whose auction has ended.
        """
        async with self._db.execute(
            "SELECT item_id, end_time FROM hits "
            "WHERE search_id = ? AND archived_at IS NULL "
            "AND buying_options LIKE '%AUCTION%' AND end_time IS NOT NULL",
            (search_id,),
        ) as cursor:
            return [(row[0], row[1]) async for row in cursor]

    async def raise_auction_prices(
        self, prices: list[tuple[str, str, float]]
    ) -> None:
        """
        Bump the stored price of already-recorded auction hits when their
        current bid has risen. Each entry is (search_id, item_id,
        current_bid_price); only rows whose recorded price is lower are
        updated, so an unchanged or lower bid is a no-op.

        Called both from the poll loop (for items still in search results) and
        the pruner (for items that dropped out of results, e.g. because their
        bid rose above the search's price_max but the auction is still live).
        """
        if not prices:
            return
        await self._db.executemany(
            "UPDATE hits SET price = ? "
            "WHERE search_id = ? AND item_id = ? AND price < ?",
            [(price, sid, iid, price) for sid, iid, price in prices],
        )
        await self._db.commit()

    async def active_auction_item_ids(self, search_id: str) -> set[str]:
        """
        Item ids of this search's still-active auction hits. Used to decide
        whether the above-ceiling refresh query is worth making: if one of these
        is missing from the normal results, an auction may have outgrown the
        price range, so re-query for it — otherwise skip the extra API call.
        """
        async with self._db.execute(
            "SELECT item_id FROM hits "
            "WHERE search_id = ? AND archived_at IS NULL "
            "AND buying_options LIKE '%AUCTION%'",
            (search_id,),
        ) as cursor:
            return {row[0] async for row in cursor}

    async def delete_hits(self, keys: list[tuple[str, str]]) -> int:
        """
        Delete hit rows by (search_id, item_id) and return how many were
        removed. Only touches the audit/display table — seen_items is left
        intact, so a still-live deleted item is not re-notified.
        """
        if not keys:
            return 0
        cursor = await self._db.executemany(
            "DELETE FROM hits WHERE search_id = ? AND item_id = ?", keys
        )
        await self._db.commit()
        return cursor.rowcount

    async def hits_to_check(self, limit: int) -> list[tuple[str, str]]:
        """
        Return up to `limit` non-archived hit keys, least-recently-verified
        first (never-checked rows come first). Drives the pruner's round-robin
        so a large table is swept over several cycles rather than all at once.
        """
        async with self._db.execute(
            "SELECT search_id, item_id FROM hits "
            "WHERE archived_at IS NULL "
            # `last_checked_at IS NOT NULL` is 0 for NULLs, so they sort first.
            "ORDER BY last_checked_at IS NOT NULL, last_checked_at ASC "
            "LIMIT ?",
            (limit,),
        ) as cursor:
            return [(row[0], row[1]) async for row in cursor]

    async def mark_checked(
        self, keys: list[tuple[str, str]], ts: str | None = None
    ) -> None:
        """Record that these hits were verified live at `ts` (default now)."""
        if not keys:
            return
        ts = ts or datetime.now(timezone.utc).isoformat()
        await self._db.executemany(
            "UPDATE hits SET last_checked_at = ? WHERE search_id = ? AND item_id = ?",
            [(ts, sid, iid) for sid, iid in keys],
        )
        await self._db.commit()

    async def archive_hits(
        self, keys: list[tuple[str, str]], ts: str | None = None
    ) -> int:
        """
        Flag hits as archived (no longer active on eBay), keeping the row for
        history. Only touches currently-active rows; returns how many changed.
        """
        if not keys:
            return 0
        ts = ts or datetime.now(timezone.utc).isoformat()
        cursor = await self._db.executemany(
            "UPDATE hits SET archived_at = ?, last_checked_at = ? "
            "WHERE search_id = ? AND item_id = ? AND archived_at IS NULL",
            [(ts, ts, sid, iid) for sid, iid in keys],
        )
        await self._db.commit()
        return cursor.rowcount

    async def unarchive_hits(self, keys: list[tuple[str, str]]) -> int:
        """Clear the archived flag, e.g. when a listing reappears in results."""
        if not keys:
            return 0
        cursor = await self._db.executemany(
            "UPDATE hits SET archived_at = NULL "
            "WHERE search_id = ? AND item_id = ? AND archived_at IS NOT NULL",
            keys,
        )
        await self._db.commit()
        return cursor.rowcount

    async def get_hit(self, search_id: str, item_id: str) -> dict | None:
        """Fetch a single hit row by its composite key, or None if absent."""
        async with self._db.execute(
            "SELECT search_id, item_id, title, price, buying_options, url, "
            "verdict, score, notified, created_at FROM hits "
            "WHERE search_id = ? AND item_id = ?",
            (search_id, item_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))

    async def update_verdict(
        self,
        *,
        search_id: str,
        item_id: str,
        verdict: str | None,
        score: int | None,
        notified: bool,
    ) -> bool:
        """
        Overwrite the LLM verdict/score (and notified flag) on an existing
        hit, e.g. after re-running analysis. Returns True if a row matched.
        """
        cursor = await self._db.execute(
            "UPDATE hits SET verdict = ?, score = ?, notified = ? "
            "WHERE search_id = ? AND item_id = ?",
            (verdict, score, int(notified), search_id, item_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def recent_hits(
        self,
        search_id: str | None = None,
        limit: int = 100,
        archived: bool | None = None,
    ) -> list[dict]:
        """
        Most recent hits, newest first, optionally scoped to one search.
        `archived`: None = all, False = only active, True = only archived.
        """
        query = (
            "SELECT search_id, item_id, title, price, buying_options, url, "
            "verdict, score, notified, created_at, archived_at FROM hits"
        )
        conditions: list[str] = []
        params: list = []
        if search_id is not None:
            conditions.append("search_id = ?")
            params.append(search_id)
        if archived is True:
            conditions.append("archived_at IS NOT NULL")
        elif archived is False:
            conditions.append("archived_at IS NULL")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._db.execute(query, params) as cursor:
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) async for row in cursor]

    async def hit_stats(self) -> dict[str, dict]:
        """Per-search hit counts: {search_id: {total, notified, last_hit_at}}."""
        async with self._db.execute(
            "SELECT search_id, COUNT(*), SUM(notified), MAX(created_at) "
            "FROM hits GROUP BY search_id"
        ) as cursor:
            return {
                row[0]: {
                    "total": row[1],
                    "notified": row[2] or 0,
                    "last_hit_at": row[3],
                }
                async for row in cursor
            }

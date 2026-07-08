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
    ) -> None:
        """Write a full hit record to the hits table for audit / retrospective queries."""
        now = datetime.now(timezone.utc).isoformat()
        options = ",".join(buying_options) if buying_options else None
        await self._db.execute(
            """
            INSERT OR REPLACE INTO hits
              (search_id, item_id, title, price, buying_options, url,
               verdict, score, notified, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (search_id, item_id, title, price, options, url,
             verdict, score, int(notified), now),
        )
        await self._db.commit()

    async def raise_auction_prices(
        self, search_id: str, prices: list[tuple[str, float]]
    ) -> None:
        """
        Bump the stored price of already-recorded auction hits when their
        current bid has risen. Each entry is (item_id, current_bid_price);
        only rows whose recorded price is lower are updated, so an unchanged
        or lower bid is a no-op.
        """
        if not prices:
            return
        await self._db.executemany(
            "UPDATE hits SET price = ? "
            "WHERE search_id = ? AND item_id = ? AND price < ?",
            [(price, search_id, item_id, price) for item_id, price in prices],
        )
        await self._db.commit()

    async def recent_hits(
        self, search_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Most recent hits, newest first, optionally scoped to one search."""
        query = (
            "SELECT search_id, item_id, title, price, buying_options, url, "
            "verdict, score, notified, created_at FROM hits"
        )
        params: list = []
        if search_id is not None:
            query += " WHERE search_id = ?"
            params.append(search_id)
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

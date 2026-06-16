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
    search_id  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    title      TEXT,
    price      REAL,
    url        TEXT,
    verdict    TEXT,
    score      INTEGER,
    notified   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (search_id, item_id)
)
"""


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
        await self._db.commit()
        log.info("store opened", path=self._path)

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
        verdict: str | None = None,
        score: int | None = None,
        notified: bool = False,
    ) -> None:
        """Write a full hit record to the hits table for audit / retrospective queries."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO hits
              (search_id, item_id, title, price, url, verdict, score, notified, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (search_id, item_id, title, price, url, verdict, score, int(notified), now),
        )
        await self._db.commit()

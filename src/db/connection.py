from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """
    Small async SQLite wrapper.

    Design:
    - One connection per process (simple, reliable for bot workloads)
    - WAL enabled for better concurrency
    - Foreign keys enforced
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database connection is not initialized. Call connect() first.")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return

        logger.info("Connecting to SQLite: %s", self._db_path)
        self._conn = await aiosqlite.connect(self._db_path.as_posix())

        # Return rows that can be accessed like dicts: row["column"]
        self._conn.row_factory = aiosqlite.Row

        # Pragmas: good defaults for bot workloads
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout = 5000;")  # ms
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        logger.info("Closing SQLite connection")
        await self._conn.close()
        self._conn = None

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """
        Execute a statement and commit immediately.
        Use transaction() for batching multiple statements.
        """
        if params is None:
            params = ()
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, seq_of_params)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> aiosqlite.Row | None:
        if params is None:
            params = ()
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[aiosqlite.Row]:
        if params is None:
            params = ()
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return list(rows)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Transaction context manager.

        Uses connection.commit()/rollback() (safe with executescript),
        instead of executing COMMIT/ROLLBACK SQL directly.
        """
        try:
            await self.conn.execute("BEGIN;")
            yield self.conn
        except Exception:
            await self.conn.rollback()
            raise
        else:
            await self.conn.commit()

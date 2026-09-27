"""SQLite storage for TradingCow. Separate file from the arcade database."""
from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_1h (
    item_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    avg_high INTEGER,
    avg_low INTEGER,
    high_vol INTEGER,
    low_vol INTEGER,
    PRIMARY KEY (item_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_1h_ts ON price_1h(ts);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    below INTEGER NOT NULL,
    price INTEGER NOT NULL,
    net INTEGER NOT NULL DEFAULT 0,
    use_high INTEGER NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_alerts (
    key TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

KEEP_DAYS = 9


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path.as_posix())
        await self.conn.execute("PRAGMA journal_mode = WAL;")
        await self.conn.execute("PRAGMA synchronous = NORMAL;")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ---- key/value
    async def get(self, key: str, default: str | None = None) -> str | None:
        async with self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

    async def set(self, key: str, value: str) -> None:
        await self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    # ---- hourly prices
    async def hours_stored(self, since: int) -> set[int]:
        async with self.conn.execute("SELECT DISTINCT ts FROM price_1h WHERE ts >= ?", (since,)) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def add_hour(self, ts: int, data: dict) -> int:
        rows = []
        for k, v in data.items():
            if not (v.get("avgLowPrice") or v.get("avgHighPrice")):
                continue
            rows.append((int(k), ts, v.get("avgHighPrice"), v.get("avgLowPrice"), v.get("highPriceVolume") or 0, v.get("lowPriceVolume") or 0))
        await self.conn.executemany("INSERT OR REPLACE INTO price_1h VALUES (?,?,?,?,?,?)", rows)
        await self.conn.execute("DELETE FROM price_1h WHERE ts < ?", (int(time.time()) - KEEP_DAYS * 86400,))
        await self.conn.commit()
        return len(rows)

    async def history(self, since: int, item_id: int | None = None) -> dict[int, list[tuple]]:
        sql = "SELECT item_id, ts, avg_high, avg_low, high_vol, low_vol FROM price_1h WHERE ts >= ?"
        args: tuple = (since,)
        if item_id is not None:
            sql += " AND item_id = ?"
            args = (since, item_id)
        out: dict[int, list[tuple]] = defaultdict(list)
        async with self.conn.execute(sql, args) as cur:
            async for r in cur:
                out[r[0]].append((r[1], r[2], r[3], r[4], r[5]))
        return out

    # ---- alert de-duplication
    async def once(self, key: str, cooldown_s: int) -> bool:
        """True if this alert key has not fired within the cooldown (and records it)."""
        now = int(time.time())
        async with self.conn.execute("SELECT ts FROM sent_alerts WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        if row and now - row[0] < cooldown_s:
            return False
        await self.conn.execute("INSERT OR REPLACE INTO sent_alerts VALUES (?,?)", (key, now))
        await self.conn.execute("DELETE FROM sent_alerts WHERE ts < ?", (now - 14 * 86400,))
        await self.conn.commit()
        return True

    # ---- watches
    async def add_watch(self, user_id: int, channel_id: int | None, item_id: int, item_name: str, below: bool,
                        price: int, net: bool, use_high: bool) -> int:
        cur = await self.conn.execute(
            "INSERT INTO watches(user_id, channel_id, item_id, item_name, below, price, net, use_high, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, channel_id, item_id, item_name, int(below), price, int(net), int(use_high), int(time.time())))
        await self.conn.commit()
        return cur.lastrowid

    async def watches(self, user_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM watches" + (" WHERE user_id=?" if user_id is not None else "") + " ORDER BY id"
        async with self.conn.execute(sql, (user_id,) if user_id is not None else ()) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]

    async def remove_watch(self, user_id: int, watch_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM watches WHERE id=? AND user_id=?", (watch_id, user_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_fired(self, watch_id: int, fired: bool) -> None:
        await self.conn.execute("UPDATE watches SET fired=? WHERE id=?", (int(fired), watch_id))
        await self.conn.commit()

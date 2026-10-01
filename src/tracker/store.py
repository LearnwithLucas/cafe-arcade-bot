"""SQLite storage for Tracker (its own file, next to the arcade database)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    handle TEXT, title TEXT, lang TEXT,
    own_key TEXT,               -- 'en' / 'nl' for your channels, NULL for competitors
    uploads TEXT,
    added INTEGER
);
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT, title TEXT,
    published INTEGER, duration INTEGER, is_short INTEGER,
    announced INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
    video_id TEXT, ts INTEGER, views INTEGER, likes INTEGER, comments INTEGER,
    PRIMARY KEY (video_id, ts)
);
CREATE TABLE IF NOT EXISTS milestones (
    video_id TEXT, milestone TEXT, views INTEGER, ts INTEGER,
    PRIMARY KEY (video_id, milestone)
);
CREATE TABLE IF NOT EXISTS channel_stats (
    channel_id TEXT, ts INTEGER, subs INTEGER, views INTEGER, videos INTEGER,
    PRIMARY KEY (channel_id, ts)
);
CREATE TABLE IF NOT EXISTS watchlist (
    kind TEXT, handle TEXT, lang TEXT,
    PRIMARY KEY (kind, handle)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, arg TEXT, created INTEGER, message_id INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS yt_stats (       -- YouTube Studio numbers for a fixed window after publishing
    video_id TEXT, win TEXT, views INTEGER, subs INTEGER, ext_pct REAL, ts INTEGER,
    PRIMARY KEY (video_id, win)
);
CREATE TABLE IF NOT EXISTS tags (           -- codes set by hand with /tag (platform: yt, tt, ig)
    platform TEXT, post_id TEXT, code TEXT, ts INTEGER,
    PRIMARY KEY (platform, post_id)
);
CREATE TABLE IF NOT EXISTS changes (        -- the one change for each week, set with /change
    week TEXT PRIMARY KEY, text TEXT, ts INTEGER
);
"""

MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN code TEXT",
    "ALTER TABLE videos ADD COLUMN kind TEXT",
]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path.as_posix())
        await self.db.execute("PRAGMA journal_mode = WAL;")
        await self.db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                await self.db.execute(sql)
            except aiosqlite.OperationalError:
                pass  # column already there
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.db.execute(sql, args) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]

    async def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = await self.rows(sql, args)
        return r[0] if r else None

    async def run(self, sql: str, args: tuple = ()) -> int:
        cur = await self.db.execute(sql, args)
        await self.db.commit()
        return cur.lastrowid

    async def get(self, key: str, default=None):
        r = await self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    async def set(self, key: str, value) -> None:
        await self.run("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    async def snapshot(self, video_id: str, views: int, likes: int, comments: int, min_gap: int = 50 * 60) -> None:
        last = await self.one("SELECT ts FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (video_id,))
        now = int(time.time())
        if last and now - last["ts"] < min_gap:
            return
        await self.run("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)", (video_id, now, views, likes, comments))

    async def views_at(self, video_id: str, ts: int) -> int | None:
        """Views from the snapshot closest to (at or before) a moment."""
        r = await self.one("SELECT views FROM snapshots WHERE video_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (video_id, ts))
        return r["views"] if r else None

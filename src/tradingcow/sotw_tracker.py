"""Backup XP tracker for Skill of the Week, using the official OSRS hiscores instead of Wise Old Man.

Every 30 minutes the bot reads each participant's XP for the skill from the official hiscores and stores it.
Gained XP = latest snapshot minus the first snapshot taken after the competition started.
When Wise Old Man is down, the board is built from these snapshots instead.

Like Wise Old Man, the official hiscores only change when a player logs out or hops worlds.
Players below the hiscores cut-off for the skill show as unranked and can't be tracked this way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger("tradingcow.sotw.tracker")

HISCORES = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player="
SEED = Path(__file__).with_name("sotw_seed.json")
PACE = 2.0  # seconds between lookups: about 9 minutes for 170 players

SCHEMA = """
CREATE TABLE IF NOT EXISTS sotw_xp (
    competition INTEGER NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
    ts INTEGER NOT NULL,
    xp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sotw_xp ON sotw_xp(competition, username, ts);
"""


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Tracker:
    def __init__(self, store, session_factory=None) -> None:
        self.store = store
        self.session: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        # No cookies: once the hiscores set a cookie, every next lookup gets an HTML page instead of the JSON.
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "TradingCow Discord bot (github.com/LearnwithLucas/cafe-arcade-bot)"},
                cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=20))
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def setup(self) -> None:
        await self.store.conn.executescript(SCHEMA)
        await self.store.conn.commit()

    # ---- the competition and its players, remembered from the last time Wise Old Man answered

    async def remember(self, comp: dict) -> None:
        meta = {k: comp[k] for k in ("id", "title", "metric", "startsAt", "endsAt")}
        meta["players"] = [p["player"]["displayName"] for p in comp.get("participations", [])]
        await self.store.set(f"sotw:meta:{comp['id']}", json.dumps(meta))

    async def meta(self, cid: int) -> dict | None:
        raw = await self.store.get(f"sotw:meta:{cid}")
        if raw:
            return json.loads(raw)
        if SEED.exists():
            seed = json.loads(SEED.read_text())
            if seed.get("id") == cid:
                return seed
        return None

    # ---- snapshots

    async def player_xp(self, name: str, skill: str) -> int | None:
        s = await self._session()
        try:
            async with s.get(HISCORES + quote(name)) as r:
                if r.status == 404:
                    return None  # unknown name or not on the hiscores
                if r.status != 200:
                    raise RuntimeError(f"hiscores {r.status}")
                text = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RuntimeError(str(e)) from e
        try:
            data = json.loads(text)
        except ValueError as e:  # the hiscores sometimes answer with an empty or HTML page
            raise RuntimeError("hiscores sent no data") from e
        for sk in data.get("skills", []):
            if sk.get("name", "").lower() == skill.lower():
                xp = int(sk.get("xp", -1))
                return xp if xp >= 0 else None
        return None

    async def snapshot(self, cid: int) -> int:
        """Store the current XP of every player. Returns how many were read."""
        meta = await self.meta(cid)
        if not meta:
            return 0
        now = time.time()
        start = datetime.fromisoformat(meta["startsAt"].replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(meta["endsAt"].replace("Z", "+00:00")).timestamp()
        if now < start - 1800 or now > end + 3600:
            return 0  # only track around the competition
        done = 0
        for name in meta["players"]:
            xp = None
            for attempt in range(2):
                try:
                    xp = await self.player_xp(name, meta["metric"])
                    break
                except RuntimeError as e:  # usually throttling: wait and try once more
                    if attempt:
                        log.warning("Hiscores lookup failed for %s: %s", name, e)
                    await asyncio.sleep(15)
            if xp is not None:
                await self.store.conn.execute("INSERT INTO sotw_xp(competition, username, ts, xp) VALUES (?,?,?,?)",
                                              (cid, name, int(now), xp))
                done += 1
            await asyncio.sleep(PACE)  # the hiscores throttle fast lookups
        await self.store.conn.commit()
        log.info("SOTW backup tracker: %d of %d players read from the hiscores", done, len(meta["players"]))
        return done

    async def competition(self, cid: int) -> dict | None:
        """The competition rebuilt from our own snapshots, in the same shape Wise Old Man returns."""
        meta = await self.meta(cid)
        if not meta:
            return None
        start = int(datetime.fromisoformat(meta["startsAt"].replace("Z", "+00:00")).timestamp())
        parts = []
        for name in meta["players"]:
            async with self.store.conn.execute(
                    "SELECT xp, ts FROM sotw_xp WHERE competition=? AND username=? ORDER BY ts", (cid, name)) as cur:
                snaps = await cur.fetchall()
            if not snaps:
                parts.append({"player": {"username": name.lower(), "displayName": name, "updatedAt": None},
                              "progress": {"start": 0, "end": 0, "gained": 0}, "levels": {}})
                continue
            after = [s for s in snaps if s[1] >= start - 1800] or snaps
            base, last = after[0][0], snaps[-1]
            parts.append({"player": {"username": name.lower(), "displayName": name, "updatedAt": iso(last[1])},
                          "progress": {"start": base, "end": last[0], "gained": max(0, last[0] - base)}, "levels": {}})
        return {"id": cid, "title": meta["title"], "metric": meta["metric"], "startsAt": meta["startsAt"],
                "endsAt": meta["endsAt"], "participantCount": len(meta["players"]), "participations": parts,
                "backup": True}

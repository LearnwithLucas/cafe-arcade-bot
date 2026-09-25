"""Small async client for the OSRS Wiki real-time price API."""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("tradingcow.wiki")

BASE = "https://prices.runescape.wiki/api/v1/osrs/"
# The Wiki asks for a descriptive User-Agent with contact details.
USER_AGENT = os.getenv("TRADINGCOW_USER_AGENT", "TradingCow Discord bot - personal GE helper (github.com/LearnwithLucas)")


class Wiki:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get(self, endpoint: str, **params):
        await self.open()
        async with self.session.get(BASE + endpoint, params=params or None) as r:
            r.raise_for_status()
            return await r.json()

    async def mapping(self) -> dict[int, dict]:
        return {m["id"]: m for m in await self.get("mapping")}

    async def latest(self) -> dict:
        return (await self.get("latest"))["data"]

    async def hour(self, timestamp: int | None = None) -> tuple[int, dict]:
        j = await self.get("1h", **({"timestamp": timestamp} if timestamp else {}))
        return int(j.get("timestamp") or timestamp or 0), j["data"]

    async def day(self) -> dict:
        return (await self.get("24h"))["data"]

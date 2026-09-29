"""YouTube Data API v3 (public numbers) and YouTube Analytics API v2 (your own channels, optional)."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, timedelta

import aiohttp

log = logging.getLogger("tracker.youtube")

DATA = "https://www.googleapis.com/youtube/v3/"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def iso_duration_seconds(text: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


class YouTube:
    def __init__(self) -> None:
        self.key = os.getenv("YOUTUBE_API_KEY", "").strip()
        self.session: aiohttp.ClientSession | None = None
        self.client_id = os.getenv("TRACKER_YT_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("TRACKER_YT_CLIENT_SECRET", "").strip()
        self.refresh = {k: os.getenv(f"TRACKER_YT_REFRESH_{k.upper()}", "").strip() for k in ("en", "nl")}
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def analytics_enabled(self, key: str) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh.get(key))

    async def open(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def _get(self, endpoint: str, **params) -> dict:
        await self.open()
        params["key"] = self.key
        async with self.session.get(DATA + endpoint, params=params) as r:
            j = await r.json()
            if r.status != 200:
                raise RuntimeError(f"YouTube {endpoint} {r.status}: {j.get('error', {}).get('message', j)}")
            return j

    async def channel(self, handle_or_id: str) -> dict | None:
        """Snippet, statistics and uploads playlist for a handle (@name) or a channel id (UC...)."""
        parts = "snippet,statistics,contentDetails"
        if handle_or_id.startswith("UC"):
            j = await self._get("channels", part=parts, id=handle_or_id)
        else:
            j = await self._get("channels", part=parts, forHandle=handle_or_id if handle_or_id.startswith("@") else "@" + handle_or_id)
        items = j.get("items") or []
        return items[0] if items else None

    async def recent_uploads(self, uploads_playlist: str, limit: int = 50) -> list[str]:
        j = await self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist, maxResults=min(50, limit))
        return [it["contentDetails"]["videoId"] for it in j.get("items", [])]

    async def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            if chunk:
                j = await self._get("videos", part="snippet,statistics,contentDetails", id=",".join(chunk))
                out += j.get("items", [])
        return out

    # ---- Analytics API (needs an OAuth refresh token per channel)
    async def _access_token(self, key: str) -> str | None:
        if not self.analytics_enabled(key):
            return None
        tok = self._tokens.get(key)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        await self.open()
        async with self.session.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh[key], "grant_type": "refresh_token"}) as r:
            j = await r.json()
            if r.status != 200:
                log.warning("OAuth refresh failed for %s: %s", key, j)
                return None
        self._tokens[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return j["access_token"]

    async def report(self, key: str, metrics: str, start: date, end: date, dimensions: str = "", filters: str = "",
                     sort: str = "", max_results: int = 0) -> list[dict] | None:
        token = await self._access_token(key)
        if not token:
            return None
        params = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(), "metrics": metrics}
        if dimensions:
            params["dimensions"] = dimensions
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort
        if max_results:
            params["maxResults"] = max_results
        async with self.session.get(ANALYTICS, params=params, headers={"Authorization": f"Bearer {token}"}) as r:
            j = await r.json()
            if r.status != 200:
                log.warning("Analytics report failed (%s): %s", metrics, j.get("error", {}).get("message", j))
                return None
        cols = [c["name"] for c in j.get("columnHeaders", [])]
        return [dict(zip(cols, row)) for row in j.get("rows", []) or []]

    async def video_analytics(self, key: str, video_id: str, published: date) -> dict:
        """CTR, impressions, retention and traffic sources for one video, lifetime to date."""
        end = date.today()
        start = min(published, end)
        out: dict = {}
        rows = await self.report(key, "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                                 start, end, filters=f"video=={video_id}")
        if rows:
            out.update(rows[0])
        rows = await self.report(key, "videoThumbnailImpressions,videoThumbnailImpressionsClickRate", start, end,
                                 filters=f"video=={video_id}")
        if rows:
            out.update(rows[0])
        rows = await self.report(key, "views", start, end, dimensions="insightTrafficSourceType",
                                 filters=f"video=={video_id}", sort="-views", max_results=5)
        if rows:
            total = sum(r["views"] for r in rows) or 1
            out["traffic"] = [(r["insightTrafficSourceType"], r["views"] / total * 100) for r in rows]
        return out

    async def channel_day(self, key: str) -> dict | None:
        """Yesterday's channel totals (Analytics data lags about two days, so we take the latest full day)."""
        end = date.today() - timedelta(days=2)
        rows = await self.report(key, "views,estimatedMinutesWatched,subscribersGained,subscribersLost", end, end)
        return rows[0] if rows else None

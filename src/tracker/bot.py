"""Tracker: YouTube stats rated on subscribers, a Monday report, weekly competitor watch and a job queue for the Grabber agent on Lucas's PC.

Runs next to the arcade bot and TradingCow in the same process, with its own token and database.
The PC agent talks to Discord directly with the same bot token: it picks up JOB messages in
#bot-status, runs them locally (Trends via the extension, TikTok via the downloader) and posts results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config
from .config import CHANNELS, KIND_LABEL, kind_of, parse_code, rate
from .store import Store
from .youtube import YouTube, iso_duration_seconds

log = logging.getLogger("tracker")

COLOR = 0x3987E5
HOUR = 3600
DAY = 86400
MILESTONES = [("24h", 24), ("7d", 168), ("28d", 672)]
try:
    TZ = ZoneInfo(config.TIMEZONE)
except ZoneInfoNotFoundError:  # no tz database on the host
    TZ = timezone(timedelta(hours=2))
try:
    PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube Analytics counts days in Pacific time
except ZoneInfoNotFoundError:
    PACIFIC = timezone(timedelta(hours=-8))
WINDOWS = [("7d", 7), ("28d", 28)]   # rating windows: publish day plus 6 or 27 days
LAG_DAYS = 3                          # Studio numbers settle about 2 to 3 days late


def n(v) -> str:
    try:
        return f"{round(v):,}"
    except (TypeError, ValueError):
        return "-"


def pct(v) -> str:
    return "-" if v is None else f"{v:.1f}%"


def parse_ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def video_id_from(text: str) -> str:
    t = text.strip()
    for key in ("v=", "youtu.be/", "shorts/", "live/"):
        if key in t:
            t = t.split(key, 1)[1]
            break
    return t.split("&")[0].split("?")[0].split("/")[0]


def fmt_label(is_short: int) -> str:
    return "Short" if is_short else "Long-form"


def kind_label(v: dict) -> str:
    return KIND_LABEL.get(v.get("kind") or kind_of(v.get("is_short", 0), v.get("code")), "Video")


def code_text(code: str | None, src: str | None = None) -> str:
    """`D1`, or `D1*` when Tracker picked the code itself."""
    if not code:
        return "`no code`"
    return f"`{code}*`" if src == "auto" else f"`{code}`"


def post_ref(link: str) -> tuple[str, str] | None:
    """(platform, id) for a YouTube, TikTok or Instagram link."""
    t = link.strip()
    if "tiktok.com" in t:
        m = re.search(r"/video/(\d+)", t)
        return ("tt", m.group(1)) if m else None
    if "instagram.com" in t:
        m = re.search(r"/(?:reel|reels|p)/([^/?#]+)", t)
        return ("ig", m.group(1)) if m else None
    vid = video_id_from(t)
    return ("yt", vid) if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None


def ratings_text(names: list[str]) -> str:
    """"over, par, par" -> "par 2, over 1"."""
    counts: dict[str, int] = {}
    for x in names:
        counts[x] = counts.get(x, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def week_key(d: date) -> str:
    """The Monday that starts the week of d."""
    return (d - timedelta(days=d.weekday())).isoformat()


class Tracker(commands.Bot):
    def __init__(self, db_path: Path) -> None:
        super().__init__(command_prefix="!tracker-unused ", intents=discord.Intents.default(), help_command=None)
        self.store = Store(db_path)
        self.yt = YouTube()
        self.first_sync_done: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.store.connect()
        if not await self.store.get("watch_seeded_v2"):
            await self.store.set("watch_seeded_v2", 1)
            for lang, handles in config.DEFAULT_TIKTOK_COMPETITORS.items():
                for h in handles:
                    await self.store.run("INSERT OR IGNORE INTO watchlist VALUES (?,?,?)", ("tiktok", h, lang))
        await self.store.run("UPDATE videos SET kind = CASE WHEN is_short=1 THEN 'short' ELSE 'long' END WHERE kind IS NULL")
        for cmd in (stats_cmd, video_cmd, shorts_cmd, social_cmd, instagram_cmd, trends_cmd, ideas_cmd, tiktok_cmd, queue_cmd, clearqueue_cmd,
                    pc_cmd, report_cmd, tag_cmd, change_cmd, help_cmd):
            self.tree.add_command(cmd)
        self.tree.add_command(watch_group)
        self.tree.add_command(rule_group)
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.yt_loop.start()
        self.report_loop.start()
        if not self.yt.enabled:
            log.warning("YOUTUBE_API_KEY not set: Tracker runs without YouTube stats")

    async def close(self) -> None:
        self.yt_loop.cancel()
        self.report_loop.cancel()
        await self.yt.close()
        await self.store.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Tracker logged in as %s", self.user)

    async def ch(self, key: str):
        cid = CHANNELS.get(key)
        if not cid:
            return None
        c = self.get_channel(cid)
        if c is None:
            try:
                c = await self.fetch_channel(cid)
            except discord.HTTPException:
                log.warning("Tracker cannot see channel %s (%s)", key, cid)
                return None
        return c

    async def post(self, key: str, **kwargs):
        c = await self.ch(key)
        if c:
            try:
                return await c.send(**kwargs)
            except discord.HTTPException:
                log.exception("Tracker could not post in %s", key)
        return None

    # ------------------------------------------------------------ YouTube collection
    async def ensure_channels(self) -> None:
        for key, (handle, lang) in config.OWN_YOUTUBE.items():
            if not await self.store.one("SELECT 1 AS x FROM channels WHERE own_key=?", (key,)):
                c = await self.yt.channel(handle)
                if c:
                    await self.store.run("INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?)",
                                         (c["id"], handle, c["snippet"]["title"], lang, key,
                                          c["contentDetails"]["relatedPlaylists"]["uploads"], int(time.time())))
        for w in await self.store.rows("SELECT handle, lang FROM watchlist WHERE kind='youtube'"):
            if not await self.store.one("SELECT 1 AS x FROM channels WHERE lower(handle)=lower(?)", (w["handle"],)):
                c = await self.yt.channel(w["handle"])
                if c:
                    await self.store.run("INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?)",
                                         (c["id"], w["handle"], c["snippet"]["title"], w["lang"], None,
                                          c["contentDetails"]["relatedPlaylists"]["uploads"], int(time.time())))
                else:
                    log.warning("YouTube competitor %s not found", w["handle"])

    @tasks.loop(minutes=30)
    async def yt_loop(self) -> None:
        if not self.yt.enabled:
            return
        try:
            await self.auto_watch_youtube()
            await self.ensure_channels()
            for c in await self.store.rows("SELECT * FROM channels"):
                await self.sync_channel(c)
            await self.check_milestones()
            await self.check_ratings()
        except Exception:
            log.exception("Tracker YouTube sync failed")

    async def sync_channel(self, c: dict) -> None:
        own = c["own_key"] is not None
        info = await self.yt.channel(c["channel_id"])
        if info:
            st = info.get("statistics", {})
            last = await self.store.one("SELECT ts FROM channel_stats WHERE channel_id=? ORDER BY ts DESC LIMIT 1", (c["channel_id"],))
            if not last or time.time() - last["ts"] > 55 * 60:
                await self.store.run("INSERT OR REPLACE INTO channel_stats VALUES (?,?,?,?,?)",
                                     (c["channel_id"], int(time.time()), int(st.get("subscriberCount", 0) or 0),
                                      int(st.get("viewCount", 0) or 0), int(st.get("videoCount", 0) or 0)))
        known_before = bool(await self.store.one("SELECT 1 AS x FROM videos WHERE channel_id=? LIMIT 1", (c["channel_id"],)))
        ids = await self.yt.recent_uploads(c["uploads"], 50 if own else 20)
        have = {r["video_id"] for r in await self.store.rows(
            f"SELECT video_id FROM videos WHERE video_id IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else set()
        new_ids = [i for i in ids if i not in have]
        now = int(time.time())
        for v in await self.yt.videos(new_ids):
            dur = iso_duration_seconds(v["contentDetails"].get("duration", ""))
            desc_code = parse_code(v["snippet"].get("description")) if own else None
            await self.store.run("INSERT OR IGNORE INTO videos(video_id, channel_id, title, published, duration, is_short, announced, desc_code) "
                                 "VALUES (?,?,?,?,?,?,?,?)",
                                 (v["id"], c["channel_id"], v["snippet"]["title"], parse_ts(v["snippet"]["publishedAt"]),
                                  dur, int(dur <= 180), 0 if known_before else 1, desc_code))
        # Fresh numbers for everything published in the last 35 days.
        recent = await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND published>=?", (c["channel_id"], now - 35 * DAY))
        for v in await self.yt.videos([r["video_id"] for r in recent]):
            if own:  # pick up a code added to the description later
                await self.store.run("UPDATE videos SET title=?, desc_code=? WHERE video_id=?",
                                     (v["snippet"]["title"], parse_code(v["snippet"].get("description")), v["id"]))
            s = v.get("statistics", {})
            await self.store.snapshot(v["id"], int(s.get("viewCount", 0) or 0), int(s.get("likeCount", 0) or 0),
                                      int(s.get("commentCount", 0) or 0))
        if own:
            await self.refresh_playlists(c)
            await self.recode(c["channel_id"])
        for r in await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND announced=0", (c["channel_id"],)):
            await self.announce(c, r)
            await self.store.run("UPDATE videos SET announced=1 WHERE video_id=?", (r["video_id"],))

    # ------------------------------------------------------------ codes
    async def code_rules(self) -> list[tuple[str, str]]:
        if not await self.store.get("code_rules_seeded"):
            for pos, (code, pattern) in enumerate(config.DEFAULT_CODE_RULES):
                await self.store.run("INSERT OR IGNORE INTO code_rules VALUES (?,?,?)", (pos, code, pattern))
            await self.store.set("code_rules_seeded", 1)
        return [(r["code"], r["pattern"]) for r in await self.store.rows("SELECT * FROM code_rules ORDER BY pos")]

    async def refresh_playlists(self, c: dict) -> None:
        """Which playlists each of your videos is in (every 6 hours), so playlist names can pick the code."""
        key = f"playlists_at:{c['channel_id']}"
        if time.time() - float(await self.store.get(key, 0) or 0) < 6 * HOUR:
            return
        try:
            pairs = []
            for pid, title in await self.yt.channel_playlists(c["channel_id"]):
                pairs += [(vid, title) for vid in await self.yt.playlist_video_ids(pid)]
        except Exception:
            log.warning("Playlist refresh failed for %s", c["channel_id"], exc_info=True)
            return
        await self.store.run("DELETE FROM video_playlists WHERE video_id IN (SELECT video_id FROM videos WHERE channel_id=?)", (c["channel_id"],))
        for vid, title in pairs:
            await self.store.run("INSERT OR IGNORE INTO video_playlists VALUES (?,?)", (vid, title))
        await self.store.set(key, time.time())

    async def auto_code(self, v: dict, rules: list[tuple[str, str]]) -> str:
        lists = [r["playlist"] for r in await self.store.rows("SELECT playlist FROM video_playlists WHERE video_id=?", (v["video_id"],))]
        text = " | ".join([v["title"] or ""] + lists)
        for code, pattern in rules:
            try:
                if re.search(pattern, text, re.I):
                    return code
            except re.error:
                continue
        return config.FALLBACK_CODES[1 if v["is_short"] else 0]

    async def recode(self, channel_id: str | None = None) -> None:
        """Code for every own video: /tag first, then the "code:" line in the description, then the automatic rules."""
        rules = await self.code_rules()
        sql = "SELECT v.* FROM videos v JOIN channels c ON c.channel_id=v.channel_id WHERE c.own_key IS NOT NULL"
        args: tuple = ()
        if channel_id:
            sql += " AND v.channel_id=?"
            args = (channel_id,)
        for v in await self.store.rows(sql, args):
            t = await self.store.one("SELECT code FROM tags WHERE platform='yt' AND post_id=?", (v["video_id"],))
            if t:
                code, src = t["code"], "tag"
            elif v.get("desc_code"):
                code, src = v["desc_code"], "desc"
            else:
                code, src = await self.auto_code(v, rules), "auto"
            kind = kind_of(v["is_short"], code)
            if (code, src, kind) != (v.get("code"), v.get("code_src"), v.get("kind")):
                await self.store.run("UPDATE videos SET code=?, code_src=?, kind=? WHERE video_id=?", (code, src, kind, v["video_id"]))

    # ------------------------------------------------------------ competitors on YouTube
    async def auto_watch_youtube(self) -> None:
        """Once: find the YouTube channel of every TikTok competitor on the watchlist and follow it too."""
        if await self.store.get("auto_yt_watch_done"):
            return
        await self.store.set("auto_yt_watch_done", 1)
        added, missing = [], []
        have = {r["handle"].lower() for r in await self.store.rows("SELECT handle FROM watchlist WHERE kind='youtube'")}
        for w in await self.store.rows("SELECT handle, lang FROM watchlist WHERE kind='tiktok' ORDER BY lang, handle"):
            found = await self.find_youtube(w["handle"])
            if not found:
                missing.append(w["handle"])
                continue
            handle, title = found
            if handle.lower() not in have:
                await self.store.run("INSERT OR REPLACE INTO watchlist VALUES (?,?,?)", ("youtube", handle, w["lang"]))
                have.add(handle.lower())
                added.append(f"{title} ({handle})")
        text = "**Watchlist:** I looked up your TikTok competitors on YouTube.\n"
        text += ("Now following: " + ", ".join(added) + ".\n") if added else "No YouTube channels found.\n"
        if missing:
            text += "Not found on YouTube: " + ", ".join(missing) + ". Add them by hand with /watchlist add if they have one.\n"
        text += "Wrong match? Remove it with /watchlist remove."
        await self.post("competitors", content=text[:1900])

    async def find_youtube(self, tiktok_handle: str) -> tuple[str, str] | None:
        stem = re.sub(r"[^a-z0-9]", "", tiktok_handle.lower())
        for cand in dict.fromkeys([tiktok_handle, tiktok_handle.replace(".", ""), tiktok_handle.replace("_", ""), stem]):
            try:
                c = await self.yt.channel("@" + cand)
            except Exception:
                c = None
            if c:
                return c["snippet"].get("customUrl") or "@" + cand, c["snippet"]["title"]
        try:
            for it in await self.yt.search_channels(tiktok_handle):
                cid = it["snippet"]["channelId"]
                c = await self.yt.channel(cid)
                if not c:
                    continue
                names = re.sub(r"[^a-z0-9]", "", (c["snippet"].get("customUrl") or "").lower() + " " + c["snippet"]["title"].lower())
                if stem and (stem in names or names.startswith(stem[:8])):
                    return c["snippet"].get("customUrl") or cid, c["snippet"]["title"]
        except Exception:
            log.warning("YouTube search failed for %s", tiktok_handle, exc_info=True)
        return None

    async def announce(self, c: dict, v: dict) -> None:
        """Your own uploads only. Competitor uploads go into the Monday competitor summary."""
        if not c["own_key"]:
            return
        v = await self.store.one("SELECT * FROM videos WHERE video_id=?", (v["video_id"],)) or v
        note = "" if v.get("code") else " No code found: add a last line like `code: D1` to the description, or use /tag."
        e = discord.Embed(title=f"New upload: {v['title']}", url=f"https://youtu.be/{v['video_id']}", color=COLOR,
                          description=f"{code_text(v.get('code'), v.get('code_src'))} {kind_label(v)}, published <t:{v['published']}:R>. "
                                      f"First look after 24 hours, first rating at 7 days.{note}")
        await self.post("yt_en" if c["own_key"] == "en" else "yt_nl", embed=e)

    async def baseline(self, channel_id: str, is_short: int, milestone: str, exclude: str) -> tuple[float | None, int, bool]:
        """Median views at this milestone for the channel's previous videos of the same format.

        Returns (median, sample size, rough). Rough means there is no milestone history yet and the
        median of current views of 14 to 120-day-old videos stands in.
        """
        rows = await self.store.rows(
            "SELECT m.views FROM milestones m JOIN videos v ON v.video_id=m.video_id "
            "WHERE v.channel_id=? AND v.is_short=? AND m.milestone=? AND m.views IS NOT NULL AND m.video_id<>? "
            "ORDER BY v.published DESC LIMIT 20", (channel_id, is_short, milestone, exclude))
        vals = [r["views"] for r in rows]
        if len(vals) >= 5:
            return statistics.median(vals), len(vals), False
        now = int(time.time())
        rough = await self.store.rows(
            "SELECT v.video_id FROM videos v WHERE v.channel_id=? AND v.is_short=? AND v.published BETWEEN ? AND ? AND v.video_id<>?",
            (channel_id, is_short, now - 120 * DAY, now - 14 * DAY, exclude))
        cur = []
        for r in rough:
            x = await self.store.one("SELECT views FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (r["video_id"],))
            if x:
                cur.append(x["views"])
        if len(cur) >= 3:
            return statistics.median(cur), len(cur), True
        return (statistics.median(vals) if vals else None), len(vals), False

    async def check_milestones(self) -> None:
        now = int(time.time())
        vids = await self.store.rows(
            "SELECT v.*, c.own_key, c.title AS channel_title FROM videos v JOIN channels c ON c.channel_id=v.channel_id "
            "WHERE v.published>=?", (now - 30 * DAY,))
        for v in vids:
            own = v["own_key"] is not None
            for name, hours in MILESTONES if own else [("7d", 168)]:
                target = v["published"] + hours * HOUR
                if now < target:
                    continue
                if await self.store.one("SELECT 1 AS x FROM milestones WHERE video_id=? AND milestone=?", (v["video_id"], name)):
                    continue
                views = await self.store.views_at(v["video_id"], target + 2 * HOUR)
                first = await self.store.one("SELECT ts FROM snapshots WHERE video_id=? ORDER BY ts LIMIT 1", (v["video_id"],))
                # Only trust the number when we were already watching before the milestone passed.
                if views is None or not first or first["ts"] > target + 6 * HOUR:
                    await self.store.run("INSERT OR REPLACE INTO milestones VALUES (?,?,?,?)", (v["video_id"], name, None, now))
                    continue
                await self.store.run("INSERT OR REPLACE INTO milestones VALUES (?,?,?,?)", (v["video_id"], name, views, now))
                if own and name == "24h":
                    await self.post_first_look(v, views)
                elif own and name == "7d" and not self.yt.analytics_enabled(v["own_key"]):
                    await self.post("yt_en" if v["own_key"] == "en" else "yt_nl", content=(
                        f"7 days: **{v['title']}** has {n(views)} views. Not rated: ratings use subscribers, which need YouTube Studio "
                        f"access for this channel (TRACKER_YT_REFRESH_{v['own_key'].upper()} on Render)."))

    async def post_first_look(self, v: dict, views: int) -> None:
        """24 hours: views only, no rating. Subscriber numbers are not in Studio yet."""
        snap = await self.store.one("SELECT likes, comments FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
        e = discord.Embed(title=f"24h first look: {v['title']}", url=f"https://youtu.be/{v['video_id']}", color=COLOR,
                          description=f"{code_text(v.get('code'), v.get('code_src'))} {kind_label(v)}. No rating yet: it is rated on subscribers at 7 days, "
                                      f"once YouTube Studio has the numbers (about 10 days after upload).")
        e.add_field(name="Views", value=n(views))
        if snap:
            e.add_field(name="Likes / comments", value=f"{n(snap['likes'])} / {n(snap['comments'])}")
        await self.post("yt_en" if v["own_key"] == "en" else "yt_nl", embed=e)

    # ------------------------------------------------------------ ratings on subscribers (YouTube Studio)
    async def check_ratings(self) -> None:
        """Fetch subscribers, views and outside-YouTube share for the 7-day and 28-day windows, then rate.

        Videos that were already old when Studio was connected are filled in quietly, as history to compare with."""
        today = datetime.now(PACIFIC).date()
        budget = 30
        for c in await self.store.rows("SELECT * FROM channels WHERE own_key IS NOT NULL"):
            key = c["own_key"]
            if not self.yt.analytics_enabled(key):
                continue
            since = await self.store.get(f"studio_since:{key}")
            if since is None:
                since = int(time.time())
                await self.store.set(f"studio_since:{key}", since)
                log.info("YouTube Studio connected for %s; filling in history quietly", key)
            vids = await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND published>=? ORDER BY published",
                                         (c["channel_id"], int(time.time()) - 200 * DAY))
            for v in vids:
                pub = datetime.fromtimestamp(v["published"], PACIFIC).date()
                for win, days in WINDOWS:
                    ready = pub + timedelta(days=days - 1 + LAG_DAYS)
                    if today < ready or await self.store.one("SELECT 1 AS x FROM yt_stats WHERE video_id=? AND win=?", (v["video_id"], win)):
                        continue
                    if budget <= 0:
                        return
                    budget -= 1
                    r = await self.yt.video_window(key, v["video_id"], pub, pub + timedelta(days=days - 1))
                    if r is None:
                        log.warning("Studio numbers unavailable for %s; check the refresh token", key)
                        break
                    await self.store.run("INSERT OR REPLACE INTO yt_stats VALUES (?,?,?,?,?,?)",
                                         (v["video_id"], win, r["views"], r["subs"], r["ext_pct"], int(time.time())))
                    ready_ts = datetime.combine(ready, datetime.min.time(), PACIFIC).timestamp()
                    if ready_ts >= since - DAY:
                        await self.post_rating({**v, "own_key": key}, win, r)

    async def earlier_subs(self, v: dict, win: str) -> list[float]:
        """Subscribers in the same window for the 20 most recent earlier videos of the same kind on the same channel."""
        rows = await self.store.rows(
            "SELECT s.subs FROM yt_stats s JOIN videos v ON v.video_id=s.video_id "
            "WHERE v.channel_id=? AND v.kind=? AND s.win=? AND v.published<? AND v.video_id<>? "
            "ORDER BY v.published DESC LIMIT 20",
            (v["channel_id"], v.get("kind") or kind_of(v["is_short"], v.get("code")), win, v["published"], v["video_id"]))
        return [r["subs"] for r in rows if r["subs"] is not None]

    async def rating_of(self, v: dict, win: str) -> tuple[dict | None, str, str]:
        s = await self.store.one("SELECT * FROM yt_stats WHERE video_id=? AND win=?", (v["video_id"], win))
        if not s:
            return None, "not rated yet", ""
        name, why = rate(s["subs"], await self.earlier_subs(v, win))
        return s, name, why

    async def post_rating(self, v: dict, win: str, r: dict) -> None:
        name, why = rate(r["subs"], await self.earlier_subs(v, win))
        days = dict(WINDOWS)[win]
        e = discord.Embed(title=f"{win} rating: {v['title']}", url=f"https://youtu.be/{v['video_id']}", color=COLOR,
                          description=f"{code_text(v.get('code'), v.get('code_src'))} {kind_label(v)}. **{name}**: {why}.")
        e.add_field(name="Subscribers", value=n(r["subs"]))
        e.add_field(name="Views", value=n(r["views"]))
        e.add_field(name="Subs per 1,000 views", value=f"{r['subs'] / r['views'] * 1000:.1f}" if r["views"] else "-")
        e.add_field(name="Views from outside YouTube", value=pct(r["ext_pct"]))
        e.set_footer(text=f"Rated on subscribers against earlier {kind_label(v).lower()}s on this channel, over the same "
                          f"{days} days after publishing. Outlier 3.6x+, over 1.6x+, par 0.72x+, under 0.5x+, flop below.")
        await self.post("yt_en" if v["own_key"] == "en" else "yt_nl", embed=e)
        if name in ("outlier", "flop"):
            await self.post("alerts", content=f"{name.capitalize()} at {win}: {code_text(v.get('code'), v.get('code_src'))} **{v['title']}**, "
                                              f"{why}. https://youtu.be/{v['video_id']}")

    async def top_shorts(self, own_key: str, days: int = 7) -> list[dict]:
        """Your Shorts published in the last N days, most viewed first, with their latest numbers."""
        now = int(time.time())
        rows = await self.store.rows(
            "SELECT v.* FROM videos v JOIN channels c ON c.channel_id=v.channel_id "
            "WHERE c.own_key=? AND v.is_short=1 AND v.published>=?", (own_key, now - days * DAY))
        out = []
        for v in rows:
            s = await self.store.one("SELECT views, likes, comments FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
            if s:
                out.append({**v, **s, "per_hour": s["views"] / max(1, (now - v["published"]) / HOUR)})
        return sorted(out, key=lambda r: r["views"], reverse=True)

    # ------------------------------------------------------------ Monday report
    @tasks.loop(minutes=5)
    async def report_loop(self) -> None:
        now = datetime.now(TZ)
        if now.weekday() != 0:
            return
        today = now.date().isoformat()
        if await self.store.get("report_sent") == today:
            return
        if now.hour >= max(0, config.REPORT_HOUR - 2) and await self.store.get("week_job") != today:
            # Ask the PC for TikTok and Instagram first, so its numbers are in by report time.
            await self.store.set("week_job", today)
            await self.store.set("week_job_ts", int(time.time()))
            await self.queue_job("week", await self.tag_arg())
        if now.hour < config.REPORT_HOUR:
            return
        pc = await self.latest_weekdata(int(await self.store.get("week_job_ts", 0) or 0))
        if pc is None and now.hour < config.REPORT_DEADLINE_HOUR:
            return  # give the PC until the deadline
        await self.store.set("report_sent", today)
        try:
            await self.post("weekly_report", embeds=await self.build_report(pc))
            await self.post("competitors", embed=await self.build_competitors())
        except Exception:
            log.exception("Monday report failed")

    @yt_loop.before_loop
    @report_loop.before_loop
    async def _wait(self) -> None:
        await self.wait_until_ready()

    async def channel_delta(self, channel_id: str, seconds: int) -> tuple[dict | None, dict | None]:
        now = int(time.time())
        cur = await self.store.one("SELECT * FROM channel_stats WHERE channel_id=? ORDER BY ts DESC LIMIT 1", (channel_id,))
        old = await self.store.one("SELECT * FROM channel_stats WHERE channel_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (channel_id, now - seconds))
        return cur, old

    async def tag_arg(self) -> str:
        """Codes set with /tag for TikTok and Instagram posts, handed to the PC with the week job."""
        rows = await self.store.rows("SELECT platform, post_id, code FROM tags WHERE platform IN ('tt','ig') AND ts>=? ORDER BY ts DESC",
                                     (int(time.time()) - 120 * DAY,))
        out = ""
        for r in rows:
            item = f"{r['platform']}:{r['post_id']}={r['code']}"
            if len(out) + len(item) + 1 > 1500:
                break
            out = f"{out},{item}" if out else item
        return out

    async def latest_weekdata(self, since_ts: int) -> dict | None:
        """The TikTok and Instagram numbers the PC posted for the week job (a WEEKDATA message with a JSON file)."""
        for m in await self.status_messages(100):
            if m.content.startswith("WEEKDATA|v1") and m.created_at.timestamp() >= since_ts - 60 and m.attachments:
                try:
                    return json.loads((await m.attachments[0].read()).decode("utf-8"))
                except Exception:
                    log.exception("Could not read WEEKDATA")
        return None

    async def youtube_formats(self, c: dict, start_ts: int, end_ts: int) -> list[str]:
        """Per code, the videos whose 7-day rating landed this week: subscribers and ratings."""
        vids = await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND published>=? AND published<? ORDER BY published",
                                     (c["channel_id"], start_ts, end_ts))
        groups: dict[str, list[tuple]] = {}
        for v in vids:
            s, name, _ = await self.rating_of(v, "7d")
            if s:
                groups.setdefault(v.get("code") or f"no code ({kind_label(v).lower()})", []).append((s["subs"], name, s["ext_pct"]))
        lines = []
        for code, items in sorted(groups.items()):
            subs = [x[0] for x in items]
            ext = [x[2] for x in items if x[2] is not None]
            lines.append(f"`{code}`: {len(items)} video{'s' if len(items) > 1 else ''}, {n(sum(subs))} subs "
                         f"({', '.join(n(x) for x in subs)}), rated: {ratings_text([x[1] for x in items])}"
                         + (f", {statistics.median(ext):.0f}% from outside YouTube" if ext else ""))
        return lines

    async def build_report(self, pc: dict | None) -> list[discord.Embed]:
        today = datetime.now(TZ).date()
        last_mon = today - timedelta(days=today.weekday() + 7)
        prev = await self.store.one("SELECT text FROM changes WHERE week=?", (last_mon.isoformat(),))
        cur = await self.store.one("SELECT text FROM changes WHERE week=?", (week_key(today),))
        head = discord.Embed(title=f"Monday report, {today.strftime('%d %B %Y')}", color=COLOR, description=(
            f"**Last week's one change:** {prev['text'] if prev else 'none logged'}\n"
            f"**This week's one change:** {cur['text'] if cur else 'not set yet. Use /change to set it.'}"))
        # Follows per platform
        lines = []
        pac_today = datetime.now(PACIFIC).date()
        end = pac_today - timedelta(days=2)
        start = end - timedelta(days=6)
        for c in await self.store.rows("SELECT * FROM channels WHERE own_key IS NOT NULL ORDER BY own_key"):
            r = await self.yt.channel_range(c["own_key"], start, end) if self.yt.analytics_enabled(c["own_key"]) else None
            if r:
                lines.append(f"YouTube {c['own_key'].upper()}: **{int(r.get('subscribersGained') or 0) - int(r.get('subscribersLost') or 0):+,}** "
                             f"subscribers (+{n(r.get('subscribersGained'))} / -{n(r.get('subscribersLost'))}), "
                             f"{start.strftime('%d %b')} to {end.strftime('%d %b')}")
            else:
                now_s, old_s = await self.channel_delta(c["channel_id"], 7 * DAY)
                delta = f"{now_s['subs'] - old_s['subs']:+,}" if now_s and old_s else "collecting"
                lines.append(f"YouTube {c['own_key'].upper()}: {delta} subscribers, last 7 days (public count, rounded; Studio not connected)")
        for p in (pc or {}).get("platforms", []):
            f = p.get("follows_week")
            lines.append(f"{p['name']}: " + (f"**{f:+,}** followers, last 7 days" if f is not None else f"not enough data ({p.get('note') or 'no follower history yet'})"))
        if not pc:
            lines.append("TikTok and Instagram: no numbers from your PC this week. Start Grabber, then use /report.")
        head.add_field(name="Follows per platform", value="\n".join(lines)[:1024] or "-", inline=False)
        # Follows per format, at equal age
        fmt = discord.Embed(title="Follows per format", color=COLOR, description=(
            "Posts that turned 7 days old this week, grouped by code. Each post is rated against earlier posts of the same kind "
            "on the same account, at the same age. Too little history means **not enough data**, not a verdict."))
        start_ts = int((datetime.now() - timedelta(days=16)).timestamp())
        end_ts = int((datetime.now() - timedelta(days=9)).timestamp())
        for c in await self.store.rows("SELECT * FROM channels WHERE own_key IS NOT NULL ORDER BY own_key"):
            if not self.yt.analytics_enabled(c["own_key"]):
                fmt.add_field(name=f"YouTube {c['own_key'].upper()}", value="Not rated: YouTube Studio is not connected.", inline=False)
                continue
            yl = await self.youtube_formats(c, start_ts, end_ts)
            fmt.add_field(name=f"YouTube {c['own_key'].upper()}", value="\n".join(yl)[:1024] or "No videos turned 7 days old this week.", inline=False)
        for p in (pc or {}).get("platforms", []):
            pl = []
            for f in p.get("formats", []):
                follows = f.get("follows") or []
                pl.append(f"`{f['code']}`: {f['n']} post{'s' if f['n'] > 1 else ''}, "
                          + (f"{n(sum(follows))} follows ({', '.join(n(x) for x in follows)}), rated: {ratings_text(f.get('ratings') or [])}"
                             if follows else "no follow numbers"))
            note = f"\n{p['format_note']}" if p.get("format_note") else ""
            body = "\n".join(pl) or "No posts turned 7 days old this week."
            fmt.add_field(name=p["name"], value=(body[:1020 - len(note[:300])] + note[:300]), inline=False)
        fmt.set_footer(text="Codes come from a last line like \"code: D1\" in the description or caption, or from /tag.")
        return [head, fmt]

    async def build_competitors(self) -> discord.Embed:
        now = int(time.time())
        e = discord.Embed(title="Competitors, last 7 days", color=0x8D8C84)
        new = await self.store.rows("SELECT v.*, c.title AS ct FROM videos v JOIN channels c ON c.channel_id=v.channel_id "
                                    "WHERE c.own_key IS NULL AND v.published>=?", (now - 7 * DAY,))
        if not new:
            e.description = "No new competitor uploads on YouTube. Add channels with /watchlist add."
            return e
        per_channel: dict[str, int] = {}
        scored = []
        for v in new:
            per_channel[v["ct"]] = per_channel.get(v["ct"], 0) + 1
            s = await self.store.one("SELECT views FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
            scored.append(((s["views"] if s else 0) / max(1, (now - v["published"]) / DAY), v))
        scored.sort(key=lambda x: x[0], reverse=True)
        e.description = "Uploads: " + ", ".join(f"{k} {c}" for k, c in sorted(per_channel.items(), key=lambda x: -x[1]))
        e.add_field(name="Fastest new uploads (views per day)", inline=False, value="\n".join(
            f"{v['ct']}: [{v['title'][:60]}](https://youtu.be/{v['video_id']}), {n(r)}/day, {fmt_label(v['is_short']).lower()}"
            for r, v in scored[:6])[:1024])
        good = []
        for m in await self.store.rows("SELECT m.*, v.title, v.channel_id, v.is_short, c.title AS ct FROM milestones m "
                                       "JOIN videos v ON v.video_id=m.video_id JOIN channels c ON c.channel_id=v.channel_id "
                                       "WHERE c.own_key IS NULL AND m.milestone='7d' AND m.views IS NOT NULL AND m.ts>=?", (now - 7 * DAY,)):
            base, size, _ = await self.baseline(m["channel_id"], m["is_short"], "7d", m["video_id"])
            if base and size >= 5 and m["views"] / base >= 1.6:
                good.append((m["views"] / base, m))
        if good:
            good.sort(key=lambda x: x[0], reverse=True)
            e.add_field(name="Did well at 7 days (views against their own usual)", inline=False, value="\n".join(
                f"{m['ct']}: [{m['title'][:60]}](https://youtu.be/{m['video_id']}), {r:.1f}x" for r, m in good[:6])[:1024])
        e.set_footer(text="Competitor subscribers per video are private, so competitors are compared on views only.")
        return e

    # ------------------------------------------------------------ PC agent link
    async def status_messages(self, limit: int = 100) -> list[discord.Message]:
        c = await self.ch("bot_status")
        if not c:
            return []
        return [m async for m in c.history(limit=limit) if m.author.id == self.user.id]

    async def pc_status(self) -> dict:
        for m in await self.status_messages(50):
            first = m.content.split("\n", 1)[0]
            if first.startswith("PC_STATUS|"):
                try:
                    info = json.loads(first.split("|", 2)[2])
                except (IndexError, ValueError):
                    info = {}
                seen = int(info.get("seen", 0))
                online = time.time() - seen < 10 * 60
                text = (f"{'Online' if online else 'Offline'}, last seen <t:{seen}:R>." if seen else "Never connected.")
                if info.get("trends_posted"):
                    text += f" Trends posted <t:{int(info['trends_posted'])}:R>."
                if info.get("tiktok_posted"):
                    text += f" TikTok report <t:{int(info['tiktok_posted'])}:R>."
                return {"online": online, "text": text}
        return {"online": False, "text": "Never connected. Start Grabber's downloader with tracker_config.json filled in."}

    async def recent_jobs(self) -> list[dict]:
        out = []
        for m in await self.status_messages(100):
            first = m.content.split("\n", 1)[0]
            if first.startswith("JOB|"):
                p = first.split("|")
                if len(p) >= 5:
                    out.append({"id": p[1], "kind": p[2], "arg": p[3], "status": "|".join(p[4:]), "msg": m})
        return out

    async def queue_job(self, kind: str, arg: str = "") -> int | None:
        c = await self.ch("bot_status")
        if not c:
            return None
        for j in await self.recent_jobs():
            if j["kind"] == kind and j["arg"] == arg and j["status"] in ("queued", "running"):
                return int(j["id"])
        jid = await self.store.run("INSERT INTO jobs(kind, arg, created) VALUES (?,?,?)", (kind, arg, int(time.time())))
        msg = await c.send(f"JOB|{jid}|{kind}|{arg}|queued\nWaiting for the PC agent to pick this up.")
        await self.store.run("UPDATE jobs SET message_id=? WHERE id=?", (msg.id, jid))
        return jid


def bot_of(i: discord.Interaction) -> Tracker:
    return i.client  # type: ignore[return-value]


# ------------------------------------------------------------ commands

@app_commands.command(name="stats", description="Channel overview: subscribers, views and recent videos")
@app_commands.choices(channel=[app_commands.Choice(name="English", value="en"), app_commands.Choice(name="Dutch", value="nl")])
async def stats_cmd(i: discord.Interaction, channel: str) -> None:
    b = bot_of(i)
    await i.response.defer()
    c = await b.store.one("SELECT * FROM channels WHERE own_key=?", (channel,))
    if not c:
        await i.followup.send("No data yet. Check that YOUTUBE_API_KEY is set, then wait up to 30 minutes.")
        return
    cur, day = await b.channel_delta(c["channel_id"], DAY)
    _, week = await b.channel_delta(c["channel_id"], 7 * DAY)
    e = discord.Embed(title=c["title"], url=f"https://www.youtube.com/{c['handle']}", color=COLOR)
    if cur:
        e.add_field(name="Subscribers", value=n(cur["subs"]) + (f" ({cur['subs'] - week['subs']:+,} in 7d)" if week else ""))
        e.add_field(name="Views, 24h", value=f"{cur['views'] - day['views']:+,}" if day else "collecting")
        e.add_field(name="Views, 7d", value=f"{cur['views'] - week['views']:+,}" if week else "collecting")
    vids = await b.store.rows("SELECT * FROM videos WHERE channel_id=? ORDER BY published DESC LIMIT 8", (c["channel_id"],))
    lines = []
    for v in vids:
        s = await b.store.one("SELECT views FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
        tag = ""
        for win in ("28d", "7d"):
            st, name, _ = await b.rating_of(v, win)
            if st:
                tag = f", {n(st['subs'])} subs at {win} ({name})"
                break
        letter = {"short": "S", "topic": "T", "long": "L"}.get(v.get("kind") or "", "L")
        lines.append(f"{letter} {code_text(v.get('code'), v.get('code_src'))} [{v['title'][:45]}](https://youtu.be/{v['video_id']}): {n(s['views'] if s else None)} views{tag}")
    e.add_field(name="Latest videos (S = Short, T = weekly topic, L = other long)", value="\n".join(lines)[:1024] or "-", inline=False)
    if not b.yt.analytics_enabled(channel):
        e.set_footer(text="YouTube Studio is not connected for this channel, so there are no subscriber ratings.")
    await i.followup.send(embed=e)


@app_commands.command(name="video", description="Numbers for one YouTube video, compared with the channel's usual")
@app_commands.describe(link="Video link or id")
async def video_cmd(i: discord.Interaction, link: str) -> None:
    b = bot_of(i)
    await i.response.defer()
    vid = video_id_from(link)
    items = await b.yt.videos([vid]) if b.yt.enabled else []
    if not items:
        await i.followup.send("I can't find that video.")
        return
    v = items[0]
    s = v.get("statistics", {})
    pub = parse_ts(v["snippet"]["publishedAt"])
    hours = max(1, (time.time() - pub) / HOUR)
    views = int(s.get("viewCount", 0) or 0)
    e = discord.Embed(title=v["snippet"]["title"], url=f"https://youtu.be/{vid}", color=COLOR,
                      description=f"{v['snippet']['channelTitle']}, published <t:{pub}:R>")
    e.add_field(name="Views", value=f"{n(views)} ({n(views / hours)}/hour)")
    e.add_field(name="Likes", value=n(s.get("likeCount")))
    e.add_field(name="Comments", value=n(s.get("commentCount")))
    row = await b.store.one("SELECT v.*, c.own_key FROM videos v JOIN channels c ON c.channel_id=v.channel_id WHERE video_id=?", (vid,))
    if row and row["own_key"]:
        e.description += f"\n{code_text(row.get('code'), row.get('code_src'))} {kind_label(row)}"
        for win, _ in WINDOWS:
            st, name, why = await b.rating_of(row, win)
            if st:
                e.add_field(name=f"At {win}", inline=False,
                            value=f"**{name}**: {n(st['subs'])} subs, {n(st['views'])} views, {pct(st['ext_pct'])} from outside YouTube. {why}")
        if b.yt.analytics_enabled(row["own_key"]):
            a = await b.yt.video_analytics(row["own_key"], vid, datetime.fromtimestamp(pub, PACIFIC).date())
            if a.get("subscribersGained") is not None:
                e.add_field(name="Subs so far", value=n(a["subscribersGained"]))
            ext = dict(a.get("traffic") or []).get("EXT_URL")
            if a.get("traffic"):
                e.add_field(name="Outside YouTube so far", value=pct(ext or 0.0))
            if a.get("videoThumbnailImpressionsClickRate") is not None:
                e.add_field(name="CTR", value=pct(a["videoThumbnailImpressionsClickRate"]))
            if a.get("averageViewPercentage") is not None:
                e.add_field(name="Avg viewed", value=pct(a["averageViewPercentage"]))
        else:
            e.set_footer(text="YouTube Studio is not connected for this channel, so there are no subscriber numbers.")
    await i.followup.send(embed=e)


watch_group = app_commands.Group(name="watchlist", description="Competitors and accounts to follow")


@watch_group.command(name="add", description="Follow a channel or account")
@app_commands.choices(kind=[app_commands.Choice(name="YouTube competitor", value="youtube"),
                            app_commands.Choice(name="TikTok competitor", value="tiktok"),
                            app_commands.Choice(name="My TikTok account", value="tiktok_mine")],
                      lang=[app_commands.Choice(name="English", value="en"), app_commands.Choice(name="Dutch", value="nl")])
async def watch_add(i: discord.Interaction, kind: str, handle: str, lang: str) -> None:
    h = handle.strip().split("/")[-1]
    if kind != "youtube":
        h = h.lstrip("@")
    elif not h.startswith("@") and not h.startswith("UC"):
        h = "@" + h
    await bot_of(i).store.run("INSERT OR REPLACE INTO watchlist VALUES (?,?,?)", (kind, h, lang))
    await i.response.send_message(f"Following {h} ({kind.replace('_', ' ')}, {lang}).", ephemeral=True)


@watch_group.command(name="remove", description="Stop following a channel or account")
async def watch_remove(i: discord.Interaction, handle: str) -> None:
    b = bot_of(i)
    h = handle.strip().split("/")[-1]
    await b.store.run("DELETE FROM watchlist WHERE lower(handle) IN (lower(?), lower(?), lower(?))", (h, h.lstrip("@"), "@" + h.lstrip("@")))
    await b.store.run("DELETE FROM channels WHERE own_key IS NULL AND lower(handle) IN (lower(?), lower(?))", (h, "@" + h.lstrip("@")))
    await i.response.send_message(f"Stopped following {h}.", ephemeral=True)


@watch_group.command(name="list", description="Everything Tracker follows")
async def watch_list(i: discord.Interaction) -> None:
    rows = await bot_of(i).store.rows("SELECT * FROM watchlist ORDER BY kind, lang, handle")
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(f"{r['kind'].replace('_', ' ')} ({r['lang']})", []).append(r["handle"])
    text = "\n".join(f"**{k}**: {', '.join(v)}" for k, v in groups.items()) or "Nothing yet."
    await i.response.send_message(text[:1900], ephemeral=True)


@app_commands.command(name="trends", description="Ask your PC to collect Google Trends now and post the brief")
async def trends_cmd(i: discord.Interaction) -> None:
    jid = await bot_of(i).queue_job("trends")
    await i.response.send_message(f"Queued as job #{jid}. Your PC picks it up when Grabber is running; results land in the trends and video-ideas channels.", ephemeral=True)


@app_commands.command(name="ideas", description="Repost the latest trends brief and video ideas from your PC")
async def ideas_cmd(i: discord.Interaction) -> None:
    jid = await bot_of(i).queue_job("ideas")
    await i.response.send_message(f"Queued as job #{jid}.", ephemeral=True)


@app_commands.command(name="tiktok", description="Ask your PC to scan TikTok accounts (one handle, or leave empty for the whole watchlist)")
async def tiktok_cmd(i: discord.Interaction, handle: str | None = None) -> None:
    b = bot_of(i)
    if handle:
        jid = await b.queue_job("tiktok", handle.strip().lstrip("@").split("/")[-1].lstrip("@"))
    else:
        rows = await b.store.rows("SELECT handle, kind FROM watchlist WHERE kind IN ('tiktok', 'tiktok_mine') ORDER BY kind DESC, handle")
        jid = await b.queue_job("tiktok", ",".join(r["handle"] for r in rows))
    await i.response.send_message(f"Queued as job #{jid}. Scans take a few minutes per account; reports land in the tiktok channel.", ephemeral=True)


@app_commands.command(name="shorts", description="Your YouTube Shorts from the last 7 days, most viewed first")
@app_commands.choices(channel=[app_commands.Choice(name="English", value="en"), app_commands.Choice(name="Dutch", value="nl"),
                               app_commands.Choice(name="Both", value="both")])
async def shorts_cmd(i: discord.Interaction, channel: str = "both", days: app_commands.Range[int, 1, 30] = 7) -> None:
    b = bot_of(i)
    await i.response.defer()
    embeds = []
    for key in (["en", "nl"] if channel == "both" else [channel]):
        c = await b.store.one("SELECT * FROM channels WHERE own_key=?", (key,))
        if not c:
            continue
        rows = await b.top_shorts(key, days)
        e = discord.Embed(title=f"{c['title']}: Shorts, last {days} days", color=COLOR)
        if not rows:
            e.description = "No Shorts in this period."
        else:
            total = sum(r["views"] for r in rows)
            e.description = f"{len(rows)} Shorts, {n(total)} views in total, median {n(statistics.median(r['views'] for r in rows))}."
            e.add_field(name="Most viewed", inline=False, value="\n".join(
                f"{k}. {code_text(r.get('code'), r.get('code_src'))} [{r['title'][:50]}](https://youtu.be/{r['video_id']}): {n(r['views'])} views "
                f"({n(r['per_hour'])}/h), {n(r['likes'])} likes, {n(r['comments'])} comments, <t:{r['published']}:R>"
                for k, r in enumerate(rows[:10], 1))[:1024])
        embeds.append(e)
    await i.followup.send(embeds=embeds or [discord.Embed(description="No channel data yet.")])


@app_commands.command(name="social", description="Last 7 days on your TikTok accounts (runs on your PC)")
async def social_cmd(i: discord.Interaction) -> None:
    jid = await bot_of(i).queue_job("tiktok7d")
    await i.response.send_message(f"Queued as job #{jid}. Your PC refreshes your TikTok numbers first, so give it a few minutes. "
                                  "Results land in the tiktok channel.", ephemeral=True)


@app_commands.command(name="instagram", description="Last 7 days on your Instagram accounts (runs on your PC, Chrome must be open)")
async def instagram_cmd(i: discord.Interaction) -> None:
    jid = await bot_of(i).queue_job("instagram7d")
    await i.response.send_message(f"Queued as job #{jid}. Chrome reads your Instagram Reels tabs first (1 to 3 minutes per account). "
                                  "Results land in the instagram channel.", ephemeral=True)


@app_commands.command(name="queue", description="Jobs waiting for or running on your PC")
async def queue_cmd(i: discord.Interaction) -> None:
    jobs = await bot_of(i).recent_jobs()
    text = "\n".join(f"#{j['id']} {j['kind']} {j['arg'][:40]}: {j['status']}" for j in jobs[:15]) or "No jobs yet."
    await i.response.send_message(text, ephemeral=True)


@app_commands.command(name="clearqueue", description="Cancel waiting jobs and tidy up finished job messages in bot-status")
async def clearqueue_cmd(i: discord.Interaction) -> None:
    b = bot_of(i)
    await i.response.defer(ephemeral=True)
    cancelled = removed = 0
    for j in await b.recent_jobs():
        try:
            if j["status"] in ("queued", "running"):
                # The PC agent only starts jobs marked queued, so this stops anything still waiting.
                await j["msg"].edit(content=f"JOB|{j['id']}|{j['kind']}|{j['arg']}|cancelled\nCancelled with /clearqueue.")
                cancelled += 1
            else:
                await j["msg"].delete()
                removed += 1
        except discord.HTTPException:
            log.warning("Could not tidy job message %s", j["id"])
    note = " A job that was already running on your PC finishes its current step first." if cancelled else ""
    await i.followup.send(f"Cancelled {cancelled} waiting job(s) and removed {removed} finished job message(s).{note}", ephemeral=True)


@app_commands.command(name="pc", description="Is the Grabber agent on your PC connected?")
async def pc_cmd(i: discord.Interaction) -> None:
    await i.response.send_message((await bot_of(i).pc_status())["text"], ephemeral=True)


@app_commands.command(name="report", description="Post the Monday report now (uses the latest TikTok and Instagram numbers from your PC)")
async def report_cmd(i: discord.Interaction) -> None:
    b = bot_of(i)
    await i.response.defer(ephemeral=True)
    pc = await b.latest_weekdata(int(time.time()) - 8 * DAY)
    await b.post("weekly_report", embeds=await b.build_report(pc))
    await b.post("competitors", embed=await b.build_competitors())
    note = "" if pc else " There were no PC numbers from the last 8 days, so I also asked your PC for them. Run /report again once the week job is done."
    if not pc:
        await b.queue_job("week", await b.tag_arg())
    await i.followup.send("Posted the report and the competitor summary." + note, ephemeral=True)


@app_commands.command(name="tag", description="Set the code (like D1) for a YouTube, TikTok or Instagram post")
@app_commands.describe(link="Link to the post", code="Its code, like D1, T1 or M1")
async def tag_cmd(i: discord.Interaction, link: str, code: str) -> None:
    b = bot_of(i)
    ref = post_ref(link)
    code = code.strip().upper()
    if not ref or not re.fullmatch(r"[A-Z]{1,3}[0-9]{1,3}", code):
        await i.response.send_message("I need a YouTube, TikTok or Instagram post link and a code like D1.", ephemeral=True)
        return
    platform, pid = ref
    await b.store.run("INSERT OR REPLACE INTO tags VALUES (?,?,?,?)", (platform, pid, code, int(time.time())))
    if platform == "yt":
        await b.recode()
    where = {"yt": "YouTube", "tt": "TikTok", "ig": "Instagram"}[platform]
    await i.response.send_message(f"{where} post tagged `{code}`. A code you set here wins over the one in the description.", ephemeral=True)


rule_group = app_commands.Group(name="coderule", description="Rules that give YouTube videos a code automatically")


@rule_group.command(name="list", description="Show the automatic code rules")
async def rule_list(i: discord.Interaction) -> None:
    rules = await bot_of(i).code_rules()
    text = "\n".join(f"{k}. `{c}`: {p.replace('|', ', ')}" for k, (c, p) in enumerate(rules, 1))
    text += (f"\nNo match: `{config.FALLBACK_CODES[1]}` for Shorts, `{config.FALLBACK_CODES[0]}` for other videos."
             "\nThe first rule with a word in the title or a playlist name wins. A `code:` line or /tag always beats these. "
             "Codes starting with T count as weekly-topic videos.")
    await i.response.send_message(text[:1900], ephemeral=True)


@rule_group.command(name="add", description="Add or replace a rule: a code and the words that trigger it")
@app_commands.describe(code="Like M2", words="Comma-separated words or phrases, like: past simple, present perfect",
                       position="1 puts it first (it wins over the others); empty puts it last")
async def rule_add(i: discord.Interaction, code: str, words: str, position: int | None = None) -> None:
    b = bot_of(i)
    code = code.strip().upper()
    parts = [re.escape(w.strip()) for w in words.split(",") if w.strip()]
    if not re.fullmatch(r"[A-Z]{1,3}[0-9]{1,3}", code) or not parts:
        await i.response.send_message("I need a code like M2 and at least one word.", ephemeral=True)
        return
    rules = [r for r in await b.code_rules() if r[0] != code]
    pos = len(rules) if position is None else max(0, min(len(rules), position - 1))
    rules.insert(pos, (code, "|".join(parts)))
    await b.store.run("DELETE FROM code_rules")
    for k, (c, p) in enumerate(rules):
        await b.store.run("INSERT INTO code_rules VALUES (?,?,?)", (k, c, p))
    await b.recode()
    await i.response.send_message(f"Rule {pos + 1}: `{code}` for {', '.join(w.strip() for w in words.split(',') if w.strip())}. "
                                  "Codes on your videos are updated.", ephemeral=True)


@rule_group.command(name="remove", description="Remove the rule for a code")
async def rule_remove(i: discord.Interaction, code: str) -> None:
    b = bot_of(i)
    await b.store.run("DELETE FROM code_rules WHERE code=?", (code.strip().upper(),))
    await b.recode()
    await i.response.send_message(f"Removed the rule for `{code.strip().upper()}`.", ephemeral=True)


@app_commands.command(name="change", description="Set this week's one change, or see it when left empty")
@app_commands.describe(text="The one thing you change this week, like: hook in the first 2 seconds on every duet")
async def change_cmd(i: discord.Interaction, text: str | None = None) -> None:
    b = bot_of(i)
    wk = week_key(datetime.now(TZ).date())
    if text:
        await b.store.run("INSERT OR REPLACE INTO changes VALUES (?,?,?)", (wk, text.strip()[:300], int(time.time())))
        await i.response.send_message(f"This week's one change: {text.strip()[:300]}\nThe Monday report shows it next to the numbers.", ephemeral=True)
        return
    row = await b.store.one("SELECT text FROM changes WHERE week=?", (wk,))
    await i.response.send_message(f"This week's one change: {row['text']}" if row else "No change set this week. Use /change text:...", ephemeral=True)


@app_commands.command(name="help", description="What Tracker can do")
async def help_cmd(i: discord.Interaction) -> None:
    e = discord.Embed(title="Tracker", color=COLOR, description=(
        "**Automatic**\n"
        "New uploads and a 24h first look (yt channels). Ratings on subscribers at 7 and 28 days, against earlier videos "
        "of the same kind: Shorts, weekly-topic videos and other long videos. Outliers and flops (alerts).\n"
        "Monday report at 08:00: follows per platform and per format code, plus the week's one change. "
        "Competitor summary every Monday.\n"
        "Trends brief, video ideas and TikTok reports when your PC runs Grabber.\n\n"
        "**Codes**\n"
        "Every post gets a code. A `code: D1` line in the description or /tag wins; otherwise Tracker picks one from the "
        "title and playlist (shown with *, rules in /coderule list). Codes starting with T count as weekly-topic videos.\n\n"
        "**Commands**\n"
        "/stats, /video link, /shorts, /report, /tag link code, /change text, /coderule\n"
        "/social: last 7 days on TikTok, /instagram: last 7 days on Instagram (both run on your PC)\n"
        "/watchlist add, remove, list\n"
        "/trends, /ideas, /tiktok [handle], /queue, /clearqueue, /pc"))
    await i.response.send_message(embed=e, ephemeral=True)


# ------------------------------------------------------------ entry point

async def run_tracker(default_db_dir: Path) -> None:
    token = os.getenv("TRACKER_DISCORD_TOKEN", "").strip()
    if not token:
        log.info("TRACKER_DISCORD_TOKEN not set, Tracker stays off")
        return
    db_path = Path(os.getenv("TRACKER_DB_PATH", "") or (default_db_dir / "tracker.sqlite"))
    bot = Tracker(db_path)
    try:
        await bot.start(token)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Tracker stopped with an error")
    finally:
        if not bot.is_closed():
            await bot.close()

"""Tracker: YouTube stats, competitor watch, daily brief and a job queue for the Grabber agent on Lucas's PC.

Runs next to the arcade bot and TradingCow in the same process, with its own token and database.
The PC agent talks to Discord directly with the same bot token: it picks up JOB messages in
#bot-status, runs them locally (Trends via the extension, TikTok via the downloader) and posts results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config
from .config import CHANNELS, bucket
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


class Tracker(commands.Bot):
    def __init__(self, db_path: Path) -> None:
        super().__init__(command_prefix="!tracker-unused ", intents=discord.Intents.default(), help_command=None)
        self.store = Store(db_path)
        self.yt = YouTube()
        self.first_sync_done: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.store.connect()
        if not await self.store.one("SELECT 1 AS x FROM watchlist LIMIT 1"):
            for lang, handles in config.DEFAULT_TIKTOK_COMPETITORS.items():
                for h in handles:
                    await self.store.run("INSERT OR IGNORE INTO watchlist VALUES (?,?,?)", ("tiktok", h, lang))
        for cmd in (stats_cmd, video_cmd, trends_cmd, ideas_cmd, tiktok_cmd, queue_cmd, pc_cmd, brief_cmd, help_cmd):
            self.tree.add_command(cmd)
        self.tree.add_command(watch_group)
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.yt_loop.start()
        self.brief_loop.start()
        if not self.yt.enabled:
            log.warning("YOUTUBE_API_KEY not set: Tracker runs without YouTube stats")

    async def close(self) -> None:
        self.yt_loop.cancel()
        self.brief_loop.cancel()
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
            await self.ensure_channels()
            for c in await self.store.rows("SELECT * FROM channels"):
                await self.sync_channel(c)
            await self.check_milestones()
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
            await self.store.run("INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?,?,?)",
                                 (v["id"], c["channel_id"], v["snippet"]["title"], parse_ts(v["snippet"]["publishedAt"]),
                                  dur, int(dur <= 180), 0 if known_before else 1))
        # Fresh numbers for everything published in the last 35 days.
        recent = await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND published>=?", (c["channel_id"], now - 35 * DAY))
        for v in await self.yt.videos([r["video_id"] for r in recent]):
            s = v.get("statistics", {})
            await self.store.snapshot(v["id"], int(s.get("viewCount", 0) or 0), int(s.get("likeCount", 0) or 0),
                                      int(s.get("commentCount", 0) or 0))
        for r in await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND announced=0", (c["channel_id"],)):
            await self.announce(c, r)
            await self.store.run("UPDATE videos SET announced=1 WHERE video_id=?", (r["video_id"],))

    async def announce(self, c: dict, v: dict) -> None:
        url = f"https://youtu.be/{v['video_id']}"
        if c["own_key"]:
            e = discord.Embed(title=f"New upload: {v['title']}", url=url, color=COLOR,
                              description=f"{fmt_label(v['is_short'])}, published <t:{v['published']}:R>. "
                                          f"First check-in after 24 hours.")
            await self.post("yt_en" if c["own_key"] == "en" else "yt_nl", embed=e)
        else:
            e = discord.Embed(title=v["title"], url=url, color=0x8D8C84,
                              description=f"**{c['title']}** posted a {fmt_label(v['is_short']).lower()} <t:{v['published']}:R>.")
            await self.post("competitors", embed=e)

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
                base, size, rough = await self.baseline(v["channel_id"], v["is_short"], name, v["video_id"])
                ratio = views / base if base else None
                if own:
                    await self.post_milestone(v, name, views, base, size, rough, ratio)
                elif ratio is not None and ratio >= 3.6:
                    e = discord.Embed(title=f"Competitor outlier: {v['title']}", url=f"https://youtu.be/{v['video_id']}", color=0xE0A53A,
                                      description=f"**{v['channel_title']}**: {n(views)} views after 7 days, "
                                                  f"{ratio:.1f}x their usual {fmt_label(v['is_short']).lower()} ({n(base)}"
                                                  f"{', rough baseline' if rough else ''}). Worth a look at the title, thumbnail and hook.")
                    await self.post("competitors", embed=e)

    async def post_milestone(self, v, name, views, base, size, rough, ratio) -> None:
        b = bucket(ratio)
        snap = await self.store.one("SELECT likes, comments FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
        e = discord.Embed(title=f"{name} check: {v['title']}", url=f"https://youtu.be/{v['video_id']}", color=COLOR)
        e.add_field(name="Views", value=n(views))
        e.add_field(name="Usual at this point", value=(n(base) + (" (rough)" if rough else "") + f"\n{size} videos") if base else "not enough data")
        e.add_field(name="Result", value=f"**{b}**" + (f" ({ratio:.2f}x)" if ratio else ""))
        if snap:
            e.add_field(name="Likes / comments", value=f"{n(snap['likes'])} / {n(snap['comments'])}")
        key = v["own_key"]
        if self.yt.analytics_enabled(key):
            try:
                a = await self.yt.video_analytics(key, v["video_id"], datetime.fromtimestamp(v["published"], TZ).date())
                if a.get("videoThumbnailImpressions") is not None:
                    e.add_field(name="Impressions / CTR", value=f"{n(a['videoThumbnailImpressions'])} / {pct(a.get('videoThumbnailImpressionsClickRate'))}")
                if a.get("averageViewPercentage") is not None:
                    e.add_field(name="Avg viewed", value=f"{pct(a['averageViewPercentage'])} ({n(a.get('averageViewDuration'))}s)")
                if a.get("subscribersGained") is not None:
                    e.add_field(name="Subs gained", value=n(a["subscribersGained"]))
                if a.get("traffic"):
                    e.add_field(name="Traffic", value=", ".join(f"{s.replace('_', ' ').lower()} {p:.0f}%" for s, p in a["traffic"][:4]), inline=False)
            except Exception:
                log.exception("Video analytics failed for %s", v["video_id"])
        e.set_footer(text=f"{fmt_label(v['is_short'])}. Buckets: outlier 3.6x+, over 1.6x+, par 0.72x+, under 0.5x+, flop below.")
        await self.post("yt_en" if key == "en" else "yt_nl", embed=e)
        if b in ("outlier", "flop"):
            await self.post("alerts", content=f"{'Outlier' if b == 'outlier' else 'Flop'} at {name}: **{v['title']}**, "
                                              f"{n(views)} views vs usual {n(base)} ({ratio:.2f}x). https://youtu.be/{v['video_id']}")

    # ------------------------------------------------------------ daily brief
    @tasks.loop(minutes=5)
    async def brief_loop(self) -> None:
        now = datetime.now(TZ)
        if now.hour != config.DAILY_BRIEF_HOUR:
            return
        today = now.date().isoformat()
        if await self.store.get("brief_sent") == today:
            return
        await self.store.set("brief_sent", today)
        try:
            await self.post("daily_brief", embed=await self.build_brief())
        except Exception:
            log.exception("Daily brief failed")

    @yt_loop.before_loop
    @brief_loop.before_loop
    async def _wait(self) -> None:
        await self.wait_until_ready()

    async def channel_delta(self, channel_id: str, seconds: int) -> tuple[dict | None, dict | None]:
        now = int(time.time())
        cur = await self.store.one("SELECT * FROM channel_stats WHERE channel_id=? ORDER BY ts DESC LIMIT 1", (channel_id,))
        old = await self.store.one("SELECT * FROM channel_stats WHERE channel_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (channel_id, now - seconds))
        return cur, old

    async def build_brief(self) -> discord.Embed:
        now = int(time.time())
        e = discord.Embed(title=f"Daily brief, {datetime.now(TZ).strftime('%A %d %B')}", color=COLOR)
        for c in await self.store.rows("SELECT * FROM channels WHERE own_key IS NOT NULL ORDER BY own_key"):
            cur, old = await self.channel_delta(c["channel_id"], DAY)
            lines = []
            if cur:
                sub_d = f" ({cur['subs'] - old['subs']:+,})" if old else ""
                view_d = f", {cur['views'] - old['views']:+,} views in 24h" if old else ""
                lines.append(f"{n(cur['subs'])} subscribers{sub_d}{view_d}")
            if self.yt.analytics_enabled(c["own_key"]):
                day = await self.yt.channel_day(c["own_key"])
                if day:
                    lines.append(f"Studio, 2 days ago: {n(day.get('views'))} views, {n((day.get('estimatedMinutesWatched') or 0) / 60)} watch hours, "
                                 f"+{n(day.get('subscribersGained'))} / -{n(day.get('subscribersLost'))} subs")
            week = await self.store.rows("SELECT * FROM videos WHERE channel_id=? AND published>=? ORDER BY published DESC",
                                         (c["channel_id"], now - 7 * DAY))
            for v in week[:6]:
                snap = await self.store.one("SELECT views FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
                ms = await self.store.one("SELECT milestone, views FROM milestones WHERE video_id=? AND views IS NOT NULL ORDER BY ts DESC LIMIT 1", (v["video_id"],))
                tag = ""
                if ms:
                    base, _, _ = await self.baseline(c["channel_id"], v["is_short"], ms["milestone"], v["video_id"])
                    tag = f" [{ms['milestone']}: {bucket(ms['views'] / base if base else None)}]"
                lines.append(f"[{v['title'][:60]}](https://youtu.be/{v['video_id']}): {n(snap['views'] if snap else None)} views{tag}")
            if not week:
                lines.append("No uploads in the last 7 days.")
            e.add_field(name=c["title"], value="\n".join(lines)[:1024] or "-", inline=False)
        comp = await self.store.rows(
            "SELECT v.*, c.title AS ct FROM videos v JOIN channels c ON c.channel_id=v.channel_id "
            "WHERE c.own_key IS NULL AND v.published>=?", (now - DAY,))
        if comp:
            best = []
            for v in comp:
                s = await self.store.one("SELECT views FROM snapshots WHERE video_id=? ORDER BY ts DESC LIMIT 1", (v["video_id"],))
                hours = max(1, (now - v["published"]) / HOUR)
                best.append(((s["views"] if s else 0) / hours, v))
            best.sort(key=lambda x: x[0], reverse=True)
            e.add_field(name=f"Competitors: {len(comp)} new YouTube upload(s)", inline=False,
                        value="\n".join(f"{v['ct']}: [{v['title'][:60]}](https://youtu.be/{v['video_id']}), {n(r)} views/hour" for r, v in best[:3]))
        pc = await self.pc_status()
        e.add_field(name="PC agent", inline=False, value=pc["text"])
        queued = [j for j in await self.recent_jobs() if j["status"] == "queued"]
        if queued:
            e.add_field(name="Waiting for your PC", value=", ".join(f"#{j['id']} {j['kind']}" for j in queued[:10]), inline=False)
        if not self.yt.enabled:
            e.description = "YouTube numbers are off until YOUTUBE_API_KEY is set on Render."
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
        ms = await b.store.one("SELECT milestone, views FROM milestones WHERE video_id=? AND views IS NOT NULL ORDER BY ts DESC LIMIT 1", (v["video_id"],))
        tag = ""
        if ms:
            base, _, _ = await b.baseline(c["channel_id"], v["is_short"], ms["milestone"], v["video_id"])
            tag = f", {ms['milestone']} {bucket(ms['views'] / base if base else None)}"
        lines.append(f"{'S' if v['is_short'] else 'L'} [{v['title'][:55]}](https://youtu.be/{v['video_id']}): {n(s['views'] if s else None)}{tag}")
    e.add_field(name="Latest videos (S = Short, L = long-form)", value="\n".join(lines)[:1024] or "-", inline=False)
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
    if row:
        for name, _ in MILESTONES:
            ms = await b.store.one("SELECT views FROM milestones WHERE video_id=? AND milestone=?", (vid, name))
            if ms and ms["views"] is not None:
                base, _, rough = await b.baseline(row["channel_id"], row["is_short"], name, vid)
                e.add_field(name=f"At {name}", value=f"{n(ms['views'])}, {bucket(ms['views'] / base if base else None)}")
        if row["own_key"] and b.yt.analytics_enabled(row["own_key"]):
            a = await b.yt.video_analytics(row["own_key"], vid, datetime.fromtimestamp(pub, TZ).date())
            if a.get("videoThumbnailImpressionsClickRate") is not None:
                e.add_field(name="CTR", value=pct(a["videoThumbnailImpressionsClickRate"]))
            if a.get("averageViewPercentage") is not None:
                e.add_field(name="Avg viewed", value=pct(a["averageViewPercentage"]))
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


@app_commands.command(name="queue", description="Jobs waiting for or running on your PC")
async def queue_cmd(i: discord.Interaction) -> None:
    jobs = await bot_of(i).recent_jobs()
    text = "\n".join(f"#{j['id']} {j['kind']} {j['arg'][:40]}: {j['status']}" for j in jobs[:15]) or "No jobs yet."
    await i.response.send_message(text, ephemeral=True)


@app_commands.command(name="pc", description="Is the Grabber agent on your PC connected?")
async def pc_cmd(i: discord.Interaction) -> None:
    await i.response.send_message((await bot_of(i).pc_status())["text"], ephemeral=True)


@app_commands.command(name="brief", description="Post the daily brief now")
async def brief_cmd(i: discord.Interaction) -> None:
    b = bot_of(i)
    await i.response.defer(ephemeral=True)
    await b.post("daily_brief", embed=await b.build_brief())
    await i.followup.send("Posted in daily-brief.", ephemeral=True)


@app_commands.command(name="help", description="What Tracker can do")
async def help_cmd(i: discord.Interaction) -> None:
    e = discord.Embed(title="Tracker", color=COLOR, description=(
        "**Automatic**\n"
        "New uploads, then 24h, 7d and 28d check-ins for every video, compared with your usual (yt channels)\n"
        "Outliers and flops (alerts). Competitor uploads and outliers (competitors). Daily brief at 08:00.\n"
        "Trends brief, video ideas and TikTok reports when your PC runs Grabber.\n\n"
        "**Commands**\n"
        "/stats, /video link, /brief\n"
        "/watchlist add, remove, list\n"
        "/trends, /ideas, /tiktok [handle], /queue, /pc"))
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

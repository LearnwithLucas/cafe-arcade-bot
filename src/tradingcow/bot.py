"""TradingCow Discord bot: GE prices, flips, weekly dump alerts, skill profit and price watches.

Runs next to the arcade bot in the same process, with its own token and database.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import market
from .store import Store
from .wiki import Wiki

log = logging.getLogger("tradingcow")

COLOR = 0xE0A53A
HOUR = 3600

# Where each kind of message goes. Override any of them with an environment variable.
CHANNELS = {
    "dumps": int(os.getenv("TRADINGCOW_CHANNEL_DUMPS", "1552974535568138250")),
    "f2p": int(os.getenv("TRADINGCOW_CHANNEL_F2P", "1552974595764658277")),
    "p2p": int(os.getenv("TRADINGCOW_CHANNEL_P2P", "1552974638777245726")),
    "general": int(os.getenv("TRADINGCOW_CHANNEL_GENERAL", "1552970635247095939")),
}
BOARD_CASH = int(os.getenv("TRADINGCOW_BOARD_CASH", "10000000"))
BOARD_MIN_PROFIT = int(os.getenv("TRADINGCOW_BOARD_MIN_PROFIT", "10000"))
GUIDE_HOUR = int(os.getenv("TRADINGCOW_GUIDE_HOUR", "10"))  # local time in Amsterdam
TZ = ZoneInfo("Europe/Amsterdam")


def guide_text(kind: str, dump_pct: int) -> tuple[str, str]:
    """Title and text of the daily explainer for one channel kind."""
    cash = n(BOARD_CASH / 1e6)
    profit = n(BOARD_MIN_PROFIT / 1e3)
    return {
        "dumps": ("About this channel: sudden dumps",
                  f"TradingCow posts here when an item suddenly trades at least {dump_pct}% under its 7-day average, "
                  "while it was still normal 24 hours ago.\n\n"
                  "**How to read a post**\n"
                  "Now vs 7-day avg: how far the price fell.\n"
                  "Last hour and sell volume: how fast it is falling and how many people are selling.\n"
                  "Back to average: what you make per item, after the 2% GE tax, if the price recovers.\n"
                  "Limit: the GE buy limit every 4 hours.\n\n"
                  "A dump can keep falling (game update, bot ban wave, item change). Check the news and the chart before you buy. "
                  "Each item posts at most once a day. Use /dumps for the full weekly list."),
        "f2p": ("About this channel: F2P flips",
                f"The live board above shows the best free-to-play flips for one GE slot and up to {cash}m, "
                f"each making at least {profit}k and likely to fill within about 6 hours. It refreshes every 10 minutes.\n\n"
                "**How to read a flip**\n"
                "Buy at the first price, sell at the second. Profit is after the 2% GE tax.\n"
                "Fill: about how long buying and selling take at that size.\n"
                "Risk: low, medium or high, based on how steady the price and volume are.\n\n"
                "Use /flips here with your own cash, free slots and patience. It defaults to F2P items in this channel."),
        "p2p": ("About this channel: members flips",
                f"The live board above shows the best members-only flips for one GE slot and up to {cash}m, "
                f"each making at least {profit}k and likely to fill within about 6 hours. It refreshes every 10 minutes.\n\n"
                "**How to read a flip**\n"
                "Buy at the first price, sell at the second. Profit is after the 2% GE tax.\n"
                "Fill: about how long buying and selling take at that size.\n"
                "Risk: low, medium or high, based on how steady the price and volume are.\n\n"
                "Use /flips here with your own cash, free slots and patience. It defaults to members items in this channel."),
        "general": ("About TradingCow",
                    "TradingCow tracks Grand Exchange prices from the OSRS Wiki and never touches your account.\n\n"
                    "**Commands**\n"
                    "/price item: live prices, margin after tax and the 7-day average.\n"
                    "/flips cash: the best flips for your cash, slots and patience.\n"
                    "/alch: the best items to high alch right now.\n"
                    "/dumps: items well under their weekly average.\n"
                    "/skill skill: the 10 most profitable things to do in a skill right now.\n"
                    "/watch add: get pinged here when an item crosses your price.\n\n"
                    "Sudden dumps, F2P flips and members flips each have their own channel."),
    }[kind]


def n(v) -> str:
    try:
        return f"{round(v):,}"
    except (TypeError, ValueError):
        return "-"


def signed(v) -> str:
    return ("+" if v > 0 else "") + n(v)


def hrs(h: float) -> str:
    if h is None or h != h or h == float("inf"):
        return "-"
    if h < 1:
        return f"{max(1, round(h * 60))}m"
    return f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d"


def parse_gp(text: str) -> int | None:
    t = str(text).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", t)
    if not m:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2)]
    return int(float(m.group(1)) * mult)


class TradingCow(commands.Bot):
    def __init__(self, db_path: Path, guild_ids: list[int], alert_channel_id: int | None, dump_min_drop: float) -> None:
        super().__init__(command_prefix="!tc-unused ", intents=discord.Intents.default(), help_command=None)
        self.store = Store(db_path)
        self.wiki = Wiki()
        self.guild_ids = guild_ids
        self.default_alert_channel = alert_channel_id
        self.dump_min_drop = dump_min_drop
        self.mapping: dict[int, dict] = {}
        self.by_name: dict[str, int] = {}
        self.latest: dict = {}
        self.h1: dict = {}
        self.d1: dict = {}
        self.latest_at = 0.0
        self.mapping_at = 0.0
        self.d1_at = 0.0
        self.backfill_task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.store.connect()
        await self.refresh_prices(force=True)
        self.tree.add_command(watch_group)
        for cmd in (price_cmd, flips_cmd, alch_cmd, dumps_cmd, skill_cmd, channel_cmd, channels_cmd, guide_cmd, help_cmd):
            self.tree.add_command(cmd)
        if self.guild_ids:
            for gid in self.guild_ids:
                g = discord.Object(id=gid)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            log.info("TradingCow commands synced to %d guild(s)", len(self.guild_ids))
        else:
            await self.tree.sync()
            log.info("TradingCow commands synced globally (can take up to an hour to appear)")
        self.price_loop.start()
        self.hour_loop.start()
        self.dump_loop.start()
        self.board_loop.start()
        self.guide_loop.start()
        self.backfill_task = asyncio.create_task(self.backfill())

    async def close(self) -> None:
        for loop in (self.price_loop, self.hour_loop, self.dump_loop, self.board_loop, self.guide_loop):
            loop.cancel()
        if self.backfill_task:
            self.backfill_task.cancel()
        await self.wiki.close()
        await self.store.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("TradingCow logged in as %s", self.user)

    # ------------------------------------------------------------ data
    async def refresh_prices(self, force: bool = False) -> None:
        now = time.time()
        try:
            if force or not self.mapping or now - self.mapping_at > 24 * HOUR:
                self.mapping = await self.wiki.mapping()
                self.by_name = {m["name"].lower(): i for i, m in self.mapping.items()}
                self.mapping_at = now
            self.latest = await self.wiki.latest()
            _, self.h1 = await self.wiki.hour()
            self.latest_at = now
            if force or now - self.d1_at > 10 * 60:
                self.d1 = await self.wiki.day()
                self.d1_at = now
        except Exception:
            log.exception("TradingCow price refresh failed")

    async def backfill(self) -> None:
        """Fill in any missing hours from the last 7 days, gently (about one request a second)."""
        await self.wait_until_ready()
        now_hour = int(time.time()) // HOUR * HOUR
        have = await self.store.hours_stored(now_hour - 170 * HOUR)
        missing = [now_hour - k * HOUR for k in range(1, 169) if now_hour - k * HOUR not in have]
        if missing:
            log.info("TradingCow backfilling %d hours of prices", len(missing))
        for ts in missing:
            try:
                got_ts, data = await self.wiki.hour(ts)
                await self.store.add_hour(got_ts, data)
            except Exception:
                log.warning("Backfill failed for hour %s", ts, exc_info=True)
            await asyncio.sleep(1.2)
        if missing:
            log.info("TradingCow backfill done")

    @tasks.loop(seconds=60)
    async def price_loop(self) -> None:
        await self.refresh_prices()
        await self.check_watches()

    @tasks.loop(minutes=10)
    async def hour_loop(self) -> None:
        try:
            ts, data = await self.wiki.hour()
            have = await self.store.hours_stored(ts)
            if ts not in have:
                count = await self.store.add_hour(ts, data)
                log.info("TradingCow stored hour %s (%d items)", ts, count)
        except Exception:
            log.exception("TradingCow hour collection failed")

    @tasks.loop(minutes=15)
    async def dump_loop(self) -> None:
        try:
            channel = await self.channel("dumps")
            if not channel:
                return
            history = await self.store.history(int(time.time()) - 8 * 24 * HOUR)
            dumps = market.find_dumps(self.mapping, self.latest, history, min_drop=self.dump_min_drop)
            for d in dumps:
                if not d.sudden or d.upside_each <= 0:
                    continue
                if not await self.store.once(f"dump:{d.item_id}", 24 * HOUR):
                    continue
                await channel.send(embed=dump_embed([d], "Sudden dump"))
        except Exception:
            log.exception("TradingCow dump scan failed")

    @tasks.loop(minutes=10)
    async def board_loop(self) -> None:
        """Keep one live flip board per channel: the message is edited, not reposted."""
        for kind in ("f2p", "p2p"):
            try:
                channel = await self.channel(kind)
                if not channel or not self.latest:
                    continue
                rows = market.flips(self.mapping, self.latest, self.h1, self.d1, cash=BOARD_CASH, slots=1, max_hours=6,
                                    min_profit=BOARD_MIN_PROFIT, f2p=kind == "f2p", max_risk="medium",
                                    members_only=kind == "p2p")[:12]
                embed = flips_embed(rows, f"{'F2P' if kind == 'f2p' else 'Members'} flips, live board",
                                    f"Best patient flips for one GE slot and up to {n(BOARD_CASH / 1e6)}m, filled within about 6h. "
                                    f"Updated <t:{int(time.time())}:R>. Use /flips for your own cash and slots.")
                key = f"board:{kind}:{channel.id}"
                msg_id = await self.store.get(key)
                msg = None
                if msg_id:
                    try:
                        msg = await channel.fetch_message(int(msg_id))
                    except discord.HTTPException:
                        msg = None
                if msg:
                    await msg.edit(embed=embed)
                else:
                    msg = await channel.send(embed=embed)
                    await self.store.set(key, str(msg.id))
            except Exception:
                log.exception("TradingCow %s board update failed", kind)

    @tasks.loop(minutes=15)
    async def guide_loop(self) -> None:
        """Once a day (and right away if a channel has none yet), repost the explainer at the bottom of each channel."""
        now = datetime.now(TZ)
        last = float(await self.store.get("guide:last", "0") or 0)
        first_run = not await self.store.get("guide:posted")
        if first_run or (now.hour == GUIDE_HOUR and time.time() - last > 20 * HOUR):
            await self.post_guides()

    async def post_guides(self) -> int:
        """Delete yesterday's explainer and post a fresh one, so it sits at the bottom. Returns the channels done."""
        by_channel: dict[int, tuple] = {}
        for kind in ("dumps", "f2p", "p2p", "general"):
            ch = await self.channel(kind)
            if ch:
                by_channel.setdefault(ch.id, (ch, []))[1].append(kind)
        done = 0
        for cid, (ch, kinds) in by_channel.items():
            try:
                old = await self.store.get(f"guide:msg:{cid}")
                if old:
                    try:
                        await (await ch.fetch_message(int(old))).delete()
                    except discord.HTTPException:
                        pass
                embeds = []
                for kind in kinds:
                    title, text = guide_text(kind, round(self.dump_min_drop * 100))
                    embeds.append(discord.Embed(title=title, description=text, color=COLOR))
                embeds[-1].set_footer(text="This explainer is reposted once a day.")
                msg = await ch.send(embeds=embeds)
                await self.store.set(f"guide:msg:{cid}", str(msg.id))
                done += 1
            except Exception:
                log.exception("TradingCow guide post failed in %s", cid)
        await self.store.set("guide:last", str(time.time()))
        await self.store.set("guide:posted", "1")
        return done

    @price_loop.before_loop
    @hour_loop.before_loop
    @dump_loop.before_loop
    @board_loop.before_loop
    @guide_loop.before_loop
    async def _wait(self) -> None:
        await self.wait_until_ready()

    async def channel(self, kind: str):
        """Channel for a message kind: set with /tc_channel first, then the environment variable, then the default."""
        raw = await self.store.get(f"channel:{kind}")
        if not raw and kind == "dumps":
            raw = await self.store.get("alert_channel")  # set with the old /tc_alerts
        cid = int(raw) if raw else CHANNELS.get(kind)
        if not cid:
            return None
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    async def check_watches(self) -> None:
        if not self.latest:
            return
        for w in await self.store.watches():
            L = self.latest.get(str(w["item_id"]))
            if not L:
                continue
            v = L.get("high") if w["use_high"] else L.get("low")
            if not v:
                continue
            if w["net"]:
                v = market.net_price(w["item_id"], v)
            hit = v < w["price"] if w["below"] else v > w["price"]
            if hit and not w["fired"]:
                await self.store.set_fired(w["id"], True)
                side = "instant buy" if w["use_high"] else "instant sell"
                msg = (f"<@{w['user_id']}> **{w['item_name']}** {side}{' after tax' if w['net'] else ''} is "
                       f"{n(v)} ({'<' if w['below'] else '>'} {n(w['price'])}). Watch #{w['id']}.")
                await self.deliver(w["user_id"], w["channel_id"], msg)
            elif not hit and w["fired"]:
                await self.store.set_fired(w["id"], False)

    async def deliver(self, user_id: int, channel_id: int | None, text: str) -> None:
        """Watch alerts go to the channel picked with here:True, else the general channel, else a DM."""
        ch = self.get_channel(channel_id) if channel_id else await self.channel("general")
        if ch:
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions(users=True))
                return
            except discord.HTTPException:
                pass
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(text)
        except discord.HTTPException:
            log.warning("Could not deliver watch alert to %s", user_id)

    # helpers for commands
    def find_item(self, text: str) -> int | None:
        t = text.strip().lower()
        if t.isdigit() and int(t) in self.mapping:
            return int(t)
        if t in self.by_name:
            return self.by_name[t]
        starts = [i for name, i in self.by_name.items() if name.startswith(t)]
        return starts[0] if len(starts) == 1 else None

    def quote(self, item_id: int):
        return market.quote(item_id, self.latest, self.h1, self.d1)


def bot_of(interaction: discord.Interaction) -> TradingCow:
    return interaction.client  # type: ignore[return-value]


async def item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    b = bot_of(interaction)
    cur = current.lower().strip()
    if not cur:
        return []
    starts = [m["name"] for m in b.mapping.values() if m["name"].lower().startswith(cur)]
    contains = [m["name"] for m in b.mapping.values() if cur in m["name"].lower() and not m["name"].lower().startswith(cur)]
    return [app_commands.Choice(name=x, value=x) for x in (sorted(starts, key=len) + sorted(contains, key=len))[:25]]


def flips_embed(rows: list[market.Flip], title: str, description: str | None = None) -> discord.Embed:
    e = discord.Embed(title=title, color=COLOR, description=description)
    if not rows:
        e.description = (description + "\n\n" if description else "") + "Nothing passes the filters right now."
    for f in rows:
        flags = f" ({', '.join(l for _, l in f.q.flags)})" if f.q.flags else ""
        e.add_field(name=f"{f.name}: {signed(f.profit)}",
                    value=f"Buy {n(f.qty)} at {n(f.q.p_buy)}, sell at {n(f.q.p_sell)} ({signed(f.q.net)} each). "
                          f"Fill about {hrs(f.hours)}. Risk {f.q.risk}{flags}.", inline=False)
    return e


def dump_embed(dumps: list[market.Dump], title: str) -> discord.Embed:
    e = discord.Embed(title=title, color=0xE66767)
    for d in dumps[:10]:
        kind = "sudden (was normal 24h ago)" if d.sudden else "slide over the week"
        e.add_field(
            name=f"{d.name}  -{d.drop_pct:.0f}%",
            value=(f"Now {n(d.cur_low)} vs 7-day avg {n(d.base_low)}. Last hour -{d.hour_drop_pct:.0f}%, "
                   f"sell volume {d.sell_vol_ratio:.1f}x normal. {kind}.\n"
                   f"Back to average: **{signed(d.upside_each)} each** after tax ({d.upside_pct:.0f}%). "
                   f"Limit {n(d.limit) if d.limit else '?'}, trades ~{n(d.daily_gp_volume / 1e6)}m gp a day."),
            inline=False)
    e.set_footer(text="A dump can keep falling. Check the news and the chart before buying.")
    return e


# ------------------------------------------------------------ commands

@app_commands.command(name="price", description="Live GE price, margin after tax, volume and 7-day average")
@app_commands.describe(item="Item name")
@app_commands.autocomplete(item=item_autocomplete)
async def price_cmd(interaction: discord.Interaction, item: str) -> None:
    b = bot_of(interaction)
    iid = b.find_item(item)
    if iid is None:
        await interaction.response.send_message(f"I can't find an item called {item}.", ephemeral=True)
        return
    q = b.quote(iid)
    m = b.mapping[iid]
    if not q:
        await interaction.response.send_message(f"No recent trades for {m['name']}.", ephemeral=True)
        return
    hist = (await b.store.history(int(time.time()) - 7 * 24 * HOUR, iid)).get(iid, [])
    lows = [(r[2], r[4]) for r in hist if r[2] and r[4]]
    week = sum(p * v for p, v in lows) / sum(v for _, v in lows) if lows else None
    e = discord.Embed(title=m["name"], color=COLOR, url=f"https://prices.runescape.wiki/osrs/item/{iid}")
    e.add_field(name="Instant buy", value=n(q.inst_buy))
    e.add_field(name="Instant sell", value=f"{n(q.inst_sell)} ({n(market.net_price(iid, q.inst_sell))} after tax)")
    e.add_field(name="Patient flip", value=f"buy {n(q.p_buy)}, sell {n(q.p_sell)}: **{signed(q.net)} each**")
    e.add_field(name="Volume per hour", value=f"{n(q.vol_buy_side)} sold into, {n(q.vol_sell_side)} bought")
    e.add_field(name="7-day avg sell", value=n(week) if week else "collecting data")
    e.add_field(name="Buy limit", value=n(m.get("limit")) if m.get("limit") else "?")
    e.set_footer(text=f"{q.classify()}{' - ' + ', '.join(l for _, l in q.flags) if q.flags else ''} - price {hrs(q.age_min / 60)} old")
    await interaction.response.send_message(embed=e)


@app_commands.command(name="flips", description="Best flips for your cash, free slots and patience")
@app_commands.describe(cash="Cash to use, e.g. 1.5m", slots="Free GE slots", speed="How long you can wait",
                       items="F2P, members or all items (default: members in the P2P channel, F2P elsewhere)",
                       risk="Highest risk to allow", min_profit="Minimum profit per slot",
                       min_margin="Minimum profit per item in gp (default 5)", min_roi="Minimum profit per item in % (default 1)")
@app_commands.choices(speed=[app_commands.Choice(name=x, value=v) for x, v in
                             [("Fast (under 1h)", "1"), ("Medium (up to 6h)", "6"), ("Overnight (up to 16h)", "16")]],
                      risk=[app_commands.Choice(name=x, value=x) for x in ("low", "medium", "high")],
                      items=[app_commands.Choice(name="F2P items", value="f2p"),
                             app_commands.Choice(name="Members items", value="p2p"),
                             app_commands.Choice(name="All items", value="all")])
async def flips_cmd(interaction: discord.Interaction, cash: str, slots: app_commands.Range[int, 1, 8] = 3,
                    speed: str = "6", items: str | None = None, risk: str = "medium", min_profit: str = "5k",
                    min_margin: app_commands.Range[int, 1, 1_000_000] = 5,
                    min_roi: app_commands.Range[float, 0.0, 50.0] = 1.0) -> None:
    b = bot_of(interaction)
    c, mp = parse_gp(cash), parse_gp(min_profit)
    if c is None or mp is None:
        await interaction.response.send_message("Use amounts like 1500000, 1.5m or 800k.", ephemeral=True)
        return
    if items is None:
        items = "p2p" if interaction.channel_id == CHANNELS["p2p"] else "f2p"
    rows = market.flips(b.mapping, b.latest, b.h1, b.d1, cash=c, slots=slots, max_hours=float(speed),
                        min_profit=mp, f2p=items == "f2p", max_risk=risk, min_margin=min_margin,
                        min_roi=min_roi / 100, members_only=items == "p2p")[:10]
    label = {"f2p": "F2P", "p2p": "Members", "all": "All"}[items]
    e = flips_embed(rows, f"{label} flips for {n(c)} over {slots} slot(s)")
    e.set_footer(text=f"At least {min_margin} gp and {min_roi:g}% profit per item. Fill times assume you catch a quarter of the hourly volume.")
    await interaction.response.send_message(embed=e)


@app_commands.command(name="alch", description="Best items to buy and high alch right now")
@app_commands.describe(items="F2P, members or all items (default: members in the P2P channel, F2P elsewhere)",
                       buy="Buy instantly (default) or with a patient offer")
@app_commands.choices(items=[app_commands.Choice(name="F2P items", value="f2p"),
                             app_commands.Choice(name="Members items", value="p2p"),
                             app_commands.Choice(name="All items", value="all")],
                      buy=[app_commands.Choice(name="Instantly", value="instant"),
                           app_commands.Choice(name="Patient offer", value="patient")])
async def alch_cmd(interaction: discord.Interaction, items: str | None = None, buy: str = "instant") -> None:
    b = bot_of(interaction)
    if items is None:
        p2p = await b.channel("p2p")
        items = "p2p" if p2p and interaction.channel_id == p2p.id else "f2p"
    rows = market.alchs(b.mapping, b.latest, b.h1, b.d1, f2p=items == "f2p", members_only=items == "p2p",
                        instant=buy == "instant")[:10]
    label = {"f2p": "F2P", "p2p": "Members", "all": "All"}[items]
    e = discord.Embed(title=f"{label} high alchs right now", color=COLOR)
    if not rows:
        e.description = "Nothing makes a profit after the nature rune right now."
    for a in rows:
        limit = f"limit {n(a.limit)} per 4h" if a.limit else "no known limit"
        e.add_field(name=f"{a.name}: {signed(a.profit)} each",
                    value=f"Buy at {n(a.buy)}, alchs for {n(a.alch)}. About {n(a.per_hour)} casts/h, "
                          f"**{signed(a.profit_hour)}/h**. {limit.capitalize()}.", inline=False)
    nature = rows[0].nature if rows else None
    e.set_footer(text=(f"Profit is after one nature rune ({n(nature)} gp). " if nature else "")
                 + "Casts per hour are capped by 1,200 casts, the buy limit and a quarter of the hourly volume. Needs 55 Magic.")
    await interaction.response.send_message(embed=e)


@app_commands.command(name="dumps", description="Items trading well under their average (weekly by default)")
@app_commands.describe(days="Baseline length in days (1 to 7)", min_drop="Minimum drop in %", f2p="F2P items only",
                       min_volume="Last hour's selling vs normal, e.g. 1.5 = 50% more than usual")
async def dumps_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 7] = 7,
                    min_drop: app_commands.Range[int, 3, 80] = 10, f2p: bool = False,
                    min_volume: app_commands.Range[float, 0.0, 20.0] = 1.5) -> None:
    b = bot_of(interaction)
    await interaction.response.defer()
    history = await b.store.history(int(time.time()) - (days + 1) * 24 * HOUR)
    stored = await b.store.hours_stored(int(time.time()) - days * 24 * HOUR)
    rows = market.find_dumps(b.mapping, b.latest, history, min_drop=min_drop / 100, f2p=f2p, window_hours=days * 24,
                             min_sell_ratio=min_volume)
    e = dump_embed(rows, f"Dumps vs {days}-day average")
    if not rows:
        e.description = "No dumps right now."
    if len(stored) < days * 24 * 0.8:
        e.description = (e.description or "") + f"\nStill collecting history: {len(stored)} of {days * 24} hours stored."
    await interaction.followup.send(embed=e)


@app_commands.command(name="skill", description="Top 10 most profitable things to do in a skill right now")
@app_commands.describe(skill="Skill", level="Your level (empty: show every method with its level)",
                       items="F2P, members or all methods (default: F2P in the F2P channel, all elsewhere)",
                       sort="Most gp per hour (default), most gp per XP, or fastest XP that still profits",
                       instant="Use instant prices instead of patient offers")
@app_commands.choices(skill=[app_commands.Choice(name=s, value=s) for s in market.SKILLS],
                      items=[app_commands.Choice(name="F2P methods", value="f2p"),
                             app_commands.Choice(name="Members methods", value="p2p"),
                             app_commands.Choice(name="All methods", value="all")],
                      sort=[app_commands.Choice(name="Most gp per hour", value="hour"),
                            app_commands.Choice(name="Most gp per XP", value="gp"),
                            app_commands.Choice(name="Fastest XP that still profits", value="xp")])
async def skill_cmd(interaction: discord.Interaction, skill: str, level: app_commands.Range[int, 1, 99] | None = None,
                    items: str | None = None, sort: str = "hour", instant: bool = False) -> None:
    b = bot_of(interaction)
    if items is None:
        f2p_ch = await b.channel("f2p")
        items = "f2p" if f2p_ch and interaction.channel_id == f2p_ch.id else "all"
    pool = [m for m in market.METHODS if m["skill"] == skill and (level is None or m["lvl"] <= level)]
    res = [market.eval_method(m, b.by_name, b.mapping, b.latest, b.h1, b.d1, instant) for m in pool]
    unpriced = [r for r in res if r.missing]
    res = [r for r in res if not r.missing and not (items == "f2p" and r.members) and not (items == "p2p" and not r.members)]
    if sort == "gp":
        res = [r for r in res if r.xp > 0]
        res.sort(key=lambda r: r.per_xp, reverse=True)
    elif sort == "xp":
        res = [r for r in res if r.xp > 0]
        # Profitable methods first, most XP per hour first; loss-making ones after, cheapest XP first.
        res.sort(key=lambda r: (r.profit >= 0, r.xp_hour if r.profit >= 0 else r.per_xp), reverse=True)
    else:
        res.sort(key=lambda r: r.profit_hour, reverse=True)
    label = {"hour": "most gp per hour", "gp": "most gp per XP", "xp": "fastest XP that still profits"}[sort]
    scope = {"f2p": "F2P methods", "p2p": "Members methods", "all": "F2P and members methods"}[items]
    title = f"{skill}" + (f" at level {level}" if level else ", all levels")
    e = discord.Embed(title=title, color=COLOR, description=f"{scope}, sorted by {label}.")
    if not res:
        e.description = ("No priced methods for this level and filter."
                         + (" Fletching and Herblore are members skills: pick All or Members methods." if skill in ("Fletching", "Herblore") else ""))
    if res and sort == "hour" and res[0].profit_hour <= 0:
        e.description += " Nothing in this skill makes money right now; these lose the least."
    to_next = market.xp_for_level(level + 1) - market.xp_for_level(level) if level and level < 99 else 0
    for i, r in enumerate(res[:10]):
        extra = ""
        if i == 0 and to_next and r.xp > 0:
            acts = -(-to_next // r.xp)
            extra = f"\nOne full level ({n(to_next)} XP): {n(acts)} actions, {'earns' if r.profit >= 0 else 'costs'} {n(abs(r.profit * acts))}."
        cap = f", capped by {r.limited_by}" if r.limited_by else ""
        xp_part = f"{n(r.xp_hour)} XP/h, {r.per_xp:+.1f} gp/XP" if r.xp > 0 else "no XP"
        e.add_field(name=f"{r.method['name']} (lvl {r.method['lvl']}{', members' if r.members else ''})",
                    value=f"**{signed(r.profit_hour)}/h** ({signed(r.profit)} each, about {n(r.per_hour)}/h{cap}). {xp_part}.{extra}",
                    inline=False)
    foot = ("Per hour uses rough action rates with banking, capped by GE buy limits and a quarter of the hourly trade volume. "
            + ("Instant prices." if instant else "Patient prices: buy near the insta-sell price, sell near the insta-buy price, after tax."))
    if unpriced:
        foot += f" {len(unpriced)} method(s) skipped: no live price."
    e.set_footer(text=foot[:2048])
    await interaction.response.send_message(embed=e)


watch_group = app_commands.Group(name="watch", description="Price alerts")


@watch_group.command(name="add", description="Alert me when an item crosses a price")
@app_commands.describe(item="Item name", direction="below or above", price="Price, e.g. 595 or 1.2k",
                       after_tax="Compare the price after 2% tax", here="Post the alert in this channel instead of a DM")
@app_commands.choices(direction=[app_commands.Choice(name="below (instant buy price)", value="below"),
                                 app_commands.Choice(name="above (instant sell price)", value="above")])
@app_commands.autocomplete(item=item_autocomplete)
async def watch_add(interaction: discord.Interaction, item: str, direction: str, price: str,
                    after_tax: bool = False, here: bool = False) -> None:
    b = bot_of(interaction)
    iid, p = b.find_item(item), parse_gp(price)
    if iid is None or not p:
        await interaction.response.send_message("Check the item name and price.", ephemeral=True)
        return
    below = direction == "below"
    wid = await b.store.add_watch(interaction.user.id, interaction.channel_id if here else None, iid,
                                  b.mapping[iid]["name"], below, p, after_tax, below)
    await interaction.response.send_message(
        f"Watch #{wid}: {b.mapping[iid]['name']} {'below' if below else 'above'} {n(p)}{' after tax' if after_tax else ''}.",
        ephemeral=True)


@watch_group.command(name="list", description="Your price alerts")
async def watch_list(interaction: discord.Interaction) -> None:
    b = bot_of(interaction)
    rows = await b.store.watches(interaction.user.id)
    text = "\n".join(f"#{w['id']} {w['item_name']} {'<' if w['below'] else '>'} {n(w['price'])}{' net' if w['net'] else ''}"
                     f"{' (hit)' if w['fired'] else ''}" for w in rows) or "No watches yet. Use /watch add."
    await interaction.response.send_message(text, ephemeral=True)


@watch_group.command(name="remove", description="Delete a price alert")
async def watch_remove(interaction: discord.Interaction, watch_id: int) -> None:
    ok = await bot_of(interaction).store.remove_watch(interaction.user.id, watch_id)
    await interaction.response.send_message("Removed." if ok else "No watch with that number.", ephemeral=True)


ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("TRADINGCOW_ADMIN_IDS", "")) if x.strip().isdigit()}


def can_configure(interaction: discord.Interaction) -> bool:
    """TRADINGCOW_ADMIN_IDS decides when set; otherwise anyone who can manage channels."""
    if ADMIN_IDS:
        return interaction.user.id in ADMIN_IDS
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_channels or perms.administrator))


@app_commands.command(name="tc_channel", description="Choose where TradingCow posts each kind of message")
@app_commands.describe(kind="Which messages", channel="Where they should go")
@app_commands.choices(kind=[app_commands.Choice(name="Sudden dumps", value="dumps"),
                            app_commands.Choice(name="F2P flip board", value="f2p"),
                            app_commands.Choice(name="Members flip board", value="p2p"),
                            app_commands.Choice(name="Watch alerts (general)", value="general")])
async def channel_cmd(interaction: discord.Interaction, kind: str, channel: discord.TextChannel) -> None:
    if not can_configure(interaction):
        await interaction.response.send_message(
            "Only TradingCow admins can change this. Ask the owner to add your user ID to TRADINGCOW_ADMIN_IDS on Render.",
            ephemeral=True)
        return
    b = bot_of(interaction)
    await b.store.set(f"channel:{kind}", str(channel.id))
    if kind == "dumps":
        await b.store.set("alert_channel", "")
    names = {"dumps": "Sudden dumps", "f2p": "The F2P flip board", "p2p": "The members flip board", "general": "Watch alerts"}
    await interaction.response.send_message(f"{names[kind]} will now go to {channel.mention}.", ephemeral=True)


@app_commands.command(name="tc_channels", description="Show where TradingCow posts each kind of message")
async def channels_cmd(interaction: discord.Interaction) -> None:
    b = bot_of(interaction)
    lines = []
    for kind, label in (("dumps", "Sudden dumps"), ("f2p", "F2P flip board"), ("p2p", "Members flip board"), ("general", "Watch alerts")):
        ch = await b.channel(kind)
        lines.append(f"{label}: {ch.mention if ch else 'not set'}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@app_commands.command(name="tc_guide", description="Repost the explainer in every TradingCow channel now")
async def guide_cmd(interaction: discord.Interaction) -> None:
    if not can_configure(interaction):
        await interaction.response.send_message("Only TradingCow admins can do this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    done = await bot_of(interaction).post_guides()
    await interaction.followup.send(f"Reposted the explainer in {done} channel(s). It also reposts by itself every day at {GUIDE_HOUR}:00.",
                                    ephemeral=True)


@app_commands.command(name="tc_help", description="What TradingCow can do")
async def help_cmd(interaction: discord.Interaction) -> None:
    e = discord.Embed(title="TradingCow", color=COLOR, description=(
        "/price item: live prices, margin after tax, 7-day average\n"
        "/flips cash: best flips for your cash, slots and patience\n"
        "/alch: best items to high alch right now\n"
        "/dumps: items well under their weekly average\n"
        "/skill skill: top 10 most profitable methods right now (level optional)\n"
        "/watch add, list, remove: price alerts (posted in the general channel)\n"
        "/tc_channel, /tc_channels: where automatic posts go\n"
        "/tc_guide: repost the channel explainers now\n\n"
        "Prices come from the OSRS Wiki. The bot never touches your account."))
    await interaction.response.send_message(embed=e, ephemeral=True)


# ------------------------------------------------------------ entry point

async def run_tradingcow(default_db_dir: Path) -> None:
    token = os.getenv("TRADINGCOW_DISCORD_TOKEN", "").strip()
    if not token:
        log.info("TRADINGCOW_DISCORD_TOKEN not set, TradingCow stays off")
        return
    guilds = [int(x) for x in re.split(r"[,\s]+", os.getenv("TRADINGCOW_GUILD_IDS", "")) if x.strip().isdigit()]
    alert = os.getenv("TRADINGCOW_ALERT_CHANNEL_ID", "").strip()
    db_path = Path(os.getenv("TRADINGCOW_DB_PATH", "") or (default_db_dir / "tradingcow.sqlite"))
    drop = float(os.getenv("TRADINGCOW_DUMP_MIN_DROP", "10")) / 100
    bot = TradingCow(db_path, guilds, int(alert) if alert.isdigit() else None, drop)
    try:
        await bot.start(token)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never take the arcade bot down with us.
        log.exception("TradingCow stopped with an error")
    finally:
        if not bot.is_closed():
            await bot.close()

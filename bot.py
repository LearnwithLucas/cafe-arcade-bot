"""TradingCow Discord bot: GE prices, flips, weekly dump alerts, skill profit and price watches.

Runs next to the arcade bot in the same process, with its own token and database.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import market
from .store import Store
from .wiki import Wiki

log = logging.getLogger("tradingcow")

COLOR = 0xE0A53A
HOUR = 3600


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
        for cmd in (price_cmd, flips_cmd, dumps_cmd, skill_cmd, alerts_cmd, help_cmd):
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
        self.backfill_task = asyncio.create_task(self.backfill())

    async def close(self) -> None:
        for loop in (self.price_loop, self.hour_loop, self.dump_loop):
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
            channel = await self.alert_channel()
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

    @price_loop.before_loop
    @hour_loop.before_loop
    @dump_loop.before_loop
    async def _wait(self) -> None:
        await self.wait_until_ready()

    async def alert_channel(self):
        raw = await self.store.get("alert_channel")
        cid = int(raw) if raw else self.default_alert_channel
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
        if channel_id:
            ch = self.get_channel(channel_id)
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
                       f2p="F2P items only", risk="Highest risk to allow", min_profit="Minimum profit per slot",
                       min_margin="Minimum profit per item in gp (default 5)", min_roi="Minimum profit per item in % (default 1)")
@app_commands.choices(speed=[app_commands.Choice(name=x, value=v) for x, v in
                             [("Fast (under 1h)", "1"), ("Medium (up to 6h)", "6"), ("Overnight (up to 16h)", "16")]],
                      risk=[app_commands.Choice(name=x, value=x) for x in ("low", "medium", "high")])
async def flips_cmd(interaction: discord.Interaction, cash: str, slots: app_commands.Range[int, 1, 8] = 3,
                    speed: str = "6", f2p: bool = True, risk: str = "medium", min_profit: str = "5k",
                    min_margin: app_commands.Range[int, 1, 1_000_000] = 5,
                    min_roi: app_commands.Range[float, 0.0, 50.0] = 1.0) -> None:
    b = bot_of(interaction)
    c, mp = parse_gp(cash), parse_gp(min_profit)
    if c is None or mp is None:
        await interaction.response.send_message("Use amounts like 1500000, 1.5m or 800k.", ephemeral=True)
        return
    rows = market.flips(b.mapping, b.latest, b.h1, b.d1, cash=c, slots=slots, max_hours=float(speed),
                        min_profit=mp, f2p=f2p, max_risk=risk, min_margin=min_margin,
                        min_roi=min_roi / 100)[:10]
    e = discord.Embed(title=f"Flips for {n(c)} over {slots} slot(s)", color=COLOR)
    if not rows:
        e.description = "Nothing passes these filters. Try more time, less minimum profit or more risk."
    for f in rows:
        flags = f" ({', '.join(l for _, l in f.q.flags)})" if f.q.flags else ""
        e.add_field(name=f"{f.name}: {signed(f.profit)}",
                    value=f"Buy {n(f.qty)} at {n(f.q.p_buy)}, sell at {n(f.q.p_sell)} ({signed(f.q.net)} each). "
                          f"Fill about {hrs(f.hours)}. Risk {f.q.risk}{flags}.", inline=False)
    e.set_footer(text=f"At least {min_margin} gp and {min_roi:g}% profit per item. Fill times assume you catch a quarter of the hourly volume.")
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


@app_commands.command(name="skill", description="Profit per XP for a skill at your level")
@app_commands.describe(skill="Skill", level="Your level", f2p="F2P methods only", instant="Use instant prices instead of patient ones")
@app_commands.choices(skill=[app_commands.Choice(name=s, value=s) for s in market.SKILLS])
async def skill_cmd(interaction: discord.Interaction, skill: str, level: app_commands.Range[int, 1, 99],
                    f2p: bool = True, instant: bool = False) -> None:
    b = bot_of(interaction)
    res = [market.eval_method(m, b.by_name, b.mapping, b.latest, b.h1, b.d1, instant) for m in market.METHODS
           if m["skill"] == skill and m["lvl"] <= level]
    res = [r for r in res if not r.missing and r.xp > 0 and not (f2p and r.members)]
    res.sort(key=lambda r: r.per_xp, reverse=True)
    e = discord.Embed(title=f"{skill} at level {level}", color=COLOR)
    if not res:
        e.description = "No priced methods for this level and filter."
    to_next = market.xp_for_level(level + 1) - market.xp_for_level(level) if level < 99 else 0
    for i, r in enumerate(res[:8]):
        extra = ""
        if i == 0 and to_next:
            acts = -(-to_next // r.xp)
            extra = f"\nOne full level ({n(to_next)} XP): {n(acts)} actions, {'earns' if r.profit >= 0 else 'costs'} {n(abs(r.profit * acts))}."
        e.add_field(name=f"{r.method['name']} (lvl {r.method['lvl']})",
                    value=f"{signed(r.profit)} each, {r.xp:g} XP, **{r.per_xp:+.2f} gp/XP**{extra}", inline=False)
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


@app_commands.command(name="tc_alerts", description="Choose the channel for automatic dump alerts")
@app_commands.default_permissions(manage_guild=True)
async def alerts_cmd(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    await bot_of(interaction).store.set("alert_channel", str(channel.id))
    await interaction.response.send_message(f"Dump alerts will go to {channel.mention}.", ephemeral=True)


@app_commands.command(name="tc_help", description="What TradingCow can do")
async def help_cmd(interaction: discord.Interaction) -> None:
    e = discord.Embed(title="TradingCow", color=COLOR, description=(
        "/price item: live prices, margin after tax, 7-day average\n"
        "/flips cash: best flips for your cash, slots and patience\n"
        "/dumps: items well under their weekly average\n"
        "/skill skill level: profit per XP at your level\n"
        "/watch add, list, remove: price alerts by DM\n"
        "/tc_alerts channel: where automatic dump alerts go\n\n"
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

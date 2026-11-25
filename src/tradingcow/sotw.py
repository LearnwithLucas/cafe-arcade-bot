"""Skill of the Week for TradingCow, powered by Wise Old Man.

The bot reads a Wise Old Man competition (https://wiseoldman.net/competitions/<id>) and keeps one live
leaderboard message in the SOTW channel, refreshed every 10 minutes. Wise Old Man updates a player's XP when
they log out with the RuneLite Wise Old Man plugin, or when someone presses "Update me" (the bot asks WOM to update).

Admins pick the competition with /sotw_settings. No XP is stored here: Wise Old Man is the source of truth.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .quest_comp import admin_only

log = logging.getLogger("tradingcow.sotw")

API = "https://api.wiseoldman.net/v2"
SITE = "https://wiseoldman.net"
USER_AGENT = "TradingCow Discord bot (github.com/LearnwithLucas/cafe-arcade-bot)"
SOTW_CHANNEL_ID = int(os.getenv("SOTW_CHANNEL_ID", "1571254714836983848"))
DEFAULT_COMPETITION = int(os.getenv("SOTW_COMPETITION_ID", "161485"))
COLOR = 0x2E8B57
MEDALS = ["\U0001F947", "\U0001F948", "\U0001F949"]
SKILL_EMOJI = {
    "thieving": "\U0001F977", "agility": "\U0001F3C3", "woodcutting": "\U0001FA93", "fishing": "\U0001F3A3",
    "mining": "⛏️", "cooking": "\U0001F373", "firemaking": "\U0001F525", "farming": "\U0001F33E",
    "hunter": "\U0001FAA4", "slayer": "\U0001F480", "runecrafting": "\U0001F52E", "construction": "\U0001F528",
    "herblore": "\U0001F33F", "smithing": "\U0001F528", "crafting": "\U0001F9F5", "fletching": "\U0001F3F9",
    "prayer": "\U0001F64F", "magic": "✨", "sailing": "⛵",
}
CHART = "\U0001F4C8"


def fmt_xp(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    return f"{n:,}"


def ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


class WOM:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[int, tuple[float, dict]] = {}

    async def _s(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            headers = {"User-Agent": USER_AGENT}
            if os.getenv("WOM_API_KEY"):
                headers["x-api-key"] = os.environ["WOM_API_KEY"]
            self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30))
        return self.session

    async def competition(self, cid: int, max_age: float = 60) -> dict:
        hit = self.cache.get(cid)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        s = await self._s()
        async with s.get(f"{API}/competitions/{cid}") as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"Wise Old Man {r.status}: {data.get('message', data) if isinstance(data, dict) else data}")
        self.cache[cid] = (time.time(), data)
        return data

    async def update_player(self, username: str) -> tuple[bool, str]:
        s = await self._s()
        async with s.post(f"{API}/players/{username}") as r:
            data = await r.json(content_type=None)
            if r.status in (200, 201):
                return True, ""
            return False, data.get("message", f"error {r.status}") if isinstance(data, dict) else f"error {r.status}"

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()


def standings(comp: dict) -> list[dict]:
    rows = []
    for p in comp.get("participations", []):
        prog = p.get("progress") or {}
        lv = p.get("levels") or {}
        rows.append(dict(name=p["player"]["displayName"], username=p["player"]["username"],
                         gained=max(0, int(prog.get("gained") or 0)), start=int(prog.get("start") or 0),
                         end=int(prog.get("end") or 0), lv_start=lv.get("start"), lv_end=lv.get("end"),
                         updated=p["player"].get("updatedAt")))
    return sorted(rows, key=lambda r: (-r["gained"], r["name"].lower()))


def board_embed(comp: dict, limit: int = 10) -> discord.Embed:
    metric = comp.get("metric", "")
    start, end, now = ts(comp["startsAt"]), ts(comp["endsAt"]), time.time()
    if now < start:
        state = f"Starts <t:{start}:f> (<t:{start}:R>)"
    elif now < end:
        state = f"\U0001F7E2 Live · ends <t:{end}:f> (<t:{end}:R>)"
    else:
        state = f"\U0001F3C1 Finished <t:{end}:f>"
    rows = standings(comp)
    active = [r for r in rows if r["gained"] > 0]
    total = sum(r["gained"] for r in rows)
    e = discord.Embed(title=f"{SKILL_EMOJI.get(metric, CHART)} {comp['title']}", url=f"{SITE}/competitions/{comp['id']}",
                      color=COLOR, description=(
                          f"{state}\n**Skill**: {metric.title()} · **Players**: {len(active)} of {comp.get('participantCount', len(rows))} "
                          f"gained XP · **Clan total**: {fmt_xp(total)} XP"))
    lines = []
    for i, r in enumerate(active[:limit]):
        mark = MEDALS[i] if i < 3 else f"`{i + 1:>2}.`"
        lv = ""
        if r["lv_start"] is not None and r["lv_end"] is not None and r["lv_end"] > r["lv_start"]:
            lv = f" · lvl {r['lv_start']}→{r['lv_end']}"
        lines.append(f"{mark} **{r['name']}**: {fmt_xp(r['gained'])} XP{lv}")
    e.add_field(name="Leaderboard", value="\n".join(lines) or "Nobody has gained XP yet.", inline=False)
    e.add_field(name="How XP is tracked", value=(
        "Wise Old Man updates your XP when you log out with the RuneLite **Wise Old Man** plugin on, "
        "or press **Update me** below."), inline=False)
    e.set_footer(text=f"Data: Wise Old Man · refreshed {time.strftime('%H:%M', time.gmtime())} UTC")
    return e


class RsnModal(discord.ui.Modal):
    rsn = discord.ui.TextInput(label="Your RuneScape name", max_length=12)

    def __init__(self, action: str) -> None:
        super().__init__(title="Update my XP" if action == "update" else "Find my rank")
        self.action = action

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        name = str(self.rsn.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        cid = await bot.sotw_competition_id()
        if self.action == "update":
            ok, err = await bot.wom.update_player(name)
            if not ok:
                await interaction.followup.send(f"Wise Old Man couldn't update **{name}**: {err}", ephemeral=True)
                return
            bot.wom.cache.pop(cid, None)
        try:
            comp = await bot.wom.competition(cid, max_age=0 if self.action == "update" else 60)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach Wise Old Man: {e}", ephemeral=True)
            return
        rows = standings(comp)
        idx = next((i for i, r in enumerate(rows) if r["username"] == name.lower() or r["name"].lower() == name.lower()), None)
        if idx is None:
            await interaction.followup.send(
                f"**{name}** isn't in this competition. Ask an admin to add you on Wise Old Man.", ephemeral=True)
            return
        r = rows[idx]
        head = "Updated! " if self.action == "update" else ""
        await interaction.followup.send(
            f"{head}**{r['name']}**: {fmt_xp(r['gained'])} XP gained, rank **{idx + 1}** of {len(rows)} "
            f"({fmt_xp(r['end'])} XP now).", ephemeral=True)
        if self.action == "update":
            await bot.sotw_refresh()


class SotwView(discord.ui.View):
    def __init__(self, cid: int | None = None) -> None:
        super().__init__(timeout=None)
        if cid:
            self.add_item(discord.ui.Button(label="Open on Wise Old Man", url=f"{SITE}/competitions/{cid}"))

    @discord.ui.button(label="Update me", style=discord.ButtonStyle.success, custom_id="sotw:update")
    async def update(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(RsnModal("update"))

    @discord.ui.button(label="My rank", style=discord.ButtonStyle.primary, custom_id="sotw:rank")
    async def rank(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(RsnModal("rank"))

    @discord.ui.button(label="Full leaderboard", style=discord.ButtonStyle.secondary, custom_id="sotw:all")
    async def full(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        comp = await bot.wom.competition(await bot.sotw_competition_id())
        rows = [r for r in standings(comp) if r["gained"] > 0]
        lines = [f"`{i + 1:>3}.` {r['name']}: {fmt_xp(r['gained'])}" for i, r in enumerate(rows)]
        text = "\n".join(lines) or "Nobody has gained XP yet."
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n... see the full list on Wise Old Man."
        await interaction.followup.send(text, ephemeral=True)


class SettingsModal(discord.ui.Modal, title="Skill of the Week"):
    link = discord.ui.TextInput(label="Wise Old Man competition link or ID", placeholder="https://wiseoldman.net/competitions/161485")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        m = re.search(r"(\d+)\s*/?\s*$", str(self.link.value).strip())
        if not m:
            await interaction.response.send_message("I can't find a competition ID in that.", ephemeral=True)
            return
        cid = int(m.group(1))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            comp = await interaction.client.wom.competition(cid, max_age=0)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load competition {cid}: {e}", ephemeral=True)
            return
        await interaction.client.store.set("sotw:competition", str(cid))
        await interaction.client.sotw_refresh(repost=True)
        await interaction.followup.send(f"Now tracking **{comp['title']}** ({comp['metric']}, {comp['participantCount']} players).",
                                        ephemeral=True)


class SotwPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    @discord.ui.button(label="Change competition", style=discord.ButtonStyle.primary)
    async def change(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(SettingsModal())

    @discord.ui.button(label="Refresh now", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        interaction.client.wom.cache.clear()
        await interaction.client.sotw_refresh()
        await interaction.followup.send("Refreshed.", ephemeral=True)

    @discord.ui.button(label="Repost board", style=discord.ButtonStyle.secondary)
    async def repost(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.client.sotw_refresh(repost=True)
        await interaction.followup.send("Board reposted at the bottom of the channel.", ephemeral=True)


@app_commands.command(name="sotw_settings", description="Skill of the Week: pick the Wise Old Man competition (admins)")
async def sotw_settings_cmd(interaction: discord.Interaction) -> None:
    if not await admin_only(interaction):
        return
    cid = await interaction.client.sotw_competition_id()
    await interaction.response.send_message(
        f"**Skill of the Week**\nTracking: {SITE}/competitions/{cid}\nChannel: <#{SOTW_CHANNEL_ID}>\n"
        "The board refreshes every 10 minutes.", view=SotwPanel(), ephemeral=True)


class SotwMixin:
    """Mixed into TradingCow."""

    async def sotw_setup(self) -> None:
        self.wom = WOM()
        self.add_view(SotwView())
        self.tree.add_command(sotw_settings_cmd)
        self.sotw_loop.start()

    async def sotw_competition_id(self) -> int:
        raw = await self.store.get("sotw:competition")
        return int(raw) if raw else DEFAULT_COMPETITION

    async def sotw_refresh(self, repost: bool = False) -> None:
        try:
            cid = await self.sotw_competition_id()
            comp = await self.wom.competition(cid)
            ch = self.get_channel(SOTW_CHANNEL_ID) or await self.fetch_channel(SOTW_CHANNEL_ID)
            embed, view = board_embed(comp), SotwView(cid)
            key = f"sotw:msg:{ch.id}"
            old = await self.store.get(key)
            if old:
                try:
                    msg = await ch.fetch_message(int(old))
                    if repost:
                        await msg.delete()
                    else:
                        await msg.edit(embed=embed, view=view)
                        return
                except discord.HTTPException:
                    pass
            msg = await ch.send(embed=embed, view=view)
            await self.store.set(key, str(msg.id))
        except Exception:
            log.exception("Skill of the Week refresh failed")

    @tasks.loop(minutes=10)
    async def sotw_loop(self) -> None:
        await self.sotw_refresh()

    @sotw_loop.before_loop
    async def _sotw_wait(self) -> None:
        await self.wait_until_ready()

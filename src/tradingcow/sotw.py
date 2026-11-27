"""Skill of the Week for TradingCow, powered by Wise Old Man.

The bot reads a Wise Old Man competition (https://wiseoldman.net/competitions/<id>) and keeps one live
leaderboard message in the SOTW channel, refreshed every hour. Wise Old Man updates a player's XP when
they log out with the RuneLite Wise Old Man plugin, or when someone presses "Update me" (the bot asks WOM to update).

Admins pick the competition with /sotw_settings. No XP is stored here: Wise Old Man is the source of truth.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .quest_comp import AdminsView, admin_only, admins_text

log = logging.getLogger("tradingcow.sotw")

API = "https://api.wiseoldman.net/v2"
SITE = "https://wiseoldman.net"
USER_AGENT = "TradingCow Discord bot (github.com/LearnwithLucas/cafe-arcade-bot)"
SOTW_CHANNEL_ID = int(os.getenv("SOTW_CHANNEL_ID", "1560197183670853682"))  # the clan's skill-of-the-week channel
OLD_TEST_CHANNEL = 1571254714836983848
DEFAULT_COMPETITION = int(os.getenv("SOTW_COMPETITION_ID", "167657"))
COLOR = 0x2E8B57
TZ = ZoneInfo("Europe/Amsterdam")
DEFAULT_LABEL = os.getenv("SOTW_LABEL", "Thieving")
PRIZE_PLACES = int(os.getenv("SOTW_PRIZE_PLACES", "0"))  # 0 = as many places as there are prizes
DEFAULT_PRIZES = os.getenv("SOTW_PRIZES", "20M, 15M, 10M, 5M")
GROUP_ID = int(os.getenv("SOTW_GROUP_ID", "23796"))  # the clan's Wise Old Man group
# Shown until the competition exists on Wise Old Man (Amsterdam time).
EVENT_START = os.getenv("SOTW_START", "2026-11-27 23:00")
EVENT_END = os.getenv("SOTW_END", "2026-12-04 23:00")
DEFAULT_INTRO = os.getenv("SOTW_INTRO", "*Forget the grind, embrace the crime. It's Thieving week!* \U0001F4B0\U0001F440")
DEFAULT_EXTRA = os.getenv("SOTW_EXTRA", (
    "\U0001F3C5 **5M** to the first person to get the Thieving pet **Rocky** "
    "(screenshot of the clan chat announcement required)\n"
    "\U0001F389 The winner gets a special rank until the next SOTW or BOTW winner"))
DEFAULT_BANNER = os.getenv("SOTW_BANNER",
                           "https://learnwithlucas.com/wp-content/uploads/2026/11/Twilight-Thieving-Skill-Showcase.jpg")
MEDALS = ["\U0001F947", "\U0001F948", "\U0001F949", "\U0001F396\uFE0F"]
SPORT_MEDAL = "\U0001F3C5"
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

    async def group_competitions(self, group_id: int, max_age: float = 600) -> list[dict]:
        hit = self.cache.get(-group_id)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        s = await self._s()
        async with s.get(f"{API}/groups/{group_id}/competitions", params={"limit": 20}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"Wise Old Man {r.status}")
        self.cache[-group_id] = (time.time(), data)
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


def place_name(i: int) -> str:
    n = i + 1
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def local_ts(text: str) -> int:
    return int(datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def board_embeds(comp: dict | None, label: str, prizes: list[str], banner: str,
                 prev_ranks: dict[str, int] | None = None, intro: str = "", extra: str = "") -> list[discord.Embed]:
    """Same layout as the quest competition: a header card with rules, prizes and banner, then the leaderboard.
    comp is None while the competition isn't on Wise Old Man yet: the board then shows the announced dates."""
    if comp is None:
        comp = {"id": None, "startsAt": None, "endsAt": None, "participations": [], "participantCount": 0}
        start, end = local_ts(EVENT_START), local_ts(EVENT_END)
    else:
        start, end = ts(comp["startsAt"]), ts(comp["endsAt"])
    now = time.time()
    if now < start:
        state = f"Starts <t:{start}:f> (<t:{start}:R>). Ends <t:{end}:f>."
    elif now < end:
        state = f"Live now. Ends <t:{end}:f> (<t:{end}:R>)."
    else:
        state = f"Finished <t:{end}:f>. Final results."
    rows = standings(comp)
    active = [r for r in rows if r["gained"] > 0]
    total = sum(r["gained"] for r in rows)
    desc = (f"{intro}\n\n" if intro else "") + (f"{state}\n\n"
            f"Gain as much **{label} XP** as you can during the week. Most XP gained wins.\n"
            "Your XP updates when you log out with the RuneLite **Wise Old Man** plugin on, "
            "or press **Update me** below and type your name.\n\n")
    if prizes:
        desc += (f"\U0001F3C6 **{len(prizes)} winning places** (most XP by the end of the week)\n"
                 + "  ".join(f"{MEDALS[i] if i < len(MEDALS) else SPORT_MEDAL} {p}" for i, p in enumerate(prizes)) + "\n")
    if extra:
        desc += extra.strip() + "\n"
    if comp["id"]:
        desc += f"\n**{len(active)}** of {comp.get('participantCount', len(rows))} players gained XP \u00b7 clan total **{fmt_xp(total)} XP**"
    head = discord.Embed(title=f"Skill of the Week: {label}", url=f"{SITE}/competitions/{comp['id']}" if comp["id"] else None,
                         color=COLOR, description=desc[:4096])
    if banner:
        head.set_image(url=banner)
    head.set_footer(text=f"Updated {datetime.now(TZ).strftime('%d %b %H:%M')} Amsterdam time \u00b7 data from Wise Old Man")

    board = discord.Embed(title="Leaderboard", color=COLOR)
    places = max(PRIZE_PLACES or len(prizes), 1)
    cutoff = active[places - 1]["gained"] if len(active) >= places else 0
    prev = prev_ranks or {}
    lines: list[str] = []
    for i, r in enumerate(active[:25]):
        if i == places:
            lines.append(f"\u2500\u2500\u2500 \U0001F3C6 prize line: {place_name(places - 1)} has {fmt_xp(cutoff)} XP \u2500\u2500\u2500")
        mark = (MEDALS[i] if i < len(MEDALS) else SPORT_MEDAL) if i < places else f"`{i + 1:>2}.`"
        was = prev.get(r["username"])
        move = ""
        if was is not None and was != i + 1:
            move = f" \u2B06\uFE0F{was - i - 1}" if was > i + 1 else f" \u2B07\uFE0F{i + 1 - was}"
        elif was is None and prev:
            move = " \U0001F195"
        prize = f" \u00b7 {prizes[i]}" if i < len(prizes) else ""
        if i < places:
            lines.append(f"{mark} **{r['name']}**: **{fmt_xp(r['gained'])} XP**{prize}{move}")
            bits = [f"{r['gained']:,} XP"]
            if r["lv_start"] is not None and r["lv_end"] is not None and r["lv_end"] > r["lv_start"]:
                bits.append(f"level {r['lv_start']} \u2192 {r['lv_end']}")
            if i > 0:
                bits.append(f"{fmt_xp(active[i - 1]['gained'] - r['gained'])} behind {place_name(i - 1)}")
            if r.get("updated"):
                bits.append(f"updated <t:{ts(r['updated'])}:R>")
            lines.append("\u2003\u2514 " + " \u00b7 ".join(bits))
        else:
            gap = (f" \u00b7 {fmt_xp(cutoff - r['gained'])} to the prize line"
                   if cutoff > r["gained"] and i < places + 5 else "")  # the chasing pack only, keeps the list calm
            lines.append(f"{mark} {r['name']}: {fmt_xp(r['gained'])} XP{gap}{move}")
    if len(active) > 25:
        lines.append(f"and {len(active) - 25} more: press **Full leaderboard**")
    if prev:
        lines += ["", "*\u2B06\uFE0F\u2B07\uFE0F places moved since the last update \u00b7 \U0001F195 new on the board*"]
    if not comp["id"]:
        lines = ["The leaderboard appears here as soon as the competition is live on Wise Old Man.",
                 "Make sure the RuneLite **Wise Old Man** plugin is on, so your XP is tracked from the first minute."]
    board.description = "\n".join(lines)[:4096] or "Nobody has gained XP yet."
    return [head, board]


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
    def __init__(self, cid: int, label: str, prizes: str, banner: str, extra: str = "") -> None:
        super().__init__()
        self.link = discord.ui.TextInput(label="Wise Old Man competition link or ID", default=f"{SITE}/competitions/{cid}")
        self.label = discord.ui.TextInput(label="Skill name shown on the board", default=label, max_length=30)
        self.prizes = discord.ui.TextInput(label="Prizes for 1st, 2nd, 3rd (commas, optional)", default=prizes or None,
                                           required=False, max_length=100, placeholder="10M, 5M, 2.5M")
        self.banner = discord.ui.TextInput(label="Banner image link (optional)", default=banner or None, required=False,
                                           max_length=400)
        self.extra = discord.ui.TextInput(label="Bonus prizes and extra info (optional)", default=extra or None,
                                          required=False, max_length=600, style=discord.TextStyle.paragraph)
        for x in (self.link, self.label, self.prizes, self.banner, self.extra):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        m = re.search(r"(\d+)\s*/?\s*$", str(self.link.value).strip())
        if not m:
            await interaction.response.send_message("I can't find a competition ID in that link.", ephemeral=True)
            return
        cid = int(m.group(1))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            comp = await interaction.client.wom.competition(cid, max_age=0)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load competition {cid}: {e}", ephemeral=True)
            return
        store = interaction.client.store
        await store.set("sotw:competition", str(cid))
        await store.set("sotw:label", str(self.label.value).strip() or DEFAULT_LABEL)
        await store.set("sotw:prizes", ", ".join(p.strip() for p in str(self.prizes.value).split(",") if p.strip()))
        await store.set("sotw:banner", str(self.banner.value).strip())
        await store.set("sotw:extra", str(self.extra.value).strip())
        await interaction.client.sotw_refresh(repost=True)
        await interaction.followup.send(f"Now tracking **{comp['title']}** ({comp['metric']}, {comp['participantCount']} players).",
                                        ephemeral=True)


class SotwPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    @discord.ui.button(label="Competition, prizes and banner", style=discord.ButtonStyle.primary)
    async def change(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            bot = interaction.client
            await interaction.response.send_modal(SettingsModal(
                await bot.sotw_competition_id(), await bot.sotw_setting("label", DEFAULT_LABEL),
                (await bot.sotw_setting("prizes", "")) or DEFAULT_PRIZES, await bot.sotw_setting("banner", DEFAULT_BANNER),
                await bot.sotw_setting("extra", DEFAULT_EXTRA)))

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


    @discord.ui.button(label="Channel", style=discord.ButtonStyle.secondary, row=1)
    async def channel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message("Where should the Skill of the Week board go?",
                                                    view=ChannelView(), ephemeral=True)

    @discord.ui.button(label="Admins", style=discord.ButtonStyle.secondary, row=1)
    async def admins(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(admins_text(), view=AdminsView(), ephemeral=True)

    @discord.ui.button(label="Admin guide", style=discord.ButtonStyle.primary, row=1)
    async def guide(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(GUIDE, ephemeral=True)


GUIDE = (
    "**Skill of the Week: admin guide**\n"
    "1. Create the competition on wiseoldman.net (skill, dates, players).\n"
    "2. Here press **Competition, prizes and banner**: paste the link, set the skill name, prizes and banner.\n"
    "3. The board updates itself every hour. **Refresh now** pulls the latest XP straight away, "
    "**Repost board** moves it to the bottom of the channel.\n\n"
    "XP comes from Wise Old Man. It updates when players log out with the RuneLite Wise Old Man plugin on, "
    "or when they press **Update me** on the board. Players missing from the board aren't in the competition "
    "on Wise Old Man yet: add them there.")


class ChannelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.ChannelSelect(placeholder="Channel", channel_types=[discord.ChannelType.text])
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        ch = self.sel.values[0]
        await interaction.client.store.set("sotw:channel", str(ch.id))
        await interaction.response.edit_message(content=f"The board now goes to {ch.mention}.", view=None)
        await interaction.client.sotw_refresh()


async def panel_text(bot) -> str:
    prizes = (await bot.sotw_setting("prizes", "")) or DEFAULT_PRIZES
    label = await bot.sotw_setting("label", DEFAULT_LABEL)
    try:
        comp = await bot.sotw_pick()
        what = (f"**{comp['title']}** ({comp['metric']}, {comp['participantCount']} players)\n{SITE}/competitions/{comp['id']}"
                if comp else f"no {label} competition on Wise Old Man yet. The board shows the announced dates "
                             f"({EVENT_START} to {EVENT_END}) and switches over by itself once it's created in the clan group.")
    except Exception:
        what = "could not reach Wise Old Man right now"
    return (f"**Skill of the Week settings**\n"
            f"Competition: {what}\n"
            f"Skill name on the board: {label}\n"
            f"Prizes: {prizes}\n"
            f"Channel: <#{await bot.sotw_channel_id()}> \u00b7 refreshes every hour")


@app_commands.command(name="sotw_settings", description="Skill of the Week: competition, board and admins (admins)")
async def sotw_settings_cmd(interaction: discord.Interaction) -> None:
    if not await admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await panel_text(interaction.client), view=SotwPanel(), ephemeral=True)


class SotwMixin:
    """Mixed into TradingCow."""

    async def sotw_setup(self) -> None:
        self.wom = WOM()
        self.add_view(SotwView())
        self.tree.add_command(sotw_settings_cmd)
        self.sotw_loop.start()

    async def sotw_setting(self, key: str, default: str) -> str:
        raw = await self.store.get(f"sotw:{key}")
        return default if raw is None else raw

    async def sotw_channel_id(self) -> int:
        raw = await self.store.get("sotw:channel")
        if raw and int(raw) == OLD_TEST_CHANNEL:  # moved from testing to the real channel
            await self.store.set("sotw:channel", "")
            raw = ""
        return int(raw) if raw else SOTW_CHANNEL_ID

    async def sotw_competition_id(self) -> int:
        raw = await self.store.get("sotw:competition")
        return int(raw) if raw else DEFAULT_COMPETITION

    async def sotw_pick(self) -> dict | None:
        """The competition to show: the newest one in the clan's Wise Old Man group for this skill.
        Falls back to the one set in /sotw_settings. None means 'not on Wise Old Man yet'."""
        label = (await self.sotw_setting("label", DEFAULT_LABEL)).strip().lower()
        cid = await self.sotw_competition_id()
        try:
            group = [c for c in await self.wom.group_competitions(GROUP_ID) if c.get("metric") == label]
        except Exception:
            log.warning("Skill of the Week: couldn't read the Wise Old Man group, using the set competition")
            group = []
        if group:
            newest = max(group, key=lambda c: c["startsAt"])
            if newest["id"] != cid:
                await self.store.set("sotw:competition", str(newest["id"]))
                cid = newest["id"]
        comp = await self.wom.competition(cid)
        return comp if comp.get("metric") == label else None

    async def sotw_refresh(self, repost: bool = False) -> None:
        try:
            comp = await self.sotw_pick()
            cid = comp["id"] if comp else await self.sotw_competition_id()
            chid = await self.sotw_channel_id()
            ch = self.get_channel(chid) or await self.fetch_channel(chid)
            await self.sotw_clean_old_channels(chid)
            rank_key = f"sotw:ranks:{cid}"
            try:
                prev = json.loads(await self.store.get(rank_key) or "{}")
            except ValueError:
                prev = {}
            prizes = [p.strip() for p in ((await self.sotw_setting("prizes", "")) or DEFAULT_PRIZES).split(",") if p.strip()]
            embeds = board_embeds(comp, await self.sotw_setting("label", DEFAULT_LABEL), prizes,
                                  await self.sotw_setting("banner", DEFAULT_BANNER), prev,
                                  intro=DEFAULT_INTRO, extra=await self.sotw_setting("extra", DEFAULT_EXTRA))
            now_ranks = {r["username"]: i + 1 for i, r in enumerate(x for x in standings(comp) if x["gained"] > 0)} if comp else {}
            if now_ranks != prev:
                await self.store.set(rank_key, json.dumps(now_ranks))
            view = SotwView(cid if comp else None)
            key = f"sotw:msg:{ch.id}"
            old = await self.store.get(key)
            if old:
                try:
                    msg = await ch.fetch_message(int(old))
                    if repost:
                        await msg.delete()
                    else:
                        await msg.edit(embeds=embeds, view=view)
                        return
                except discord.HTTPException:
                    pass
            msg = await ch.send(embeds=embeds, view=view)
            await self.store.set(key, str(msg.id))
        except discord.Forbidden as e:
            log.warning("Skill of the Week: no permission in the channel (%s)", e)
            await self.sotw_tell_owner(
                "\u26A0\uFE0F I can't post the Skill of the Week board in "
                f"<#{await self.sotw_channel_id()}>. Give TradingCow **View Channel**, **Send Messages** and "
                "**Embed Links** there, then press Refresh now in /sotw_settings.")
        except Exception:
            log.exception("Skill of the Week refresh failed")

    async def sotw_clean_old_channels(self, current: int) -> None:
        """When the board moves channel, delete the board left behind in the old one."""
        last = await self.store.get("sotw:last_channel")
        if not last and await self.store.get(f"sotw:msg:{OLD_TEST_CHANNEL}"):
            last = str(OLD_TEST_CHANNEL)  # boards posted before this was tracked lived in the test channel
        if last and int(last) != current:
            old_id = await self.store.get(f"sotw:msg:{last}")
            if old_id:
                try:
                    old_ch = self.get_channel(int(last)) or await self.fetch_channel(int(last))
                    await (await old_ch.fetch_message(int(old_id))).delete()
                except discord.HTTPException:
                    pass
                await self.store.set(f"sotw:msg:{last}", "")
        if last != str(current):
            await self.store.set("sotw:last_channel", str(current))

    async def sotw_tell_owner(self, text: str) -> None:
        if await self.store.get("sotw:warned") == text:
            return  # warn once, not every hour
        try:
            app = await self.application_info()
            owner = app.team.owner if app.team and app.team.owner else app.owner
            await owner.send(text)
            await self.store.set("sotw:warned", text)
        except Exception:
            pass

    @tasks.loop(hours=1)
    async def sotw_loop(self) -> None:
        await self.sotw_refresh()

    @sotw_loop.before_loop
    async def _sotw_wait(self) -> None:
        await self.wait_until_ready()

"""Quest competition for TradingCow.

Players report their Quest Points (QP) with /qp or the button on the daily prompt.
The bot keeps one live leaderboard message per competition channel and edits it after every update.

Groups:
  Low and Medium: most QP gained between the start and the end.
  High: first players to reach the Quest Cape (max QP) during the competition.

Test mode (QC_TEST_MODE=1, the default) lets only the testers use anything.
Turn it off on Render with QC_TEST_MODE=0 when the real competition goes live.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger("tradingcow.qc")

TZ = ZoneInfo("Europe/Amsterdam")
COLOR = 0x4FA3D9
GROUPS = ("low", "medium", "high")
GROUP_NAMES = {"low": "Low group", "medium": "Medium group", "high": "High group"}
PRIZES = {
    "low": ("5M", "2.5M"),
    "medium": ("10M", "5M"),
    "high": ("15M", "10M"),
}
ORGANISER = os.getenv("QC_ORGANISER", "@Killi0naire8")


def _ids(raw: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", raw) if x.strip().isdigit()}


def _names(raw: str) -> set[str]:
    return {x.strip().lower().lstrip("@") for x in re.split(r"[,\s]+", raw) if x.strip()}


TEST_MODE = os.getenv("QC_TEST_MODE", "1").strip() not in ("0", "false", "no", "off")
TESTER_IDS = _ids(os.getenv("QC_TESTER_IDS", ""))
TESTER_NAMES = _names(os.getenv("QC_TESTER_NAMES", "lucasnotlucas"))
QC_ADMIN_IDS = _ids(os.getenv("QC_ADMIN_IDS", ""))

DEFAULT_CHANNEL = int(os.getenv("QC_CHANNEL_ID", "1552970635247095939"))
DEFAULT_START = os.getenv("QC_START", "2026-11-07 22:00")
DEFAULT_END = os.getenv("QC_END", "2026-11-14 22:00")
DEFAULT_MAX_QP = int(os.getenv("QC_MAX_QP", "346"))
DEFAULT_LOW_MAX = int(os.getenv("QC_LOW_MAX", "150"))      # 0..150 = low
DEFAULT_MEDIUM_MAX = int(os.getenv("QC_MEDIUM_MAX", "280"))  # 151..280 = medium, above = high
DEFAULT_ASK_HOUR = int(os.getenv("QC_ASK_HOUR", "20"))     # Amsterdam time
LAST_CALL_HOURS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_players (
    rsn TEXT PRIMARY KEY COLLATE NOCASE,
    user_id INTEGER,
    grp TEXT NOT NULL,
    start_qp INTEGER,
    added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS qc_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rsn TEXT NOT NULL COLLATE NOCASE,
    qp INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    by_user INTEGER,
    proof TEXT
);
CREATE INDEX IF NOT EXISTS idx_qc_entries_rsn ON qc_entries(rsn, ts);
"""


# ------------------------------------------------------------ time helpers

def parse_local(text: str) -> int | None:
    """'2026-11-07 22:00' in Amsterdam time to a unix timestamp."""
    for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ).timestamp())
        except ValueError:
            continue
    return None


def fmt_local(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d %B %Y %H:%M")


# ------------------------------------------------------------ scoring (pure, no Discord)

@dataclass
class Standing:
    rsn: str
    grp: str
    user_id: int | None
    start: int | None
    current: int | None
    updated: int | None
    gain: int = 0
    reached_at: int | None = None   # when the current value was first reported (tie break)
    cape_at: int | None = None      # high group: when max QP was first reported in the window
    entries: int = 0
    notes: list[str] = field(default_factory=list)


def standing(player: dict, entries: list[tuple[int, int]], start_ts: int, end_ts: int, max_qp: int) -> Standing:
    """entries: (ts, qp) sorted by ts. Entries after the end do not count."""
    counted = [(t, q) for t, q in entries if t <= end_ts]
    before = [(t, q) for t, q in counted if t <= start_ts]
    during = [(t, q) for t, q in counted if t > start_ts]

    if player.get("start_qp") is not None:
        start = player["start_qp"]
    elif before:
        start = before[-1][1]
    elif during:
        start = during[0][1]  # first report came after the start: count from there
    else:
        start = None

    current = counted[-1][1] if counted else start
    updated = counted[-1][0] if counted else None
    s = Standing(player["rsn"], player["grp"], player.get("user_id"), start, current, updated, entries=len(counted))
    if start is not None and current is not None:
        s.gain = max(0, current - start)
        s.reached_at = next((t for t, q in during if q == current), updated)
        if start < max_qp:
            s.cape_at = next((t for t, q in during if q >= max_qp), None)
        else:
            s.notes.append("already had the cape")
    return s


def rank(group: str, rows: list[Standing]) -> list[Standing]:
    if group == "high":
        capped = sorted((r for r in rows if r.cape_at), key=lambda r: r.cape_at)
        rest = sorted((r for r in rows if not r.cape_at),
                      key=lambda r: (-(r.current or 0), r.reached_at or 1 << 62, r.rsn.lower()))
        return capped + rest
    return sorted(rows, key=lambda r: (-r.gain, r.reached_at or 1 << 62, r.rsn.lower()))


def auto_group(qp: int, low_max: int, medium_max: int) -> str:
    if qp <= low_max:
        return "low"
    return "medium" if qp <= medium_max else "high"


# ------------------------------------------------------------ storage

class QCStore:
    def __init__(self, store) -> None:
        self.s = store  # TradingCow Store (shares its connection and kv table)

    @property
    def conn(self):
        return self.s.conn

    async def setup(self) -> None:
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        PANEL_ADMINS.clear()
        PANEL_ADMINS.update(_ids(await self.setting("admins", "")))

    async def set_admins(self, ids: set[int]) -> None:
        await self.put("admins", ",".join(str(i) for i in sorted(ids)))
        PANEL_ADMINS.clear()
        PANEL_ADMINS.update(ids)

    async def setting(self, key: str, default):
        raw = await self.s.get(f"qc:{key}")
        if raw in (None, ""):
            return default
        return type(default)(raw) if not isinstance(default, str) else raw

    async def put(self, key: str, value) -> None:
        await self.s.set(f"qc:{key}", str(value))

    async def window(self) -> tuple[int, int]:
        start = int(await self.setting("start", 0)) or parse_local(DEFAULT_START)
        end = int(await self.setting("end", 0)) or parse_local(DEFAULT_END)
        return start, end

    async def max_qp(self) -> int:
        return int(await self.setting("max_qp", DEFAULT_MAX_QP))

    # players
    async def upsert_player(self, rsn: str, grp: str, user_id: int | None, start_qp: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO qc_players(rsn, user_id, grp, start_qp, added) VALUES (?,?,?,?,?) "
            "ON CONFLICT(rsn) DO UPDATE SET grp=excluded.grp, "
            "user_id=COALESCE(excluded.user_id, qc_players.user_id), "
            "start_qp=COALESCE(excluded.start_qp, qc_players.start_qp)",
            (rsn, user_id, grp, start_qp, int(time.time())))
        await self.conn.commit()

    async def set_start(self, rsn: str, start_qp: int | None) -> None:
        await self.conn.execute("UPDATE qc_players SET start_qp=? WHERE rsn=?", (start_qp, rsn))
        await self.conn.commit()

    async def remove_player(self, rsn: str) -> bool:
        cur = await self.conn.execute("DELETE FROM qc_players WHERE rsn=?", (rsn,))
        await self.conn.execute("DELETE FROM qc_entries WHERE rsn=?", (rsn,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def players(self) -> list[dict]:
        async with self.conn.execute("SELECT rsn, user_id, grp, start_qp FROM qc_players ORDER BY rsn") as cur:
            return [dict(rsn=r[0], user_id=r[1], grp=r[2], start_qp=r[3]) for r in await cur.fetchall()]

    async def player(self, rsn: str) -> dict | None:
        async with self.conn.execute("SELECT rsn, user_id, grp, start_qp FROM qc_players WHERE rsn=?", (rsn,)) as cur:
            r = await cur.fetchone()
        return dict(rsn=r[0], user_id=r[1], grp=r[2], start_qp=r[3]) if r else None

    async def players_of(self, user_id: int) -> list[dict]:
        return [p for p in await self.players() if p["user_id"] == user_id]

    # entries
    async def add_entry(self, rsn: str, qp: int, by_user: int | None, proof: str | None = None, ts: int | None = None) -> None:
        await self.conn.execute("INSERT INTO qc_entries(rsn, qp, ts, by_user, proof) VALUES (?,?,?,?,?)",
                                (rsn, qp, ts or int(time.time()), by_user, proof))
        await self.conn.commit()

    async def entries(self, rsn: str | None = None) -> dict[str, list[tuple[int, int]]]:
        sql = "SELECT rsn, ts, qp FROM qc_entries" + (" WHERE rsn=?" if rsn else "") + " ORDER BY ts, id"
        out: dict[str, list[tuple[int, int]]] = {}
        async with self.conn.execute(sql, (rsn,) if rsn else ()) as cur:
            async for r in cur:
                out.setdefault(r[0].lower(), []).append((r[1], r[2]))
        return out

    async def history(self, rsn: str) -> list[tuple]:
        async with self.conn.execute("SELECT ts, qp, by_user, proof FROM qc_entries WHERE rsn=? ORDER BY ts DESC, id DESC LIMIT 15",
                                     (rsn,)) as cur:
            return await cur.fetchall()

    async def last_qp(self, rsn: str) -> int | None:
        async with self.conn.execute("SELECT qp FROM qc_entries WHERE rsn=? ORDER BY ts DESC, id DESC LIMIT 1", (rsn,)) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def reset(self, everything: bool) -> None:
        await self.conn.execute("DELETE FROM qc_entries")
        if everything:
            await self.conn.execute("DELETE FROM qc_players")
            await self.conn.execute("DELETE FROM kv WHERE key LIKE 'qc:%' AND key NOT LIKE 'qc:msg:%'")
        else:
            await self.conn.execute("UPDATE qc_players SET start_qp=NULL")
            for k in ("final_posted", "last_call", "ask_last"):
                await self.conn.execute("DELETE FROM kv WHERE key=?", (f"qc:{k}",))
        await self.conn.commit()

    async def standings(self) -> tuple[dict[str, list[Standing]], int, int, int]:
        start, end = await self.window()
        mx = await self.max_qp()
        ent = await self.entries()
        groups: dict[str, list[Standing]] = {g: [] for g in GROUPS}
        for p in await self.players():
            s = standing(p, ent.get(p["rsn"].lower(), []), start, end, mx)
            groups.setdefault(p["grp"], []).append(s)
        return {g: rank(g, rows) for g, rows in groups.items()}, start, end, mx


# ------------------------------------------------------------ access

def is_tester(user: discord.abc.User) -> bool:
    return user.id in TESTER_IDS or (user.name or "").lower() in TESTER_NAMES


PANEL_ADMINS: set[int] = set()  # admins added with /qp_settings, loaded from the database at start


def is_admin(interaction: discord.Interaction) -> bool:
    """Testers, QC_ADMIN_IDS and admins added in the panel. Works in test mode too, so admins can prepare."""
    uid = interaction.user.id
    if is_tester(interaction.user) or uid in QC_ADMIN_IDS or uid in PANEL_ADMINS:
        return True
    if TEST_MODE or QC_ADMIN_IDS or PANEL_ADMINS:
        return False
    from .bot import can_configure  # late import, bot.py imports this module
    return can_configure(interaction)  # nobody set yet: anyone who can manage channels


def can_play(user: discord.abc.User) -> bool:
    return is_tester(user) or user.id in PANEL_ADMINS or not TEST_MODE


TEST_MSG = "The quest competition is still being tested. It opens soon."


# ------------------------------------------------------------ messages

def medal(i: int) -> str:
    return ("1st", "2nd")[i]


def board_embeds(groups: dict[str, list[Standing]], start: int, end: int, mx: int, final: bool = False) -> list[discord.Embed]:
    now = int(time.time())
    if final or now > end:
        state = f"Finished <t:{end}:f>. Final results."
    elif now < start:
        state = f"Starts <t:{start}:f> (<t:{start}:R>). Ends <t:{end}:f>."
    else:
        state = f"Live now. Ends <t:{end}:f> (<t:{end}:R>)."
    head = discord.Embed(
        title="Quest Competition" + (" (test)" if TEST_MODE else ""), color=COLOR,
        description=(f"{state}\n\nLow and Medium: most Quest Points gained wins. "
                     f"High: the first two to reach the Quest Cape ({mx} QP) win.\n"
                     f"Press **Update my QP** below or type /qp. "
                     f"Not on the list? Ask {ORGANISER}."))
    head.set_footer(text=f"Updated {datetime.fromtimestamp(now, TZ).strftime('%d %b %H:%M')} Amsterdam time")
    out = [head]
    for g in GROUPS:
        rows = groups.get(g, [])
        p1, p2 = PRIZES[g]
        e = discord.Embed(title=f"{GROUP_NAMES[g]}  (1st {p1}, 2nd {p2})", color=COLOR)
        if not rows:
            e.description = "No players yet."
            out.append(e)
            continue
        lines = []
        for i, r in enumerate(rows[:25]):
            pos = f"**{i + 1}.**"
            if r.current is None:
                lines.append(f"{pos} {r.rsn}: no QP reported yet")
                continue
            when = f" (<t:{r.updated}:R>)" if r.updated else ""
            if g == "high":
                if r.cape_at:
                    lines.append(f"{pos} {r.rsn}: **Quest Cape** <t:{r.cape_at}:f>")
                else:
                    left = max(0, mx - r.current)
                    extra = ", already had it" if "already had the cape" in r.notes else ""
                    lines.append(f"{pos} {r.rsn}: {r.current} QP, {left} to go{extra}{when}")
            else:
                lines.append(f"{pos} {r.rsn}: **+{r.gain}** ({r.start} to {r.current}){when}")
        if len(rows) > 25:
            lines.append(f"and {len(rows) - 25} more")
        e.description = "\n".join(lines)
        out.append(e)
    return out


def winners_text(groups: dict[str, list[Standing]]) -> str:
    lines = []
    for g in GROUPS:
        rows = groups.get(g, [])
        if g == "high":
            top = [r for r in rows if r.cape_at][:2]
        else:
            top = [r for r in rows if r.gain > 0][:2]
        names = []
        for i, r in enumerate(top):
            who = f"<@{r.user_id}>" if r.user_id else r.rsn
            what = "Quest Cape" if g == "high" else f"+{r.gain} QP"
            names.append(f"{medal(i)} {who} ({r.rsn}, {what}, {PRIZES[g][i]})")
        lines.append(f"**{GROUP_NAMES[g]}**: " + ("; ".join(names) if names else "no winner"))
    return "\n".join(lines)


# ------------------------------------------------------------ submitting

async def submit(bot, interaction: discord.Interaction, qp: int, rsn: str | None, proof: str | None) -> str:
    """Record a QP update. Returns the reply text."""
    qc: QCStore = bot.qc
    if not can_play(interaction.user):
        return TEST_MSG
    start, end = await qc.window()
    now = int(time.time())
    if now > end:
        return f"The competition ended {fmt_local(end)}. Your QP can't change any more. Ask {ORGANISER} if something is wrong."
    mine = await qc.players_of(interaction.user.id)
    if rsn:
        p = await qc.player(rsn)
        if not p or (p["user_id"] != interaction.user.id and not is_admin(interaction)):
            names = ", ".join(x["rsn"] for x in mine)
            return (f"{rsn} isn't linked to your Discord account." + (f" Yours: {names}." if names else "")
                    + f" Ask {ORGANISER} to link it.")
    elif len(mine) == 1:
        p = mine[0]
    elif mine:
        return "You have more than one account in the competition. Use /qp and fill in rsn: " + ", ".join(x["rsn"] for x in mine)
    else:
        return f"You aren't on the participant list yet. Ask {ORGANISER} to add you."
    mx = await qc.max_qp()
    if not 0 <= qp <= mx:
        return f"Quest Points go from 0 to {mx}."
    last = await qc.last_qp(p["rsn"])
    if last is not None and qp < last:
        return f"Quest Points can't go down. Your last update for {p['rsn']} was {last} QP. If that was a typo, ask {ORGANISER} to fix it."
    await qc.add_entry(p["rsn"], qp, interaction.user.id, proof)
    bot.qc_refresh_soon()
    if now < start:
        return f"Saved: {p['rsn']} has {qp} QP. This counts as your starting point. The competition starts {fmt_local(start)}."
    gained = ""
    for s in (await qc.standings())[0].get(p["grp"], []):
        if s.rsn.lower() == p["rsn"].lower():
            gained = (" Quest Cape reached, nice!" if s.cape_at and qp >= mx and p["grp"] == "high"
                      else f" That's +{s.gain} since the start.")
    return f"Saved: {p['rsn']} has {qp} QP.{gained}"


class QPModal(discord.ui.Modal, title="Update your Quest Points"):
    qp = discord.ui.TextInput(label="Your Quest Points right now", placeholder="e.g. 187", max_length=4)
    rsn = discord.ui.TextInput(label="Account (only if you have more than one)", required=False, max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.qp.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("Type the number only, e.g. 187.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await submit(interaction.client, interaction, int(raw), str(self.rsn.value).strip() or None, None)
        await interaction.followup.send(text, ephemeral=True)


async def player_status(qc: QCStore, user_id: int) -> str:
    """What a player sees: their accounts, start, current, gain and place."""
    mine = await qc.players_of(user_id)
    if not mine:
        return f"You aren't on the participant list yet. Ask {ORGANISER} to add you."
    groups, start, end, mx = await qc.standings()
    lines = []
    for p in mine:
        rows = groups.get(p["grp"], [])
        place = next((i + 1 for i, r in enumerate(rows) if r.rsn.lower() == p["rsn"].lower()), None)
        r = rows[place - 1] if place else None
        if not r or r.current is None:
            lines.append(f"**{p['rsn']}** ({GROUP_NAMES[p['grp']].lower()}): no QP reported yet.")
            continue
        score = ("Quest Cape reached" if r.cape_at else f"{max(0, mx - r.current)} QP to the cape") if p["grp"] == "high" \
            else f"+{r.gain} since the start"
        lines.append(f"**{p['rsn']}** ({GROUP_NAMES[p['grp']].lower()}): {r.current} QP, {score}, place {place} of {len(rows)}.")
    now = int(time.time())
    when = f"Starts <t:{start}:R>." if now < start else (f"Ends <t:{end}:R>." if now < end else "The competition has ended.")
    return "\n".join(lines) + f"\n{when}"


async def history_text(qc: QCStore, p: dict) -> str:
    rows = await qc.history(p["rsn"])
    lines = []
    for ts, qp, _by, proof in rows:
        src = {"import": " (imported)", "admin": " (set by admin)"}.get(proof or "", "")
        link = f" [screenshot]({proof})" if proof and proof.startswith("http") else ""
        lines.append(f"<t:{ts}:f>: {qp} QP{src}{link}")
    return "\n".join(lines) or "No updates yet."


class QPView(discord.ui.View):
    """Persistent buttons under the board and the daily reminder (they keep working after restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Update my QP", style=discord.ButtonStyle.primary, custom_id="qc:update")
    async def update(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not can_play(interaction.user):
            await interaction.response.send_message(TEST_MSG, ephemeral=True)
            return
        await interaction.response.send_modal(QPModal())

    @discord.ui.button(label="My progress", style=discord.ButtonStyle.secondary, custom_id="qc:mine")
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not can_play(interaction.user):
            await interaction.response.send_message(TEST_MSG, ephemeral=True)
            return
        qc: QCStore = interaction.client.qc
        text = await player_status(qc, interaction.user.id)
        for p in await qc.players_of(interaction.user.id):
            text += f"\n\n**{p['rsn']} updates**\n" + await history_text(qc, p)
        await interaction.response.send_message(text[:2000], ephemeral=True)


# ------------------------------------------------------------ player command

@app_commands.command(name="qp", description="Quest competition: update your Quest Points or see your progress")
@app_commands.describe(qp="Your Quest Points right now (leave empty to see your progress)",
                       proof="Optional screenshot of your quest list", rsn="Only if you have more than one account")
async def qp_cmd(interaction: discord.Interaction, qp: app_commands.Range[int, 0, 1000] | None = None,
                 proof: discord.Attachment | None = None, rsn: str | None = None) -> None:
    if not can_play(interaction.user):
        await interaction.response.send_message(TEST_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if qp is None:
        text = await player_status(interaction.client.qc, interaction.user.id)
    else:
        text = await submit(interaction.client, interaction, qp, rsn, proof.url if proof else None)
    await interaction.followup.send(text, view=QPView(), ephemeral=True)


# ------------------------------------------------------------ admin panel

async def admin_only(interaction: discord.Interaction) -> bool:
    """True when the user may use the panel; otherwise replies and returns False."""
    if is_admin(interaction):
        return True
    await interaction.response.send_message(TEST_MSG if TEST_MODE else "Only competition admins can do this.", ephemeral=True)
    return False


async def panel_text(qc: QCStore, bot) -> str:
    start, end = await qc.window()
    ch = await bot.qc_channel()
    players = await qc.players()
    counts = {g: sum(1 for p in players if p["grp"] == g) for g in GROUPS}
    linked = sum(1 for p in players if p["user_id"])
    return (f"**Quest competition settings**{' (test mode)' if TEST_MODE else ''}\n"
            f"Start {fmt_local(start)}, end {fmt_local(end)} (Amsterdam time).\n"
            f"Channel: {ch.mention if ch else 'not found'}. Quest Cape at {await qc.max_qp()} QP.\n"
            f"Players: {counts['low']} low, {counts['medium']} medium, {counts['high']} high "
            f"({linked} linked to Discord).\n"
            f"Admins: {', '.join(f'<@{u}>' for u in sorted(PANEL_ADMINS)) or 'only the testers'}"
            f"{' plus anyone who can manage channels' if not PANEL_ADMINS and not QC_ADMIN_IDS and not TEST_MODE else ''}.\n"
            f"Groups by starting QP: low up to {await qc.setting('low_max', DEFAULT_LOW_MAX)}, "
            f"medium up to {await qc.setting('medium_max', DEFAULT_MEDIUM_MAX)}, high above. "
            f"Daily reminder at {await qc.setting('ask_hour', DEFAULT_ASK_HOUR)}:00.")


class SettingsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    @discord.ui.button(label="Add player", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(AddModal())

    @discord.ui.button(label="Import list", style=discord.ButtonStyle.success, row=0)
    async def imp(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(ImportModal())

    @discord.ui.button(label="Edit player", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        players = await interaction.client.qc.players()
        if not players:
            await interaction.response.send_message("No players yet. Use Add player or Import list.", ephemeral=True)
        elif len(players) <= 25:
            await interaction.response.send_message("Pick a player:", view=PickView(players), ephemeral=True)
        else:
            await interaction.response.send_modal(FindModal())

    @discord.ui.button(label="Dates and limits", style=discord.ButtonStyle.secondary, row=1)
    async def dates(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        qc: QCStore = interaction.client.qc
        s, e = await qc.window()
        await interaction.response.send_modal(DatesModal(
            datetime.fromtimestamp(s, TZ).strftime("%Y-%m-%d %H:%M"), datetime.fromtimestamp(e, TZ).strftime("%Y-%m-%d %H:%M"),
            await qc.max_qp(), f"{await qc.setting('low_max', DEFAULT_LOW_MAX)}/{await qc.setting('medium_max', DEFAULT_MEDIUM_MAX)}",
            await qc.setting("ask_hour", DEFAULT_ASK_HOUR)))

    @discord.ui.button(label="Channel", style=discord.ButtonStyle.secondary, row=1)
    async def channel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message("Where should the board and reminders go?", view=ChannelView(), ephemeral=True)

    @discord.ui.button(label="Admins", style=discord.ButtonStyle.secondary, row=1)
    async def admins(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(admins_text(), view=AdminsView(), ephemeral=True)

    @discord.ui.button(label="Post reminder now", style=discord.ButtonStyle.secondary, row=2)
    async def ask(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.client.qc_ask()
        await interaction.followup.send("Reminder posted.", ephemeral=True)

    @discord.ui.button(label="Repost board", style=discord.ButtonStyle.secondary, row=2)
    async def board(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.client.qc_refresh_board(repost=True)
        await interaction.followup.send("Board reposted at the bottom of the channel.", ephemeral=True)

    @discord.ui.button(label="Post results", style=discord.ButtonStyle.secondary, row=2)
    async def final(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message("Post the final results in the channel now?",
                                                    view=ConfirmView("final"), ephemeral=True)

    @discord.ui.button(label="Reset test data", style=discord.ButtonStyle.danger, row=3)
    async def reset(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(
                "**QP updates only** keeps the player list. **Everything** also removes players, dates and channel. "
                "This can't be undone.", view=ResetView(), ephemeral=True)


@app_commands.command(name="qp_settings", description="Quest competition: settings and players (admins)")
async def qp_settings_cmd(interaction: discord.Interaction) -> None:
    if not await admin_only(interaction):
        return
    await interaction.response.send_message(await panel_text(interaction.client.qc, interaction.client),
                                            view=SettingsView(), ephemeral=True)


# ---- add, import, find

def read_group(text: str) -> str | None:
    t = text.strip().lower()
    return {"l": "low", "low": "low", "m": "medium", "med": "medium", "medium": "medium",
            "h": "high", "high": "high"}.get(t) if t else ""


class AddModal(discord.ui.Modal, title="Add a player"):
    rsn = discord.ui.TextInput(label="RuneScape name", max_length=12)
    qp = discord.ui.TextInput(label="Quest Points now (optional)", required=False, max_length=4, placeholder="e.g. 139")
    group = discord.ui.TextInput(label="Group: low, medium or high (optional)", required=False, max_length=6,
                                 placeholder="Empty: picked from their QP")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        rsn, raw_qp = str(self.rsn.value).strip(), str(self.qp.value).strip()
        grp = read_group(str(self.group.value))
        if grp is None:
            await interaction.response.send_message("Group must be low, medium or high.", ephemeral=True)
            return
        if raw_qp and not raw_qp.isdigit():
            await interaction.response.send_message("Quest Points must be a number.", ephemeral=True)
            return
        existing = await qc.player(rsn)
        if not grp:
            if existing:
                grp = existing["grp"]
            elif raw_qp:
                grp = auto_group(int(raw_qp), int(await qc.setting("low_max", DEFAULT_LOW_MAX)),
                                 int(await qc.setting("medium_max", DEFAULT_MEDIUM_MAX)))
            else:
                await interaction.response.send_message("Fill in their QP or a group, so I know where they go.", ephemeral=True)
                return
        await qc.upsert_player(rsn, grp, None, None)
        if raw_qp:
            await qc.add_entry(rsn, int(raw_qp), interaction.user.id, "admin")
        interaction.client.qc_refresh_soon()
        p = await qc.player(rsn)
        await interaction.response.send_message(
            f"{'Updated' if existing else 'Added'} **{p['rsn']}** in the {GROUP_NAMES[grp].lower()}"
            + (f" with {raw_qp} QP" if raw_qp else "") + ".\nPick their Discord account so they can update their own QP:",
            view=PlayerView(p["rsn"]), ephemeral=True)


class ImportModal(discord.ui.Modal, title="Import players"):
    text = discord.ui.TextInput(
        label="One per line: name, QP, group (group optional)", style=discord.TextStyle.paragraph,
        placeholder="Wez, 313\nKodageist, 114/346, low\nBeanTown Dad, 1, medium", max_length=4000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        qc: QCStore = interaction.client.qc
        low_max = int(await qc.setting("low_max", DEFAULT_LOW_MAX))
        med_max = int(await qc.setting("medium_max", DEFAULT_MEDIUM_MAX))
        done, bad = [], []
        for line in str(self.text.value).splitlines():
            if not line.strip():
                continue
            m = re.fullmatch(r"\s*(.+?)\s*[,;\t|]\s*(\d+)(?:\s*/\s*\d+)?\s*(?:[,;\t|]\s*(low|medium|med|high))?\s*", line, re.I)
            if not m:
                bad.append(line.strip())
                continue
            rsn, qp, grp = m.group(1), int(m.group(2)), read_group(m.group(3) or "")
            await qc.upsert_player(rsn, grp or auto_group(qp, low_max, med_max), None, None)
            last = await qc.last_qp(rsn)
            if last is None or qp > last:  # importing the same list twice changes nothing
                await qc.add_entry(rsn, qp, interaction.user.id, "import")
            done.append(rsn)
        interaction.client.qc_refresh_soon()
        msg = f"Imported {len(done)} player(s): {', '.join(done)}." if done else "Nothing imported."
        if bad:
            msg += "\nCouldn't read: " + "; ".join(bad[:10]) + ". Use name, QP per line."
        if done:
            msg += "\nUse Edit player to link their Discord accounts or fix a group."
        await interaction.followup.send(msg[:2000], ephemeral=True)


class FindModal(discord.ui.Modal, title="Edit a player"):
    rsn = discord.ui.TextInput(label="RuneScape name", max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        p = await interaction.client.qc.player(str(self.rsn.value).strip())
        if not p:
            await interaction.response.send_message("No player with that name.", ephemeral=True)
            return
        await interaction.response.send_message(await player_card(interaction.client.qc, p["rsn"]),
                                                view=PlayerView(p["rsn"]), ephemeral=True)


class PickView(discord.ui.View):
    def __init__(self, players: list[dict]) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.Select(placeholder="Player", options=[
            discord.SelectOption(label=p["rsn"], value=p["rsn"], description=GROUP_NAMES[p["grp"]]) for p in players[:25]])
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        rsn = self.sel.values[0]
        await interaction.response.edit_message(content=await player_card(interaction.client.qc, rsn), view=PlayerView(rsn))


# ---- one player

async def player_card(qc: QCStore, rsn: str) -> str:
    p = await qc.player(rsn)
    if not p:
        return f"{rsn} is no longer in the competition."
    groups, start, end, mx = await qc.standings()
    r = next((x for x in groups.get(p["grp"], []) if x.rsn.lower() == p["rsn"].lower()), None)
    who = f"<@{p['user_id']}>" if p["user_id"] else "not linked to Discord"
    if r and r.current is not None:
        nums = f"Start {r.start}{' (fixed)' if p['start_qp'] is not None else ''}, now {r.current}, gained {r.gain}."
    else:
        nums = "No QP yet."
    return (f"**{p['rsn']}**: {GROUP_NAMES[p['grp']].lower()}, {who}.\n{nums}\n\n**Updates**\n"
            + await history_text(qc, p))[:2000]


class QPEditModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Quest Points", max_length=4)

    def __init__(self, rsn: str, which: str) -> None:
        super().__init__(title=f"{'Starting' if which == 'start' else 'Current'} QP for {rsn}"[:45])
        self.rsn, self.which = rsn, which

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.value.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("Type a number.", ephemeral=True)
            return
        qc: QCStore = interaction.client.qc
        if self.which == "start":
            await qc.set_start(self.rsn, int(raw))
        else:
            await qc.add_entry(self.rsn, int(raw), interaction.user.id, "admin")
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(content=await player_card(qc, self.rsn), view=PlayerView(self.rsn))


class PlayerView(discord.ui.View):
    def __init__(self, rsn: str) -> None:
        super().__init__(timeout=900)
        self.rsn = rsn
        link = discord.ui.UserSelect(placeholder="Link their Discord account", row=0)
        link.callback = self.link
        self.link_select = link
        self.add_item(link)
        grp = discord.ui.Select(placeholder="Move to another group", row=1, options=[
            discord.SelectOption(label=GROUP_NAMES[g], value=g) for g in GROUPS])
        grp.callback = self.move
        self.grp_select = grp
        self.add_item(grp)

    async def link(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        qc: QCStore = interaction.client.qc
        user = self.link_select.values[0]
        await qc.conn.execute("UPDATE qc_players SET user_id=? WHERE rsn=?", (user.id, self.rsn))
        await qc.conn.commit()
        await interaction.response.edit_message(content=await player_card(qc, self.rsn), view=PlayerView(self.rsn))

    async def move(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        qc: QCStore = interaction.client.qc
        await qc.conn.execute("UPDATE qc_players SET grp=? WHERE rsn=?", (self.grp_select.values[0], self.rsn))
        await qc.conn.commit()
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(content=await player_card(qc, self.rsn), view=PlayerView(self.rsn))

    @discord.ui.button(label="Set current QP", style=discord.ButtonStyle.primary, row=2)
    async def cur(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(QPEditModal(self.rsn, "current"))

    @discord.ui.button(label="Set starting QP", style=discord.ButtonStyle.secondary, row=2)
    async def start(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(QPEditModal(self.rsn, "start"))

    @discord.ui.button(label="Remove player", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.edit_message(content=f"Remove **{self.rsn}** and all their updates?",
                                                    view=ConfirmView("remove", self.rsn))


# ---- settings pieces

class DatesModal(discord.ui.Modal, title="Dates and limits"):
    def __init__(self, start: str, end: str, max_qp: int, limits: str, ask_hour) -> None:
        super().__init__()
        self.start = discord.ui.TextInput(label="Start (Amsterdam time)", default=start, max_length=16)
        self.end = discord.ui.TextInput(label="End (Amsterdam time)", default=end, max_length=16)
        self.max_qp = discord.ui.TextInput(label="QP needed for the Quest Cape", default=str(max_qp), max_length=4)
        self.limits = discord.ui.TextInput(label="Top QP of low / medium group", default=limits, max_length=9)
        self.ask_hour = discord.ui.TextInput(label="Daily reminder hour (0 to 23)", default=str(ask_hour), max_length=2)
        for x in (self.start, self.end, self.max_qp, self.limits, self.ask_hour):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        s, e = parse_local(str(self.start.value)), parse_local(str(self.end.value))
        lim = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(self.limits.value))
        mx, hour = str(self.max_qp.value).strip(), str(self.ask_hour.value).strip()
        problems = []
        if not s or not e:
            problems.append("dates must look like 2026-11-07 22:00")
        elif e <= s:
            problems.append("the end must be after the start")
        if not lim or int(lim.group(1)) >= int(lim.group(2)):
            problems.append("limits must look like 150/280")
        if not mx.isdigit():
            problems.append("max QP must be a number")
        if not hour.isdigit() or not 0 <= int(hour) <= 23:
            problems.append("the reminder hour must be 0 to 23")
        if problems:
            await interaction.response.send_message("Not saved: " + "; ".join(problems) + ".", ephemeral=True)
            return
        old_s, old_e = await qc.window()
        if (s, e) != (old_s, old_e):
            for k in ("final_posted", "last_call"):
                await qc.put(k, "")
        for k, v in (("start", s), ("end", e), ("max_qp", int(mx)), ("low_max", int(lim.group(1))),
                     ("medium_max", int(lim.group(2))), ("ask_hour", int(hour))):
            await qc.put(k, v)
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(content=await panel_text(qc, interaction.client), view=SettingsView())


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
        await interaction.client.qc.put("channel", ch.id)
        await interaction.response.defer(ephemeral=True)
        interaction.client.qc_refresh_soon()
        await interaction.edit_original_response(content=f"The board and reminders now go to {ch.mention}.", view=None)


class ResetView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def _do(self, interaction: discord.Interaction, everything: bool) -> None:
        if not await admin_only(interaction):
            return
        await interaction.client.qc.reset(everything)
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(
            content="Wiped." + (" Dates and channel are back to the defaults." if everything else ""), view=None)

    @discord.ui.button(label="QP updates only", style=discord.ButtonStyle.danger)
    async def entries(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, False)

    @discord.ui.button(label="Everything", style=discord.ButtonStyle.danger)
    async def everything(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing changed.", view=None)


class ConfirmView(discord.ui.View):
    def __init__(self, action: str, rsn: str | None = None) -> None:
        super().__init__(timeout=300)
        self.action, self.rsn = action, rsn

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        if self.action == "remove":
            await interaction.client.qc.remove_player(self.rsn)
            interaction.client.qc_refresh_soon()
            await interaction.response.edit_message(content=f"Removed {self.rsn}.", view=None)
        else:
            await interaction.response.edit_message(content="Posting results...", view=None)
            await interaction.client.qc_final()
            await interaction.edit_original_response(content="Results posted.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self.action == "remove":
            await interaction.response.edit_message(content=await player_card(interaction.client.qc, self.rsn),
                                                    view=PlayerView(self.rsn))
        else:
            await interaction.response.edit_message(content="Nothing changed.", view=None)


# ------------------------------------------------------------ bot mixin

class QuestCompMixin:
    """Mixed into TradingCow. Needs self.store, self.get_channel and self.fetch_channel."""

    async def qc_setup(self) -> None:
        self.qc = QCStore(self.store)
        await self.qc.setup()
        self.add_view(QPView())
        self.tree.add_command(qp_cmd)
        self.tree.add_command(qp_settings_cmd)
        self.qc_loop.start()

    def qc_refresh_soon(self) -> None:
        """Update the board in the background, so buttons answer Discord within its 3 seconds."""
        self._qc_dirty = True
        task = getattr(self, "_qc_task", None)
        if task and not task.done():
            return
        self._qc_task = asyncio.create_task(self._qc_refresh_worker())

    async def _qc_refresh_worker(self) -> None:
        while getattr(self, "_qc_dirty", False):
            self._qc_dirty = False
            try:
                await self.qc_refresh_board()
            except Exception:
                log.exception("Quest competition board refresh failed")

    async def qc_channel(self):
        cid = int(await self.qc.setting("channel", DEFAULT_CHANNEL))
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    async def qc_refresh_board(self, repost: bool = False, final: bool = False) -> None:
        ch = await self.qc_channel()
        if not ch:
            return
        groups, start, end, mx = await self.qc.standings()
        embeds = board_embeds(groups, start, end, mx, final)
        key = f"qc:msg:board:{ch.id}"
        msg_id = await self.store.get(key)
        msg = None
        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))
            except discord.HTTPException:
                msg = None
        if msg and repost:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            msg = None
        if msg:
            await msg.edit(embeds=embeds, view=QPView())
        else:
            msg = await ch.send(embeds=embeds, view=QPView())
            await self.store.set(key, str(msg.id))

    async def qc_ask(self, last_call: bool = False) -> None:
        """Post the reminder with the button. Pings linked players who haven't updated in 20 hours."""
        ch = await self.qc_channel()
        if not ch:
            return
        start, end = await self.qc.window()
        now = int(time.time())
        ent = await self.qc.entries()
        stale = sorted({p["user_id"] for p in await self.qc.players()
                        if p["user_id"] and (not ent.get(p["rsn"].lower()) or now - ent[p["rsn"].lower()][-1][0] > 20 * 3600)})
        if TEST_MODE:
            stale = [u for u in stale if u in TESTER_IDS]  # never ping real players while testing
        if last_call:
            text = f"**Last call.** The Quest Competition ends <t:{end}:R>. Update your QP now so your final score counts."
        elif now < start:
            text = f"The Quest Competition starts <t:{start}:R>. Report your QP now so we know your starting point."
        else:
            text = "Daily check-in: how many Quest Points do you have now? Press the button below."
        if stale:
            text += "\n" + " ".join(f"<@{u}>" for u in stale)
        old = await self.store.get(f"qc:msg:ask:{ch.id}")
        if old:
            try:
                await (await ch.fetch_message(int(old))).delete()
            except discord.HTTPException:
                pass
        msg = await ch.send(text, view=QPView(), allowed_mentions=discord.AllowedMentions(users=True))
        await self.store.set(f"qc:msg:ask:{ch.id}", str(msg.id))

    async def qc_final(self) -> None:
        ch = await self.qc_channel()
        if not ch:
            return
        await self.qc_refresh_board(final=True)
        groups, *_ = await self.qc.standings()
        await ch.send("**Quest Competition results**\n" + winners_text(groups)
                      + f"\n\nCongratulations! {ORGANISER} will send the prizes.",
                      allowed_mentions=discord.AllowedMentions(users=True))
        await self.qc.put("final_posted", "1")

    @tasks.loop(minutes=10)
    async def qc_loop(self) -> None:
        try:
            start, end = await self.qc.window()
            now = int(time.time())
            if now > end + 7 * 86400:
                return  # long over, stay quiet
            await self.qc_refresh_board()
            local = datetime.now(TZ)
            ask_hour = int(await self.qc.setting("ask_hour", DEFAULT_ASK_HOUR))
            last = int(await self.qc.setting("ask_last", 0))
            in_window = start - 2 * 86400 <= now <= end - LAST_CALL_HOURS * 3600
            if in_window and local.hour == ask_hour and now - last > 20 * 3600:
                await self.qc_ask()
                await self.qc.put("ask_last", now)
            if end - LAST_CALL_HOURS * 3600 <= now < end and not await self.qc.setting("last_call", ""):
                await self.qc_ask(last_call=True)
                await self.qc.put("last_call", "1")
            if now >= end and not await self.qc.setting("final_posted", ""):
                await self.qc_final()
        except Exception:
            log.exception("Quest competition loop failed")

    @qc_loop.before_loop
    async def _qc_wait(self) -> None:
        await self.wait_until_ready()




def admins_text() -> str:
    names = ", ".join(f"<@{u}>" for u in sorted(PANEL_ADMINS)) or "none yet"
    return (f"**Admins**: {names}.\nAdmins can open /qp_settings. To also play, add them as a player and link their Discord "
            "account, like anyone else.\nPick people below to make them admin. Pick someone who is already admin to remove them.")


class AdminsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.UserSelect(placeholder="Add or remove admins", min_values=1, max_values=10)
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        ids = set(PANEL_ADMINS)
        for u in self.sel.values:
            if u.bot:
                continue
            ids.symmetric_difference_update({u.id})
        if interaction.user.id not in ids and not is_tester(interaction.user) and interaction.user.id not in QC_ADMIN_IDS:
            ids.add(interaction.user.id)  # don't let someone lock themselves out
        await interaction.client.qc.set_admins(ids)
        await interaction.response.edit_message(content=admins_text(), view=AdminsView())

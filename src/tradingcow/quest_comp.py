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


def is_admin(interaction: discord.Interaction) -> bool:
    if is_tester(interaction.user):
        return True
    if TEST_MODE:
        return False
    if interaction.user.id in QC_ADMIN_IDS:
        return True
    from .bot import can_configure  # late import, bot.py imports this module
    return can_configure(interaction)


def can_play(user: discord.abc.User) -> bool:
    return is_tester(user) or not TEST_MODE


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
                     f"Update your QP with **/qp** or the button on the daily reminder. "
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
    try:
        await bot.qc_refresh_board()
    except Exception:
        log.exception("Board refresh after a QP update failed")
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


class QPView(discord.ui.View):
    """Persistent button under the daily reminder (works after restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Update my QP", style=discord.ButtonStyle.primary, custom_id="qc:update")
    async def update(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not can_play(interaction.user):
            await interaction.response.send_message(TEST_MSG, ephemeral=True)
            return
        await interaction.response.send_modal(QPModal())


# ------------------------------------------------------------ bot mixin

class QuestCompMixin:
    """Mixed into TradingCow. Needs self.store, self.get_channel and self.fetch_channel."""

    async def qc_setup(self) -> None:
        self.qc = QCStore(self.store)
        await self.qc.setup()
        self.add_view(QPView())
        self.tree.add_command(qp_cmd)
        self.tree.add_command(qc_group)
        self.qc_loop.start()

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
            await msg.edit(embeds=embeds)
        else:
            msg = await ch.send(embeds=embeds)
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
            text = "Daily check-in: how many Quest Points do you have now? Press the button or use /qp."
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


# ------------------------------------------------------------ commands

async def rsn_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    qc: QCStore = interaction.client.qc
    players = await qc.players()
    if not is_admin(interaction):
        players = [p for p in players if p["user_id"] == interaction.user.id]
    cur = current.lower()
    return [app_commands.Choice(name=p["rsn"], value=p["rsn"]) for p in players if cur in p["rsn"].lower()][:25]


@app_commands.command(name="qp", description="Update your Quest Points for the quest competition")
@app_commands.describe(qp="Your Quest Points right now", rsn="Which account (only if you have more than one)",
                       proof="Optional screenshot of your quest list")
@app_commands.autocomplete(rsn=rsn_autocomplete)
async def qp_cmd(interaction: discord.Interaction, qp: app_commands.Range[int, 0, 1000], rsn: str | None = None,
                 proof: discord.Attachment | None = None) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    text = await submit(interaction.client, interaction, qp, rsn, proof.url if proof else None)
    await interaction.followup.send(text, ephemeral=True)


qc_group = app_commands.Group(name="qc", description="Quest competition")


async def _deny(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return False
    await interaction.response.send_message(
        TEST_MSG if TEST_MODE else "Only competition admins can do this.", ephemeral=True)
    return True


@qc_group.command(name="add", description="Add or update a participant (admin)")
@app_commands.describe(rsn="RuneScape name", member="Their Discord account, so they can update their own QP",
                       group="Leave empty to pick by starting QP", start_qp="Starting QP (empty: their first report)")
@app_commands.choices(group=[app_commands.Choice(name=GROUP_NAMES[g], value=g) for g in GROUPS])
async def qc_add(interaction: discord.Interaction, rsn: str, member: discord.Member | None = None,
                 group: str | None = None, start_qp: app_commands.Range[int, 0, 1000] | None = None) -> None:
    if await _deny(interaction):
        return
    qc: QCStore = interaction.client.qc
    rsn = rsn.strip()
    existing = await qc.player(rsn)
    if group is None:
        if existing:
            group = existing["grp"]
        elif start_qp is not None:
            group = auto_group(start_qp, int(await qc.setting("low_max", DEFAULT_LOW_MAX)),
                               int(await qc.setting("medium_max", DEFAULT_MEDIUM_MAX)))
        else:
            await interaction.response.send_message("Pick a group, or give a start_qp so I can pick one.", ephemeral=True)
            return
    await qc.upsert_player(rsn, group, member.id if member else None, start_qp)
    await interaction.client.qc_refresh_board()
    who = f", linked to {member.mention}" if member else ""
    await interaction.response.send_message(
        f"{'Updated' if existing else 'Added'} {rsn} in the {GROUP_NAMES[group].lower()}{who}"
        + (f", starting at {start_qp} QP" if start_qp is not None else "") + ".", ephemeral=True)


@qc_group.command(name="import", description="Add many participants at once (admin)")
async def qc_import(interaction: discord.Interaction) -> None:
    if await _deny(interaction):
        return
    await interaction.response.send_modal(ImportModal())


class ImportModal(discord.ui.Modal, title="Import participants"):
    text = discord.ui.TextInput(
        label="One per line: name, QP, group (group optional)", style=discord.TextStyle.paragraph,
        placeholder="Wez, 313\nKodageist, 114/346, low\nBeanTown Dad, 1, medium", max_length=4000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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
            rsn, qp, grp = m.group(1), int(m.group(2)), (m.group(3) or "").lower()
            grp = "medium" if grp == "med" else grp or auto_group(qp, low_max, med_max)
            await qc.upsert_player(rsn, grp, None, None)
            await qc.add_entry(rsn, qp, interaction.user.id, "import")
            done.append(f"{rsn} ({qp}, {grp})")
        await interaction.client.qc_refresh_board()
        msg = f"Imported {len(done)}: " + ", ".join(done) if done else "Nothing imported."
        if bad:
            msg += "\nCouldn't read: " + "; ".join(bad[:10])
        msg += "\nLink Discord accounts with /qc add rsn member so players can update themselves."
        await interaction.response.send_message(msg[:2000], ephemeral=True)


@qc_group.command(name="remove", description="Remove a participant and their updates (admin)")
@app_commands.autocomplete(rsn=rsn_autocomplete)
async def qc_remove(interaction: discord.Interaction, rsn: str) -> None:
    if await _deny(interaction):
        return
    ok = await interaction.client.qc.remove_player(rsn)
    await interaction.client.qc_refresh_board()
    await interaction.response.send_message(f"Removed {rsn}." if ok else f"No participant called {rsn}.", ephemeral=True)


@qc_group.command(name="set", description="Correct someone's QP (admin)")
@app_commands.describe(rsn="RuneScape name", qp="The right QP", which="Current QP (default) or starting QP")
@app_commands.choices(which=[app_commands.Choice(name="Current QP", value="current"),
                             app_commands.Choice(name="Starting QP", value="start")])
@app_commands.autocomplete(rsn=rsn_autocomplete)
async def qc_set(interaction: discord.Interaction, rsn: str, qp: app_commands.Range[int, 0, 1000], which: str = "current") -> None:
    if await _deny(interaction):
        return
    qc: QCStore = interaction.client.qc
    p = await qc.player(rsn)
    if not p:
        await interaction.response.send_message(f"No participant called {rsn}.", ephemeral=True)
        return
    if which == "start":
        await qc.set_start(p["rsn"], qp)
    else:
        await qc.add_entry(p["rsn"], qp, interaction.user.id, "admin")
    await interaction.client.qc_refresh_board()
    await interaction.response.send_message(f"{p['rsn']}: {'starting' if which == 'start' else 'current'} QP set to {qp}.",
                                            ephemeral=True)


@qc_group.command(name="history", description="Someone's QP updates")
@app_commands.autocomplete(rsn=rsn_autocomplete)
async def qc_history(interaction: discord.Interaction, rsn: str) -> None:
    if not can_play(interaction.user):
        await interaction.response.send_message(TEST_MSG, ephemeral=True)
        return
    qc: QCStore = interaction.client.qc
    p = await qc.player(rsn)
    if not p:
        await interaction.response.send_message(f"No participant called {rsn}.", ephemeral=True)
        return
    rows = await qc.history(p["rsn"])
    lines = []
    for ts, qp, by_user, proof in rows:
        src = {"import": " (imported)", "admin": " (set by admin)"}.get(proof or "", "")
        link = f" [screenshot]({proof})" if proof and proof.startswith("http") else ""
        lines.append(f"<t:{ts}:f>: {qp} QP{src}{link}")
    head = f"**{p['rsn']}**, {GROUP_NAMES[p['grp']].lower()}" + (f", start fixed at {p['start_qp']}" if p["start_qp"] is not None else "")
    await interaction.response.send_message(head + "\n" + ("\n".join(lines) or "No updates yet."), ephemeral=True)


@qc_group.command(name="board", description="Repost the leaderboard at the bottom of the channel (admin)")
async def qc_board(interaction: discord.Interaction) -> None:
    if await _deny(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.client.qc_refresh_board(repost=True)
    await interaction.followup.send("Leaderboard reposted.", ephemeral=True)


@qc_group.command(name="ask", description="Post the QP reminder with the button now (admin)")
async def qc_ask(interaction: discord.Interaction) -> None:
    if await _deny(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.client.qc_ask()
    await interaction.followup.send("Reminder posted.", ephemeral=True)


@qc_group.command(name="final", description="Post the final results now (admin)")
async def qc_final(interaction: discord.Interaction) -> None:
    if await _deny(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.client.qc_final()
    await interaction.followup.send("Results posted.", ephemeral=True)


@qc_group.command(name="settings", description="Dates, channel, max QP and group limits (admin)")
@app_commands.describe(start="Amsterdam time, e.g. 2026-11-07 22:00", end="Amsterdam time, e.g. 2026-11-14 22:00",
                       channel="Where the board and reminders go", max_qp="QP needed for the Quest Cape",
                       low_max="Highest starting QP in the low group", medium_max="Highest starting QP in the medium group",
                       ask_hour="Hour of the daily reminder (Amsterdam time)")
async def qc_settings(interaction: discord.Interaction, start: str | None = None, end: str | None = None,
                      channel: discord.TextChannel | None = None, max_qp: app_commands.Range[int, 1, 1000] | None = None,
                      low_max: int | None = None, medium_max: int | None = None,
                      ask_hour: app_commands.Range[int, 0, 23] | None = None) -> None:
    if await _deny(interaction):
        return
    qc: QCStore = interaction.client.qc
    for name, raw in (("start", start), ("end", end)):
        if raw:
            ts = parse_local(raw)
            if not ts:
                await interaction.response.send_message(f"I can't read {raw}. Use 2026-11-07 22:00.", ephemeral=True)
                return
            await qc.put(name, ts)
            await qc.put("final_posted", "")
            await qc.put("last_call", "")
    for name, val in (("max_qp", max_qp), ("low_max", low_max), ("medium_max", medium_max), ("ask_hour", ask_hour)):
        if val is not None:
            await qc.put(name, val)
    if channel:
        await qc.put("channel", channel.id)
    s, e = await qc.window()
    ch = await interaction.client.qc_channel()
    await interaction.client.qc_refresh_board()
    await interaction.response.send_message(
        f"Start {fmt_local(s)}, end {fmt_local(e)} (Amsterdam).\n"
        f"Channel {ch.mention if ch else 'not found'}. Quest Cape at {await qc.max_qp()} QP.\n"
        f"Groups by starting QP: low up to {await qc.setting('low_max', DEFAULT_LOW_MAX)}, "
        f"medium up to {await qc.setting('medium_max', DEFAULT_MEDIUM_MAX)}, high above.\n"
        f"Daily reminder at {await qc.setting('ask_hour', DEFAULT_ASK_HOUR)}:00. "
        f"Test mode is {'on' if TEST_MODE else 'off'}.", ephemeral=True)


@qc_group.command(name="reset", description="Wipe test data (admin)")
@app_commands.describe(what="Updates only keeps the participant list; everything also removes players and settings")
@app_commands.choices(what=[app_commands.Choice(name="QP updates only", value="entries"),
                            app_commands.Choice(name="Everything", value="all")])
async def qc_reset(interaction: discord.Interaction, what: str, confirm: bool = False) -> None:
    if await _deny(interaction):
        return
    if not confirm:
        await interaction.response.send_message("Run it again with confirm: True. This can't be undone.", ephemeral=True)
        return
    await interaction.client.qc.reset(what == "all")
    await interaction.client.qc_refresh_board()
    await interaction.response.send_message("Wiped." + (" Dates and channel are back to the defaults." if what == "all" else ""),
                                            ephemeral=True)

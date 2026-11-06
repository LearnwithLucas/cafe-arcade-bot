"""Quest competition for TradingCow: one leaderboard, points per finished quest.

Players log every quest they finish during the competition with the "I finished a quest" button or /quest.
Each quest is worth points by its official difficulty (see quest_list.py):
Novice 1, Intermediate 2, Experienced 4, Master 7, Grandmaster 12. Most points wins.

The bot keeps one live leaderboard message, posts a daily reminder, a last call and the results.
Admins run everything from /qp_settings.

Test mode (QC_TEST_MODE=1, the default) lets only testers and panel admins use anything.
Turn it off on Render with QC_TEST_MODE=0 when the competition goes live.
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

from .quest_list import POINTS, QUESTS

log = logging.getLogger("tradingcow.qc")

TZ = ZoneInfo("Europe/Amsterdam")
COLOR = 0x4FA3D9
ORGANISER = os.getenv("QC_ORGANISER", "@Killi0naire8")
TIERS = ("Novice", "Intermediate", "Experienced", "Master", "Grandmaster")
QUEST_INFO = {name.lower(): (name, diff, POINTS[diff], qp) for name, diff, qp in QUESTS}  # name, tier, points, QP


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
DEFAULT_ASK_HOUR = int(os.getenv("QC_ASK_HOUR", "20"))  # Amsterdam time
DEFAULT_PRIZES = os.getenv("QC_PRIZES", "17.5M, 12.5M, 8.5M, 5.5M, 3.5M")
DEFAULT_BANNER = os.getenv("QC_BANNER", "https://learnwithlucas.com/wp-content/uploads/2026/11/Golden-Knight-Quest-Competition.jpg")
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
CREATE TABLE IF NOT EXISTS qc_done (
    rsn TEXT NOT NULL COLLATE NOCASE,
    quest TEXT NOT NULL COLLATE NOCASE,
    points INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    by_user INTEGER,
    proof TEXT,
    PRIMARY KEY (rsn, quest)
);
"""

# Short names people type in the search.
ALIASES = {
    "ds1": "Dragon Slayer I", "ds2": "Dragon Slayer II", "mm1": "Monkey Madness I", "mm2": "Monkey Madness II",
    "dt1": "Desert Treasure I", "dt2": "Desert Treasure II - The Fallen Empire", "sote": "Song of the Elves",
    "wgs": "While Guthix Sleeps", "sotf": "Sins of the Father", "mep1": "Mourning's End Part I",
    "mep2": "Mourning's End Part II", "fremmy trials": "The Fremennik Trials", "akd": "A Kingdom Divided",
    "rfd": "RFD: Another Cook's Quest",
}


# ------------------------------------------------------------ helpers

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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", "and")).strip()


def search_quests(term: str, exclude: set[str] = frozenset(), limit: int = 25) -> list[str]:
    """Quest names matching what someone typed: exact, then starts with, then a word starts with, then contains."""
    t = _norm(term)
    pool = [q[0] for q in QUESTS if q[0].lower() not in exclude]
    if not t:
        return sorted(pool, key=str.lower)[:limit]
    if t == "rfd":
        return [n for n in pool if n.startswith("RFD: ")][:limit]
    if t in ALIASES and ALIASES[t].lower() not in exclude:
        return [ALIASES[t]]
    exact = [n for n in pool if _norm(n) == t]
    starts = [n for n in pool if _norm(n).startswith(t) and n not in exact]
    words = [n for n in pool if any(w.startswith(t) for w in _norm(n).split()) and n not in exact + starts]
    contains = [n for n in pool if t in _norm(n) and n not in exact + starts + words]
    return (exact + sorted(starts, key=len) + sorted(words, key=len) + sorted(contains, key=len))[:limit]


def quest_label(name: str) -> str:
    _, diff, pts, _qp = QUEST_INFO[name.lower()]
    return f"{diff}, {points_text(pts)}"


def parse_prizes(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;/]", raw) if p.strip()][:10]


def place_name(i: int) -> str:
    n = i + 1
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def points_text(p: int) -> str:
    return f"{p} point{'s' if p != 1 else ''}"


# ------------------------------------------------------------ scoring (pure, no Discord)

@dataclass
class Row:
    rsn: str
    user_id: int | None
    points: int = 0
    quests: int = 0
    last_ts: int | None = None  # when they reached their current score (tie break: earlier wins)
    done: list[str] = field(default_factory=list)  # counted quests, oldest first


def build_rows(players: list[dict], done: list[tuple], start: int, end: int) -> list[Row]:
    """done: (rsn, quest, points, ts). Only quests logged inside the competition window count."""
    rows = {p["rsn"].lower(): Row(p["rsn"], p["user_id"]) for p in players}
    for rsn, _quest, pts, ts in sorted(done, key=lambda d: d[3]):
        r = rows.get(rsn.lower())
        if not r or not start < ts <= end:
            continue
        r.points += pts
        r.quests += 1
        r.last_ts = ts
        r.done.append(QUEST_INFO[_quest.lower()][0] if _quest.lower() in QUEST_INFO else _quest)
    return sorted(rows.values(), key=lambda r: (-r.points, r.last_ts or 1 << 62, r.rsn.lower()))


@dataclass
class QPCheck:
    start: int | None       # QP before the competition
    logged: int             # QP of the quests logged during the competition
    reported: int | None    # latest QP the player reported during the competition
    reported_at: int | None

    @property
    def expected(self) -> int | None:
        return None if self.start is None else self.start + self.logged

    @property
    def status(self) -> str:
        if self.start is None:
            return "no starting QP"
        if self.reported is None:
            return "not checked yet"
        diff = self.reported - self.expected
        if diff == 0:
            return "matches"
        return f"{diff} QP more than logged" if diff > 0 else f"{-diff} QP less than logged"

    def line(self) -> str:
        if self.start is None:
            return f"QP check: no starting QP yet. Logged quests are worth {self.logged} QP."
        txt = f"QP check: started at {self.start}, logged quests add {self.logged}, so expected {self.expected}."
        if self.reported is not None:
            txt += f" Reported {self.reported} <t:{self.reported_at}:R>: {self.status}."
        return txt


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
        # A banner saved in the panel overrides DEFAULT_BANNER; drop it so a new default always shows after an update.
        await self.put("banner", "")

    async def setting(self, key: str, default):
        raw = await self.s.get(f"qc:{key}")
        if raw in (None, ""):
            return default
        return raw if isinstance(default, str) else type(default)(raw)

    async def put(self, key: str, value) -> None:
        await self.s.set(f"qc:{key}", str(value))

    async def window(self) -> tuple[int, int]:
        start = int(await self.setting("start", 0)) or parse_local(DEFAULT_START)
        end = int(await self.setting("end", 0)) or parse_local(DEFAULT_END)
        return start, end

    async def prizes(self) -> list[str]:
        return parse_prizes(await self.setting("prizes", DEFAULT_PRIZES))

    async def set_admins(self, ids: set[int]) -> None:
        await self.put("admins", ",".join(str(i) for i in sorted(ids)))
        PANEL_ADMINS.clear()
        PANEL_ADMINS.update(ids)

    # players
    async def upsert_player(self, rsn: str, user_id: int | None = None, start_qp: int | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO qc_players(rsn, user_id, grp, start_qp, added) VALUES (?,?,'all',?,?) "
            "ON CONFLICT(rsn) DO UPDATE SET user_id=COALESCE(excluded.user_id, qc_players.user_id), "
            "start_qp=COALESCE(excluded.start_qp, qc_players.start_qp)",
            (rsn, user_id, start_qp, int(time.time())))
        await self.conn.commit()

    async def link(self, rsn: str, user_id: int | None) -> None:
        await self.conn.execute("UPDATE qc_players SET user_id=? WHERE rsn=?", (user_id, rsn))
        await self.conn.commit()

    async def set_start(self, rsn: str, qp: int | None) -> None:
        await self.conn.execute("UPDATE qc_players SET start_qp=? WHERE rsn=?", (qp, rsn))
        await self.conn.commit()

    async def add_qp_report(self, rsn: str, qp: int, by_user: int | None, note: str | None = None) -> None:
        await self.conn.execute("INSERT INTO qc_entries(rsn, qp, ts, by_user, proof) VALUES (?,?,?,?,?)",
                                (rsn, qp, int(time.time()), by_user, note))
        await self.conn.commit()

    async def qp_check(self, rsn: str) -> "QPCheck":
        """Starting QP + QP of every logged quest should equal the QP they report now."""
        start, end = await self.window()
        async with self.conn.execute("SELECT start_qp FROM qc_players WHERE rsn=?", (rsn,)) as cur:
            row = await cur.fetchone()
        start_qp = row[0] if row else None
        async with self.conn.execute("SELECT qp, ts FROM qc_entries WHERE rsn=? ORDER BY ts, id", (rsn,)) as cur:
            reports = await cur.fetchall()
        if start_qp is None:  # fall back to the last QP reported before the start (the old QP list)
            before = [q for q, ts in reports if ts <= start]
            start_qp = before[-1] if before else None
        after = [(q, ts) for q, ts in reports if ts > start]
        logged = sum(QUEST_INFO[q.lower()][3] for q, _p, ts, _b, _pr in await self.done_of(rsn)
                     if q.lower() in QUEST_INFO and start < ts <= end)
        return QPCheck(start_qp, logged, after[-1][0] if after else None, after[-1][1] if after else None)

    async def remove_player(self, rsn: str) -> bool:
        cur = await self.conn.execute("DELETE FROM qc_players WHERE rsn=?", (rsn,))
        await self.conn.execute("DELETE FROM qc_done WHERE rsn=?", (rsn,))
        await self.conn.execute("DELETE FROM qc_entries WHERE rsn=?", (rsn,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def players(self) -> list[dict]:
        async with self.conn.execute("SELECT rsn, user_id FROM qc_players ORDER BY rsn COLLATE NOCASE") as cur:
            return [dict(rsn=r[0], user_id=r[1]) for r in await cur.fetchall()]

    async def player(self, rsn: str) -> dict | None:
        async with self.conn.execute("SELECT rsn, user_id FROM qc_players WHERE rsn=?", (rsn.strip(),)) as cur:
            r = await cur.fetchone()
        return dict(rsn=r[0], user_id=r[1]) if r else None

    async def players_of(self, user_id: int) -> list[dict]:
        return [p for p in await self.players() if p["user_id"] == user_id]

    # finished quests
    async def add_done(self, rsn: str, quest: str, points: int, ts: int, by_user: int | None, proof: str | None) -> bool:
        cur = await self.conn.execute("INSERT OR IGNORE INTO qc_done VALUES (?,?,?,?,?,?)",
                                      (rsn, quest, points, ts, by_user, proof))
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_done(self, rsn: str, quest: str) -> bool:
        cur = await self.conn.execute("DELETE FROM qc_done WHERE rsn=? AND quest=?", (rsn, quest))
        await self.conn.commit()
        return cur.rowcount > 0

    async def done_of(self, rsn: str) -> list[tuple]:
        """(quest, points, ts, by_user, proof), newest first."""
        async with self.conn.execute("SELECT quest, points, ts, by_user, proof FROM qc_done WHERE rsn=? ORDER BY ts DESC",
                                     (rsn,)) as cur:
            return await cur.fetchall()

    async def all_done(self) -> list[tuple]:
        async with self.conn.execute("SELECT rsn, quest, points, ts FROM qc_done") as cur:
            return await cur.fetchall()

    async def reset(self, everything: bool) -> None:
        await self.conn.execute("DELETE FROM qc_done")
        if not everything:  # keep the starting QP (reports from before the start), drop the rest
            start, _ = await self.window()
            await self.conn.execute("DELETE FROM qc_entries WHERE ts > ?", (start,))
        if everything:
            await self.conn.execute("DELETE FROM qc_entries")
            await self.conn.execute("DELETE FROM qc_players")
            await self.conn.execute(
                "DELETE FROM kv WHERE key LIKE 'qc:%' AND key NOT LIKE 'qc:msg:%' AND key != 'qc:admins'")
        else:
            for k in ("final_posted", "last_call", "ask_last"):
                await self.conn.execute("DELETE FROM kv WHERE key=?", (f"qc:{k}",))
        await self.conn.commit()

    async def standings(self) -> tuple[list[Row], int, int]:
        start, end = await self.window()
        return build_rows(await self.players(), await self.all_done(), start, end), start, end


# ------------------------------------------------------------ access

PANEL_ADMINS: set[int] = set()  # admins added with /qp_settings, loaded from the database at start


def is_tester(user: discord.abc.User) -> bool:
    return user.id in TESTER_IDS or (user.name or "").lower() in TESTER_NAMES


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


async def admin_only(interaction: discord.Interaction) -> bool:
    """True when the user may use admin tools; otherwise replies and returns False."""
    if is_admin(interaction):
        return True
    await interaction.response.send_message(TEST_MSG if TEST_MODE else "Only competition admins can do this.", ephemeral=True)
    return False


async def play_only(interaction: discord.Interaction) -> bool:
    if can_play(interaction.user):
        return True
    await interaction.response.send_message(TEST_MSG, ephemeral=True)
    return False


# ------------------------------------------------------------ messages

RULES = ("Log every quest you finish during the competition. Points by difficulty: "
         "Novice 1, Intermediate 2, Experienced 4, Master 7, Grandmaster 12. "
         "Each Recipe for Disaster subquest counts as Experienced. Most points wins.")


MEDALS = {0: "\U0001F947", 1: "\U0001F948", 2: "\U0001F949"}  # gold, silver, bronze


def rank_mark(i: int, prizes: list[str]) -> str:
    if i in MEDALS:
        return MEDALS[i]
    if i < len(prizes):
        return "\U0001F3C5"  # sports medal for the other prize places
    return f"`{i + 1:>2}.`"


def pre_start_embeds(rows: list[Row], prizes: list[str]) -> list[discord.Embed]:
    """Before the start: the prize places waiting to be claimed, and everyone taking part (A to Z, no ranking yet)."""
    places = discord.Embed(title="Prizes up for grabs", color=COLOR, description="\n".join(
        f"{rank_mark(i, prizes)} **{place_name(i)} place**: {p}" for i, p in enumerate(prizes)))
    names = sorted((r.rsn for r in rows), key=str.lower)
    people = discord.Embed(title=f"Participants ({len(names)})", color=COLOR)
    if names:
        half = (len(names) + 1) // 2
        people.add_field(name="\u200b", value="\n".join(names[:half])[:1024], inline=True)
        if names[half:]:
            people.add_field(name="\u200b", value="\n".join(names[half:])[:1024], inline=True)
        people.set_footer(text="Everyone starts at 0 points. The leaderboard goes live at the start.")
    else:
        people.description = "No players yet."
    return [places, people]


def board_embeds(rows: list[Row], start: int, end: int, prizes: list[str], final: bool = False,
                 banner: str = "") -> list[discord.Embed]:
    now = int(time.time())
    if final or now > end:
        state = f"Finished <t:{end}:f>. Final results."
    elif now < start:
        state = f"Starts <t:{start}:f> (<t:{start}:R>). Ends <t:{end}:f>."
    else:
        state = f"Live now. Ends <t:{end}:f> (<t:{end}:R>)."
    prize_line = ", ".join(f"{place_name(i)} {p}" for i, p in enumerate(prizes))
    head = discord.Embed(
        title="Quest Competition" + (" (test)" if TEST_MODE else ""), color=COLOR,
        description=(f"{state}\n\n{RULES}\n\n**Prizes**: {prize_line}\n"
                     f"Press **I finished a quest** below, type part of the name and pick it. Not on the list? Ask {ORGANISER}."))
    head.set_footer(text=f"Updated {datetime.fromtimestamp(now, TZ).strftime('%d %b %H:%M')} Amsterdam time")
    if banner:
        head.set_image(url=banner)
    if now < start and not final:
        return [head, *pre_start_embeds(rows, prizes)]
    board = discord.Embed(title="Leaderboard", color=COLOR)
    if not rows:
        board.description = "No players yet."
    else:
        shown = rows[:40]
        heads = []
        for i, r in enumerate(shown):
            mark = rank_mark(i, prizes)
            prize = f"  \u00b7  {prizes[i]}" if i < len(prizes) and r.points else ""
            if r.quests:
                heads.append(f"{mark} **{r.rsn}**: **{points_text(r.points)}**{prize}")
            else:
                heads.append(f"{mark} {r.rsn}: 0 points")
        # Each player's finished quests under their line, newest first, cut to fit Discord's 4096 characters.
        with_quests = sum(1 for r in shown if r.quests) or 1
        budget = max(40, (3900 - sum(len(h) + 1 for h in heads)) // with_quests - 6)
        lines = []
        for i, (h, r) in enumerate(zip(heads, shown)):
            if i == len(prizes) and i > 0:
                lines.append("")  # gap between the prize places and the rest
            lines.append(h)
            if r.quests:
                names, used = [], 0
                for name in reversed(r.done):
                    if used + len(name) + 2 > budget:
                        break
                    names.append(name)
                    used += len(name) + 2
                more = r.quests - len(names)
                lines.append(f"\u2003\u2514 {r.quests} quest{'s' if r.quests != 1 else ''}: " + ", ".join(names)
                             + (f" and {more} more" if more else ""))
        if len(rows) > 40:
            lines.append(f"and {len(rows) - 40} more")
        board.description = "\n".join(lines)[:4096]
        board.set_footer(text="Press See someone's quests for a full list.")
    return [head, board]


def winners_text(rows: list[Row], prizes: list[str]) -> str:
    top = [r for r in rows if r.points > 0][:len(prizes)]
    if not top:
        return "Nobody scored any points."
    out = []
    for i, r in enumerate(top):
        who = f"<@{r.user_id}> ({r.rsn})" if r.user_id else r.rsn
        out.append(f"**{place_name(i)}**: {who} with {points_text(r.points)}, wins {prizes[i]}")
    return "\n".join(out)


async def progress_text(qc: QCStore, p: dict) -> str:
    rows, start, end = await qc.standings()
    place = next((i + 1 for i, r in enumerate(rows) if r.rsn.lower() == p["rsn"].lower()), None)
    r = rows[place - 1] if place else Row(p["rsn"], p["user_id"])
    done = await qc.done_of(p["rsn"])
    lines = [f"**{p['rsn']}**: {points_text(r.points)}, place {place} of {len(rows)}.", (await qc.qp_check(p["rsn"])).line()]
    for quest, pts, ts, _by, proof in done[:20]:
        info = QUEST_INFO.get(quest.lower())
        link = f" [screenshot]({proof})" if proof and proof.startswith("http") else ""
        counted = "" if start < ts <= end else " (outside the competition, not counted)"
        lines.append(f"{info[0] if info else quest}: {info[1] if info else '?'}, +{pts} <t:{ts}:R>{counted}{link}")
    if len(done) > 20:
        lines.append(f"and {len(done) - 20} more")
    if not done:
        lines.append("No quests logged yet.")
    return "\n".join(lines)


# ------------------------------------------------------------ logging a quest

async def resolve_player(qc: QCStore, interaction: discord.Interaction, rsn: str | None) -> tuple[dict | None, str]:
    """The player this person is logging for, or an error to show them."""
    mine = await qc.players_of(interaction.user.id)
    if rsn:
        p = await qc.player(rsn)
        if p and (p["user_id"] == interaction.user.id or is_admin(interaction)):
            return p, ""
        names = ", ".join(x["rsn"] for x in mine)
        return None, (f"{rsn} isn't linked to your Discord account." + (f" Yours: {names}." if names else "")
                      + f" Ask {ORGANISER} to link it.")
    if len(mine) == 1:
        return mine[0], ""
    if mine:
        return None, ("You have more than one account in the competition. Fill in the account name too: "
                      + ", ".join(x["rsn"] for x in mine))
    return None, f"You aren't on the participant list yet. Ask {ORGANISER} to add you and link your Discord account."


async def log_quest(bot, interaction: discord.Interaction, p: dict, quest: str, proof: str | None) -> tuple[str, bool]:
    """Record one finished quest. Returns (reply, saved)."""
    qc: QCStore = bot.qc
    info = QUEST_INFO.get(quest.strip().lower())
    if not info:
        options = search_quests(quest, limit=5)
        return ("I can't find that quest." + (f" Did you mean: {', '.join(options)}?" if options else "")), False
    name, diff, pts, _qp = info
    start, end = await qc.window()
    now = int(time.time())
    ts = now
    if now <= start:
        return f"The competition starts {fmt_local(start)} (Amsterdam time). Only quests finished after that count.", False
    if now > end:
        if not is_admin(interaction):
            return f"The competition ended {fmt_local(end)}. Ask {ORGANISER} if a quest is missing.", False
        ts = end  # admin fixing a missed quest after the end
    if not await qc.add_done(p["rsn"], name, pts, ts, interaction.user.id, proof):
        return f"{name} is already logged for {p['rsn']}.", False
    bot.qc_refresh_soon()
    rows, *_ = await qc.standings()
    place = next((i + 1 for i, r in enumerate(rows) if r.rsn.lower() == p["rsn"].lower()), None)
    total = rows[place - 1].points if place else pts
    return (f"Logged **{name}** for {p['rsn']} ({diff}, +{pts}). "
            f"{p['rsn']} now has {points_text(total)}, place {place} of {len(rows)}."), True


class UndoView(discord.ui.View):
    def __init__(self, rsn: str, quest: str) -> None:
        super().__init__(timeout=900)
        self.rsn, self.quest = rsn, quest

    @discord.ui.button(label="I finished another one", style=discord.ButtonStyle.success)
    async def another(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(QuestSearchModal(self.rsn))

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        qc: QCStore = interaction.client.qc
        p = await qc.player(self.rsn)
        if not p or (p["user_id"] != interaction.user.id and not is_admin(interaction)):
            await interaction.response.send_message("You can only undo your own quests.", ephemeral=True)
            return
        await qc.remove_done(self.rsn, self.quest)
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(content=f"Removed {self.quest} for {self.rsn}.", view=None)


async def send_log_result(interaction: discord.Interaction, p: dict, quest: str, proof: str | None, edit: bool) -> None:
    text, saved = await log_quest(interaction.client, interaction, p, quest, proof)
    view = UndoView(p["rsn"], QUEST_INFO[quest.lower()][0]) if saved else None
    if edit:
        await interaction.response.edit_message(content=text, view=view)
    elif interaction.response.is_done():
        await interaction.followup.send(text, view=view or discord.utils.MISSING, ephemeral=True)
    else:
        await interaction.response.send_message(text, view=view or discord.utils.MISSING, ephemeral=True)


class QuestPickView(discord.ui.View):
    """Search results: pick the quest you finished."""

    def __init__(self, p: dict, matches: list[str]) -> None:
        super().__init__(timeout=900)
        self.p = p
        sel = discord.ui.Select(placeholder="Pick the quest you finished", options=[
            discord.SelectOption(label=m[:100], value=m[:100], description=quest_label(m)) for m in matches[:25]])
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        await send_log_result(interaction, self.p, self.sel.values[0], None, edit=True)


class QuestSearchModal(discord.ui.Modal, title="Which quest did you finish?"):
    term = discord.ui.TextInput(label="Type the name (part of it is fine)", placeholder="e.g. waterfall, dragon slayer, ds2",
                                max_length=60)

    def __init__(self, rsn: str | None = None) -> None:
        super().__init__()
        self.rsn = rsn
        self.acct = None
        if rsn is None:
            self.acct = discord.ui.TextInput(label="Account (only if you have more than one)", required=False, max_length=12)
            self.add_item(self.acct)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        rsn = self.rsn or (str(self.acct.value).strip() if self.acct else "") or None
        p, err = await resolve_player(qc, interaction, rsn)
        if not p:
            await interaction.response.send_message(err, ephemeral=True)
            return
        have = {q[0].lower() for q in await qc.done_of(p["rsn"])}
        matches = search_quests(str(self.term.value), exclude=have)
        if not matches:
            already = [m for m in search_quests(str(self.term.value), limit=3) if m.lower() in have]
            msg = (f"Already logged for {p['rsn']}: {', '.join(already)}." if already
                   else "No quest matches that. Try a shorter part of the name.")
            await interaction.response.send_message(msg, ephemeral=True)
        elif len(matches) == 1:
            await send_log_result(interaction, p, matches[0], None, edit=False)
        else:
            extra = " Showing the first 25: type more of the name to narrow it down." if len(matches) == 25 else ""
            await interaction.response.send_message(f"Which quest did {p['rsn']} finish?{extra}",
                                                    view=QuestPickView(p, matches), ephemeral=True)


class MyQuestsView(discord.ui.View):
    """Under My progress: remove a quest you logged by mistake."""

    def __init__(self, p: dict, done: list[tuple]) -> None:
        super().__init__(timeout=900)
        self.p = p
        if done:
            sel = discord.ui.Select(placeholder="Logged one by mistake? Remove it here", options=[
                discord.SelectOption(label=q[0][:100], value=q[0][:100], description=f"+{q[1]} points") for q in done[:25]])
            sel.callback = self.remove
            self.sel = sel
            self.add_item(sel)

    @discord.ui.button(label="I finished a quest", style=discord.ButtonStyle.success, row=1)
    async def log_here(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(QuestSearchModal(self.p["rsn"]))

    @discord.ui.button(label="Check my QP", style=discord.ButtonStyle.secondary, row=1)
    async def check(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(QPCheckModal(self.p["rsn"]))

    async def remove(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        p = await qc.player(self.p["rsn"])
        if not p or (p["user_id"] != interaction.user.id and not is_admin(interaction)):
            await interaction.response.send_message("You can only remove your own quests.", ephemeral=True)
            return
        await qc.remove_done(p["rsn"], self.sel.values[0])
        interaction.client.qc_refresh_soon()
        done = await qc.done_of(p["rsn"])
        await interaction.response.edit_message(
            content=(f"Removed {self.sel.values[0]}.\n\n" + await progress_text(qc, p))[:2000], view=MyQuestsView(p, done))


class QPCheckModal(discord.ui.Modal):
    """Player (or admin) types the QP shown in game; the bot compares it with the logged quests."""
    value = discord.ui.TextInput(label="Your Quest Points in game right now", placeholder="e.g. 187", max_length=4)

    def __init__(self, rsn: str, which: str = "check") -> None:
        title = {"check": f"Check QP for {rsn}", "start": f"Starting QP for {rsn}", "current": f"Current QP for {rsn}"}[which]
        super().__init__(title=title[:45])
        self.rsn, self.which = rsn, which
        if which == "start":
            self.value.label = "Quest Points before the competition"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.value.value).strip()
        if not raw.isdigit() or int(raw) > 1000:
            await interaction.response.send_message("Type the number only, e.g. 187.", ephemeral=True)
            return
        qc: QCStore = interaction.client.qc
        p = await qc.player(self.rsn)
        if not p or (self.which == "check" and p["user_id"] != interaction.user.id and not is_admin(interaction)) \
                or (self.which != "check" and not is_admin(interaction)):
            await interaction.response.send_message("You can't change this player.", ephemeral=True)
            return
        if self.which == "start":
            await qc.set_start(p["rsn"], int(raw))
        else:
            start, _ = await qc.window()
            if int(time.time()) <= start:
                await qc.set_start(p["rsn"], int(raw))  # before the start this is their starting QP
            else:
                await qc.add_qp_report(p["rsn"], int(raw), interaction.user.id, "admin" if self.which == "current" else None)
        chk = await qc.qp_check(p["rsn"])
        hint = ""
        if chk.reported is not None and chk.expected is not None and chk.reported != chk.expected:
            hint = ("\nYour game shows more QP than your logged quests: did you forget to log one?"
                    if chk.reported > chk.expected else
                    "\nYour logged quests add up to more QP than your game shows: check for a quest logged by mistake.")
        if self.which == "check":
            await interaction.response.send_message(chk.line() + hint, ephemeral=True)
        else:
            await interaction.response.edit_message(content=await player_card(qc, p["rsn"]),
                                                    view=PlayerView(p["rsn"], await qc.done_of(p["rsn"])))


async def show_progress(interaction: discord.Interaction) -> None:
    qc: QCStore = interaction.client.qc
    mine = await qc.players_of(interaction.user.id)
    if not mine:
        await interaction.response.send_message(
            f"You aren't on the participant list yet. Ask {ORGANISER} to add you and link your Discord account.", ephemeral=True)
        return
    p = mine[0]
    text = await progress_text(qc, p)
    for other in mine[1:]:
        text += "\n\n" + await progress_text(qc, other)
    await interaction.response.send_message(text[:2000], view=MyQuestsView(p, await qc.done_of(p["rsn"])), ephemeral=True)


async def open_log(interaction: discord.Interaction) -> None:
    """'I finished a quest': straight to the search box. Only asks which account when someone has several."""
    if not await play_only(interaction):
        return
    mine = await interaction.client.qc.players_of(interaction.user.id)
    if not mine:
        await interaction.response.send_message(
            f"You aren't on the participant list yet. Ask {ORGANISER} to add you and link your Discord account.", ephemeral=True)
        return
    await interaction.response.send_modal(QuestSearchModal(mine[0]["rsn"] if len(mine) == 1 else None))


class QPView(discord.ui.View):
    """Persistent buttons under the board and the daily reminder (they keep working after restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="I finished a quest", style=discord.ButtonStyle.success, custom_id="qc:log")
    async def log_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await open_log(interaction)

    @discord.ui.button(label="My progress", style=discord.ButtonStyle.secondary, custom_id="qc:mine")
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await play_only(interaction):
            await show_progress(interaction)

    @discord.ui.button(label="See someone's quests", style=discord.ButtonStyle.secondary, custom_id="qc:who")
    async def who(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await play_only(interaction):
            return
        rows, *_ = await interaction.client.qc.standings()
        if not rows:
            await interaction.response.send_message("No players yet.", ephemeral=True)
            return
        await interaction.response.send_message("Whose quests do you want to see?",
                                                view=WhoView([{"rsn": r.rsn} for r in rows]), ephemeral=True)


class WhoView(discord.ui.View):
    def __init__(self, players: list[dict]) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.Select(placeholder="Pick a player", options=[
            discord.SelectOption(label=p["rsn"], value=p["rsn"]) for p in players[:25]])
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        rows, start, end = await qc.standings()
        rsn = self.sel.values[0]
        place = next((i for i, r in enumerate(rows) if r.rsn.lower() == rsn.lower()), None)
        if place is None:
            await interaction.response.edit_message(content=f"{rsn} is no longer in the competition.", view=None)
            return
        r = rows[place]
        lines = [f"**{r.rsn}**: {points_text(r.points)}, place {place + 1} of {len(rows)}."]
        for name in reversed(r.done):
            lines.append(f"{name}: {quest_label(name)}" if name.lower() in QUEST_INFO else name)
        if not r.done:
            lines.append("No quests logged yet.")
        await interaction.response.edit_message(content="\n".join(lines)[:2000], view=self)


class LegacyRulesView(discord.ui.View):
    """The old 'Points per quest' button on reminders posted before it was removed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Points per quest", style=discord.ButtonStyle.secondary, custom_id="qc:rules")
    async def rules(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        counts = {t: sum(1 for q in QUESTS if q[1] == t) for t in TIERS}
        text = RULES + "\n\n" + "\n".join(f"{t}: {points_text(POINTS[t])} each ({counts[t]} quests)" for t in TIERS)
        text += "\n\nNot sure about a quest? Search for it with I finished a quest: every result shows its difficulty and points."
        await interaction.response.send_message(text, ephemeral=True)


class LegacyView(discord.ui.View):
    """Old 'Update my QP' buttons on messages from before the points system."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Update my QP", style=discord.ButtonStyle.primary, custom_id="qc:update")
    async def old(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await open_log(interaction)


# ------------------------------------------------------------ player command

async def quest_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=f"{m} ({quest_label(m)})"[:100], value=m[:100]) for m in search_quests(current)]


async def rsn_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    qc: QCStore = interaction.client.qc
    players = await qc.players()
    if not is_admin(interaction):
        players = [p for p in players if p["user_id"] == interaction.user.id]
    cur = current.lower()
    return [app_commands.Choice(name=p["rsn"], value=p["rsn"]) for p in players if cur in p["rsn"].lower()][:25]


@app_commands.command(name="quest", description="Quest competition: log a quest you finished, or see your progress")
@app_commands.describe(quest="Start typing the quest name (leave empty to see your progress)",
                       proof="Optional screenshot", rsn="Only if you have more than one account")
@app_commands.autocomplete(quest=quest_autocomplete, rsn=rsn_autocomplete)
async def quest_cmd(interaction: discord.Interaction, quest: str | None = None,
                    proof: discord.Attachment | None = None, rsn: str | None = None) -> None:
    if not await play_only(interaction):
        return
    if not quest:
        await show_progress(interaction)
        return
    p, err = await resolve_player(interaction.client.qc, interaction, rsn)
    if not p:
        await interaction.response.send_message(err, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if quest.strip().lower() not in QUEST_INFO:
        text, _ = await log_quest(interaction.client, interaction, p, quest, None)
        await interaction.followup.send(text, ephemeral=True)
        return
    await send_log_result(interaction, p, quest.strip(), proof.url if proof else None, edit=False)


# ------------------------------------------------------------ admin panel

async def panel_text(qc: QCStore, bot) -> str:
    start, end = await qc.window()
    ch = await bot.qc_channel()
    players = await qc.players()
    linked = sum(1 for p in players if p["user_id"])
    done = await qc.all_done()
    prizes = await qc.prizes()
    return (f"**Quest competition settings**{' (test mode)' if TEST_MODE else ''}\n"
            f"Start {fmt_local(start)}, end {fmt_local(end)} (Amsterdam time).\n"
            f"Channel: {ch.mention if ch else 'not found'}. Daily reminder at {await qc.setting('ask_hour', DEFAULT_ASK_HOUR)}:00.\n"
            f"Prizes: {', '.join(f'{place_name(i)} {p}' for i, p in enumerate(prizes))}.\n"
            f"Players: {len(players)} ({linked} linked to Discord). Quests logged: {len(done)}.\n"
            f"Admins: {', '.join(f'<@{u}>' for u in sorted(PANEL_ADMINS)) or 'only the testers'}.")


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

    @discord.ui.button(label="Dates and prizes", style=discord.ButtonStyle.secondary, row=1)
    async def dates(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        qc: QCStore = interaction.client.qc
        s, e = await qc.window()
        await interaction.response.send_modal(DatesModal(
            datetime.fromtimestamp(s, TZ).strftime("%Y-%m-%d %H:%M"), datetime.fromtimestamp(e, TZ).strftime("%Y-%m-%d %H:%M"),
            ", ".join(await qc.prizes()), await qc.setting("ask_hour", DEFAULT_ASK_HOUR), await qc.setting("banner", DEFAULT_BANNER)))

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

    @discord.ui.button(label="QP check", style=discord.ButtonStyle.primary, row=3)
    async def qpcheck(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        qc: QCStore = interaction.client.qc
        lines = []
        for p in await qc.players():
            c = await qc.qp_check(p["rsn"])
            mark = {"matches": "OK", "not checked yet": "-", "no starting QP": "?"}.get(c.status, "CHECK")
            exp = c.expected if c.expected is not None else "?"
            rep_ = c.reported if c.reported is not None else "-"
            lines.append(f"`{mark:5}` {p['rsn']}: start {c.start if c.start is not None else '?'} + {c.logged} = {exp}, "
                         f"reported {rep_}" + ("" if mark in ("OK", "-", "?") else f" ({c.status})"))
        text = ("**QP check**: starting QP + QP of logged quests vs the QP they report in game.\n"
                "OK matches, CHECK doesn't match, - not checked yet, ? no starting QP.\n\n" + "\n".join(lines or ["No players yet."]))
        await interaction.followup.send(text[:2000], ephemeral=True)

    @discord.ui.button(label="Admin guide", style=discord.ButtonStyle.primary, row=3)
    async def guide(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(embed=await guide_embed(interaction.client.qc), ephemeral=True)

    @discord.ui.button(label="Reset test data", style=discord.ButtonStyle.danger, row=3)
    async def reset(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_message(
                "**Logged quests only** keeps the player list. **Everything** also removes players, dates, prizes and channel. "
                "This can't be undone.", view=ResetView(), ephemeral=True)


@app_commands.command(name="qp_settings", description="Quest competition: settings and players (admins)")
async def qp_settings_cmd(interaction: discord.Interaction) -> None:
    if not await admin_only(interaction):
        return
    await interaction.response.send_message(await panel_text(interaction.client.qc, interaction.client),
                                            view=SettingsView(), ephemeral=True)


# ---- add, import, find

class AddModal(discord.ui.Modal, title="Add a player"):
    rsn = discord.ui.TextInput(label="RuneScape name", max_length=12)
    qp = discord.ui.TextInput(label="Quest Points before the start (optional)", required=False, max_length=4)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        rsn, raw = str(self.rsn.value).strip(), str(self.qp.value).strip()
        if raw and not raw.isdigit():
            await interaction.response.send_message("Quest Points must be a number.", ephemeral=True)
            return
        existing = await qc.player(rsn)
        await qc.upsert_player(rsn, start_qp=int(raw) if raw else None)
        interaction.client.qc_refresh_soon()
        p = await qc.player(rsn)
        await interaction.response.send_message(
            f"{'Already in' if existing else 'Added'}: **{p['rsn']}**.\nPick their Discord account so they can log quests:",
            view=PlayerView(p["rsn"], []), ephemeral=True)


class ImportModal(discord.ui.Modal, title="Import players"):
    text = discord.ui.TextInput(label="One per line: name, starting QP", style=discord.TextStyle.paragraph,
                                placeholder="Wez, 313\nKodageist, 114\nBeanTown Dad", max_length=4000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        qc: QCStore = interaction.client.qc
        done = []
        for line in str(self.text.value).splitlines():
            parts = [x.strip() for x in re.split(r"[,;\t|]", line)]
            name = parts[0]
            qp = re.match(r"(\d+)", parts[1]) if len(parts) > 1 else None  # "161" or "161/346"
            if name and len(name) <= 12:
                await qc.upsert_player(name, start_qp=int(qp.group(1)) if qp else None)
                done.append(name + (f" ({qp.group(1)})" if qp else ""))
        interaction.client.qc_refresh_soon()
        msg = f"Imported {len(done)} player(s): {', '.join(done)}." if done else "Nothing imported."
        if done:
            msg += "\nThe number becomes their starting QP. Use Edit player to link their Discord accounts."
        await interaction.followup.send(msg[:2000], ephemeral=True)


class FindModal(discord.ui.Modal, title="Edit a player"):
    rsn = discord.ui.TextInput(label="RuneScape name", max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        p = await qc.player(str(self.rsn.value))
        if not p:
            await interaction.response.send_message("No player with that name.", ephemeral=True)
            return
        await interaction.response.send_message(await player_card(qc, p["rsn"]),
                                                view=PlayerView(p["rsn"], await qc.done_of(p["rsn"])), ephemeral=True)


class PickView(discord.ui.View):
    def __init__(self, players: list[dict]) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.Select(placeholder="Player", options=[
            discord.SelectOption(label=p["rsn"], value=p["rsn"],
                                 description="linked to Discord" if p["user_id"] else "not linked yet") for p in players[:25]])
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        qc: QCStore = interaction.client.qc
        rsn = self.sel.values[0]
        await interaction.response.edit_message(content=await player_card(qc, rsn),
                                                view=PlayerView(rsn, await qc.done_of(rsn)))


# ---- one player

async def player_card(qc: QCStore, rsn: str) -> str:
    p = await qc.player(rsn)
    if not p:
        return f"{rsn} is no longer in the competition."
    who = f"<@{p['user_id']}>" if p["user_id"] else "not linked to Discord"
    return (f"Linked to: {who}.\n" + await progress_text(qc, p))[:2000]


class PlayerView(discord.ui.View):
    def __init__(self, rsn: str, done: list[tuple]) -> None:
        super().__init__(timeout=900)
        self.rsn = rsn
        link = discord.ui.UserSelect(placeholder="Link their Discord account", row=0)
        link.callback = self.link
        self.link_select = link
        self.add_item(link)
        if done:
            rm = discord.ui.Select(placeholder="Remove a logged quest", row=1, options=[
                discord.SelectOption(label=q[0][:100], value=q[0][:100], description=f"+{q[1]} points") for q in done[:25]])
            rm.callback = self.remove_quest
            self.rm_select = rm
            self.add_item(rm)

    async def _refresh(self, interaction: discord.Interaction, note: str = "") -> None:
        qc: QCStore = interaction.client.qc
        await interaction.response.edit_message(content=(note + await player_card(qc, self.rsn))[:2000],
                                                view=PlayerView(self.rsn, await qc.done_of(self.rsn)))

    async def link(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        user = self.link_select.values[0]
        if user.bot:
            await interaction.response.send_message("That's a bot.", ephemeral=True)
            return
        await interaction.client.qc.link(self.rsn, user.id)
        interaction.client.qc_refresh_soon()
        await self._refresh(interaction)

    async def remove_quest(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        await interaction.client.qc.remove_done(self.rsn, self.rm_select.values[0])
        interaction.client.qc_refresh_soon()
        await self._refresh(interaction, f"Removed {self.rm_select.values[0]}.\n")

    @discord.ui.button(label="Set starting QP", style=discord.ButtonStyle.secondary, row=3)
    async def set_start(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(QPCheckModal(self.rsn, "start"))

    @discord.ui.button(label="Set current QP", style=discord.ButtonStyle.secondary, row=3)
    async def set_current(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(QPCheckModal(self.rsn, "current"))

    @discord.ui.button(label="Log a quest for them", style=discord.ButtonStyle.primary, row=2)
    async def add_quest(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(QuestSearchModal(self.rsn))

    @discord.ui.button(label="Remove player", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.edit_message(content=f"Remove **{self.rsn}** and all their quests?",
                                                    view=ConfirmView("remove", self.rsn))


# ---- settings pieces

class DatesModal(discord.ui.Modal, title="Dates and prizes"):
    def __init__(self, start: str, end: str, prizes: str, ask_hour, banner: str = "") -> None:
        super().__init__()
        self.start = discord.ui.TextInput(label="Start (Amsterdam time)", default=start, max_length=16)
        self.end = discord.ui.TextInput(label="End (Amsterdam time)", default=end, max_length=16)
        self.prizes = discord.ui.TextInput(label="Prizes for 1st, 2nd, 3rd (commas)", default=prizes, max_length=100)
        self.ask_hour = discord.ui.TextInput(label="Daily reminder hour (0 to 23)", default=str(ask_hour), max_length=2)
        self.banner = discord.ui.TextInput(label="Banner image link (optional)", default=banner or None, required=False,
                                           max_length=400, placeholder="https://... (.png, .jpg or .gif)")
        for x in (self.start, self.end, self.prizes, self.ask_hour, self.banner):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qc: QCStore = interaction.client.qc
        s, e = parse_local(str(self.start.value)), parse_local(str(self.end.value))
        prizes, hour = parse_prizes(str(self.prizes.value)), str(self.ask_hour.value).strip()
        problems = []
        if not s or not e:
            problems.append("dates must look like 2026-11-07 22:00")
        elif e <= s:
            problems.append("the end must be after the start")
        if not prizes:
            problems.append("give at least one prize, e.g. 25M, 15M, 7.5M")
        if not hour.isdigit() or not 0 <= int(hour) <= 23:
            problems.append("the reminder hour must be 0 to 23")
        banner = str(self.banner.value).strip()
        if banner and not re.match(r"https?://\S+$", banner):
            problems.append("the banner must be a link starting with https://")
        if problems:
            await interaction.response.send_message("Not saved: " + "; ".join(problems) + ".", ephemeral=True)
            return
        if (s, e) != await qc.window():
            for k in ("final_posted", "last_call"):
                await qc.put(k, "")
        for k, v in (("start", s), ("end", e), ("prizes", ", ".join(prizes)), ("ask_hour", int(hour)), ("banner", banner)):
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
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(content=f"The board and reminders now go to {ch.mention}.", view=None)


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
            if not u.bot:
                ids.symmetric_difference_update({u.id})
        if interaction.user.id not in ids and not is_tester(interaction.user) and interaction.user.id not in QC_ADMIN_IDS:
            ids.add(interaction.user.id)  # don't let someone lock themselves out
        await interaction.client.qc.set_admins(ids)
        await interaction.response.edit_message(content=admins_text(), view=AdminsView())


class ResetView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def _do(self, interaction: discord.Interaction, everything: bool) -> None:
        if not await admin_only(interaction):
            return
        await interaction.client.qc.reset(everything)
        interaction.client.qc_refresh_soon()
        await interaction.response.edit_message(
            content="Wiped." + (" Dates, prizes and channel are back to the defaults." if everything else ""), view=None)

    @discord.ui.button(label="Logged quests only", style=discord.ButtonStyle.danger)
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
            qc: QCStore = interaction.client.qc
            await interaction.response.edit_message(content=await player_card(qc, self.rsn),
                                                    view=PlayerView(self.rsn, await qc.done_of(self.rsn)))
        else:
            await interaction.response.edit_message(content="Nothing changed.", view=None)


async def guide_embed(qc: QCStore) -> discord.Embed:
    hour = await qc.setting("ask_hour", DEFAULT_ASK_HOUR)
    start, end = await qc.window()
    e = discord.Embed(title="Quest competition: admin guide", color=COLOR, description=(
        f"Runs from <t:{start}:f> to <t:{end}:f>. {RULES} "
        "Only quests logged between the start and the end count. Everything below is in **/qp_settings**.\n\n"
        "**Before the start**\n"
        "1. **Add player** for anyone new: RuneScape name and their QP now. "
        "Or paste a list in **Import list**, one per line, like `Wez, 313`.\n"
        "2. **Edit player**, pick the name, then pick their Discord account in the top dropdown. "
        "Without a link they can't log quests. The player list shows who is still \"not linked yet\".\n"
        "3. Make sure everyone has their real starting QP: **Edit player**, **Set starting QP**. "
        "Players on 1 QP will fail the QP check later.\n\n"
        "**During the competition**\n"
        "Players press **I finished a quest**, type part of the name and pick it. They can undo their own mistakes.\n"
        "Someone stuck? **Edit player**, then **Log a quest for them**. Logged wrong? Remove it with the dropdown there.\n"
        "Press **QP check** now and then. Starting QP plus the QP of their logged quests should match their QP in game. "
        "CHECK means it doesn't add up: ask for a screenshot of their quest list. "
        "Players report their QP with **My progress**, **Check my QP**, or you set it with **Set current QP**.\n\n"
        "**The bot does this by itself**\n"
        f"Keeps the leaderboard up to date, posts a reminder every day at {hour}:00 and pings players who logged nothing "
        "in 20 hours, posts a last call 2 hours before the end, and posts the winners at the end. "
        "After the end, only admins can still add a missed quest.\n\n"
        "**Handy**\n"
        "**Repost board** if the leaderboard got buried in the chat. **Post reminder now** to nudge everyone.\n"
        "Leave **Dates and prizes**, **Post results** and **Reset test data** to Lucas."))
    e.set_footer(text="Players only need the I finished a quest button.")
    return e


# ------------------------------------------------------------ bot mixin

class QuestCompMixin:
    """Mixed into TradingCow. Needs self.store, self.get_channel and self.fetch_channel."""

    async def qc_setup(self) -> None:
        self.qc = QCStore(self.store)
        await self.qc.setup()
        self.add_view(QPView())
        self.add_view(LegacyView())
        self.add_view(LegacyRulesView())
        self.tree.add_command(quest_cmd)
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
        rows, start, end = await self.qc.standings()
        embeds = board_embeds(rows, start, end, await self.qc.prizes(), final, await self.qc.setting("banner", DEFAULT_BANNER))
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
        """Post the reminder with the buttons. Pings linked players who logged nothing in 20 hours."""
        ch = await self.qc_channel()
        if not ch:
            return
        start, end = await self.qc.window()
        now = int(time.time())
        last_log: dict[str, int] = {}
        for rsn, _q, _p, ts in await self.qc.all_done():
            last_log[rsn.lower()] = max(ts, last_log.get(rsn.lower(), 0))
        stale = sorted({p["user_id"] for p in await self.qc.players()
                        if p["user_id"] and now - last_log.get(p["rsn"].lower(), 0) > 20 * 3600})
        if TEST_MODE:
            stale = [u for u in stale if u in TESTER_IDS]  # never ping real players while testing
        if last_call:
            text = f"**Last call.** The Quest Competition ends <t:{end}:R>. Log every quest you finished so it counts."
        elif now < start:
            text = f"The Quest Competition starts <t:{start}:R>. From then on, log every quest you finish with the button below."
            stale = []
        else:
            text = "Daily check-in: finished any quests? Log them with the button below."
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
        rows, *_ = await self.qc.standings()
        await ch.send("**Quest Competition results**\n" + winners_text(rows, await self.qc.prizes())
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

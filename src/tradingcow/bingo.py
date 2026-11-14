"""Clan bingo for TradingCow: two teams race on one 5x5 board.

Players submit a tile with /bingo_submit and a screenshot. The bot posts the proof in the bingo channel with
Approve / Reject buttons for admins. Approved tiles light up in the team's colour on the board image.
Score: tile points (Easy 1, Medium 2, Hard 3), +5 per completed line, +15 for a full board.
Admins manage teams from /bingo_settings.
"""
from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

from .bingo_tiles import FREE_INDEX, FULL_BONUS, LINE_BONUS, POINTS, TILES
from .quest_comp import admin_only

log = logging.getLogger("tradingcow.bingo")

BINGO_CHANNEL_ID = int(os.getenv("BINGO_CHANNEL_ID", "1571254714836983848"))
TEAMS = ("red", "blue")
DEFAULT_NAMES = {"red": "Team Red", "blue": "Team Blue"}
TEAM_RGB = {"red": (214, 69, 65), "blue": (66, 135, 245)}
TEAM_COLOR = {"red": 0xD64541, "blue": 0x4287F5}
GOLD = (212, 175, 55)
EMBED_GOLD = 0xD4AF37
TEAM_DOT = {"red": "\U0001F534", "blue": "\U0001F535"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bingo_members (
    user_id INTEGER PRIMARY KEY,
    team TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bingo_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team TEXT NOT NULL,
    tile INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,          -- pending, approved, rejected
    ts INTEGER NOT NULL,
    msg_id INTEGER,
    reviewer INTEGER
);
"""

LINES = [[r * 5 + c for c in range(5)] for r in range(5)] + [[r * 5 + c for r in range(5)] for c in range(5)] + \
        [[i * 6 for i in range(5)], [(i + 1) * 4 for i in range(5)]]


# ------------------------------------------------------------ scoring (pure)

@dataclass
class TeamState:
    team: str
    progress: dict[int, int] = field(default_factory=dict)  # tile -> approved amount
    done: set[int] = field(default_factory=set)
    lines: int = 0
    full: bool = False
    points: int = 0


def team_state(team: str, approved: list[tuple[int, int]]) -> TeamState:
    """approved: (tile, amount) for this team."""
    st = TeamState(team)
    for tile, amount in approved:
        st.progress[tile] = st.progress.get(tile, 0) + amount
    st.done = {i for i, t in enumerate(TILES) if i == FREE_INDEX or st.progress.get(i, 0) >= t[2]}
    st.lines = sum(1 for line in LINES if all(i in st.done for i in line))
    st.full = len(st.done) == 25
    st.points = sum(POINTS.get(TILES[i][1], 0) for i in st.done) + st.lines * LINE_BONUS + (FULL_BONUS if st.full else 0)
    return st


# ------------------------------------------------------------ board image

def _font(size: int) -> ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_board(states: dict[str, TeamState], names: dict[str, str], title: str) -> bytes:
    tile, gap, pad, head, foot = 190, 10, 30, 120, 90
    w = pad * 2 + tile * 5 + gap * 4
    h = head + tile * 5 + gap * 4 + foot + pad
    img = Image.new("RGB", (w, h), (30, 31, 34))
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_tile, f_small, f_tiny = _font(40), _font(22), _font(19), _font(16), _font(14)

    d.text((w // 2, 40), title, font=f_title, fill=GOLD, anchor="mm")
    sub_x = w // 2
    left = f"{names['red']}: {states['red'].points} pts"
    right = f"{names['blue']}: {states['blue'].points} pts"
    d.text((sub_x - 20, 88), left, font=f_sub, fill=TEAM_RGB["red"], anchor="rm")
    d.text((sub_x, 88), "vs", font=f_sub, fill=(180, 180, 180), anchor="mm")
    d.text((sub_x + 20, 88), right, font=f_sub, fill=TEAM_RGB["blue"], anchor="lm")

    diff_dots = {"Easy": 1, "Medium": 2, "Hard": 3}
    for i, (name, diff, target, _how) in enumerate(TILES):
        r, c = divmod(i, 5)
        x0, y0 = pad + c * (tile + gap), head + r * (tile + gap)
        x1, y1 = x0 + tile, y0 + tile
        free = i == FREE_INDEX
        d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=(58, 52, 40) if free else (49, 51, 56),
                            outline=GOLD if free else (78, 80, 88), width=3 if free else 2)
        if free:
            d.text(((x0 + x1) // 2, (y0 + y1) // 2 - 10), "★", font=_font(56), fill=GOLD, anchor="mm")
            d.text(((x0 + x1) // 2, y1 - 30), "FREE", font=f_sub, fill=GOLD, anchor="mm")
            continue
        # difficulty dots and label
        for k in range(diff_dots[diff]):
            d.ellipse((x0 + 12 + k * 16, y0 + 12, x0 + 24 + k * 16, y0 + 24), fill=GOLD)
        d.text((x1 - 12, y0 + 18), f"{POINTS[diff]} pt" + ("s" if POINTS[diff] > 1 else ""), font=f_tiny,
               fill=(190, 190, 190), anchor="rm")
        lines = _wrap(d, name, f_tile, tile - 24)
        ty = y0 + 78 - (len(lines) - 1) * 12
        for ln in lines:
            d.text(((x0 + x1) // 2, ty), ln, font=f_tile, fill=(235, 235, 235), anchor="mm")
            ty += 24
        # team strips: left red, right blue
        sy0, sy1 = y1 - 44, y1 - 10
        half = (tile - 30) // 2
        for k, team in enumerate(TEAMS):
            sx0 = x0 + 10 + k * (half + 10)
            sx1 = sx0 + half
            st = states[team]
            have = st.progress.get(i, 0)
            done = i in st.done
            col = TEAM_RGB[team]
            if done:
                d.rounded_rectangle((sx0, sy0, sx1, sy1), radius=8, fill=col)
                d.text(((sx0 + sx1) // 2, (sy0 + sy1) // 2), "✓", font=f_sub, fill=(255, 255, 255), anchor="mm")
            else:
                d.rounded_rectangle((sx0, sy0, sx1, sy1), radius=8, outline=col, width=2)
                if target > 1:
                    if have:
                        fill_w = int((sx1 - sx0) * min(1, have / target))
                        d.rounded_rectangle((sx0, sy0, sx0 + max(fill_w, 8), sy1), radius=8,
                                            fill=tuple(int(v * 0.45) for v in col))
                    d.text(((sx0 + sx1) // 2, (sy0 + sy1) // 2), f"{have}/{target}", font=f_small,
                           fill=(235, 235, 235), anchor="mm")
        # tiles in a completed line get the team's colour as border (red outer, blue inner)
        for k, team in enumerate(TEAMS):
            if any(i in line and all(j in states[team].done for j in line) for line in LINES):
                inset = k * 5
                d.rounded_rectangle((x0 + inset, y0 + inset, x1 - inset, y1 - inset), radius=14 - inset,
                                    outline=TEAM_RGB[team], width=4)

    # legend
    ly = head + tile * 5 + gap * 4 + 30
    legend = (f"Easy 1  ·  Medium 2  ·  Hard 3  ·  Line +{LINE_BONUS}  ·  Full board +{FULL_BONUS}")
    d.text((w // 2, ly), legend, font=f_sub, fill=(200, 200, 200), anchor="mm")
    d.text((w // 2, ly + 34), "Left strip = " + names["red"] + "   ·   Right strip = " + names["blue"]
           + "   ·   Coloured border = full line",
           font=f_small, fill=(160, 160, 160), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------ storage

class BingoStore:
    def __init__(self, store) -> None:
        self.s = store

    @property
    def conn(self):
        return self.s.conn

    async def setup(self) -> None:
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def names(self) -> dict[str, str]:
        return {t: (await self.s.get(f"bingo:name:{t}")) or DEFAULT_NAMES[t] for t in TEAMS}

    async def set_name(self, team: str, name: str) -> None:
        await self.s.set(f"bingo:name:{team}", name)

    async def team_of(self, user_id: int) -> str | None:
        async with self.conn.execute("SELECT team FROM bingo_members WHERE user_id=?", (user_id,)) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def members(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {t: [] for t in TEAMS}
        async with self.conn.execute("SELECT user_id, team FROM bingo_members ORDER BY rowid") as cur:
            async for uid, team in cur:
                out.setdefault(team, []).append(uid)
        return out

    async def set_member(self, user_id: int, team: str | None) -> None:
        if team is None:
            await self.conn.execute("DELETE FROM bingo_members WHERE user_id=?", (user_id,))
        else:
            await self.conn.execute("INSERT INTO bingo_members(user_id, team) VALUES (?,?) "
                                    "ON CONFLICT(user_id) DO UPDATE SET team=excluded.team", (user_id, team))
        await self.conn.commit()

    async def add_sub(self, team: str, tile: int, user_id: int, amount: int) -> int:
        cur = await self.conn.execute("INSERT INTO bingo_subs(team, tile, user_id, amount, status, ts) VALUES (?,?,?,?,?,?)",
                                      (team, tile, user_id, amount, "pending", int(time.time())))
        await self.conn.commit()
        return cur.lastrowid

    async def set_sub_msg(self, sub_id: int, msg_id: int) -> None:
        await self.conn.execute("UPDATE bingo_subs SET msg_id=? WHERE id=?", (msg_id, sub_id))
        await self.conn.commit()

    async def sub_by_msg(self, msg_id: int) -> dict | None:
        async with self.conn.execute("SELECT id, team, tile, user_id, amount, status FROM bingo_subs WHERE msg_id=?",
                                     (msg_id,)) as cur:
            r = await cur.fetchone()
        return dict(id=r[0], team=r[1], tile=r[2], user_id=r[3], amount=r[4], status=r[5]) if r else None

    async def review(self, sub_id: int, status: str, reviewer: int) -> None:
        await self.conn.execute("UPDATE bingo_subs SET status=?, reviewer=? WHERE id=?", (status, reviewer, sub_id))
        await self.conn.commit()

    async def pending(self, team: str | None = None) -> list[tuple]:
        sql = "SELECT id, team, tile, user_id, amount, msg_id FROM bingo_subs WHERE status='pending'"
        args: tuple = ()
        if team:
            sql += " AND team=?"
            args = (team,)
        async with self.conn.execute(sql + " ORDER BY ts", args) as cur:
            return await cur.fetchall()

    async def states(self) -> dict[str, TeamState]:
        approved: dict[str, list[tuple[int, int]]] = {t: [] for t in TEAMS}
        async with self.conn.execute("SELECT team, tile, amount FROM bingo_subs WHERE status='approved'") as cur:
            async for team, tile, amount in cur:
                approved.setdefault(team, []).append((tile, amount))
        return {t: team_state(t, approved.get(t, [])) for t in TEAMS}

    async def reset(self, members_too: bool) -> None:
        await self.conn.execute("DELETE FROM bingo_subs")
        if members_too:
            await self.conn.execute("DELETE FROM bingo_members")
        await self.conn.commit()


# ------------------------------------------------------------ messages

def tile_label(i: int) -> str:
    name, diff, target, _ = TILES[i]
    pts = POINTS.get(diff, 0)
    return f"{name} ({diff}, {pts} pt{'s' if pts != 1 else ''}" + (f", team total {target}" if target > 1 else "") + ")"


async def board_message(bs: BingoStore) -> tuple[discord.Embed, discord.File]:
    states, names, members = await bs.states(), await bs.names(), await bs.members()
    png = render_board(states, names, "CLAN BINGO")
    e = discord.Embed(title="\U0001F3B2 Clan Bingo (test)", color=EMBED_GOLD, description=(
        "Get drops, submit them with **/bingo_submit** and a screenshot, and light up your team's tiles.\n"
        f"Easy 1 · Medium 2 · Hard 3 · every full line +{LINE_BONUS} · full board +{FULL_BONUS}"))
    for t in TEAMS:
        st = states[t]
        who = ", ".join(f"<@{u}>" for u in members.get(t, [])) or "No players yet"
        e.add_field(name=f"{TEAM_DOT[t]} {names[t]}: {st.points} point{'s' if st.points != 1 else ''}",
                    value=f"{len(st.done) - 1} tiles · {st.lines} line{'s' if st.lines != 1 else ''}\n{who}"[:1024],
                    inline=True)
    e.set_image(url="attachment://bingo.png")
    e.set_footer(text=f"Updated {time.strftime('%d %b %H:%M', time.gmtime())} UTC")
    return e, discord.File(io.BytesIO(png), filename="bingo.png")


HOW_TO = (
    "**How to submit a tile**\n"
    "1. Get the drop or finish the task.\n"
    "2. Type **/bingo_submit**, start typing the tile name and pick it.\n"
    "3. Attach your screenshot (drop message in chat visible). For team totals like *100 KBD kills*, fill in the amount.\n"
    "4. An admin approves it and your team's strip on the board lights up.\n\n"
    "**Scoring**: Easy 1, Medium 2, Hard 3 points. Every full row, column or diagonal +5. Full board +15. "
    "The FREE centre counts for lines.\n"
    "**Strips**: left is red team, right is blue team. Numbers like 40/100 show team progress.")


class BoardView(discord.ui.View):
    """Persistent buttons under the board."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="How to submit", style=discord.ButtonStyle.success, custom_id="bingo:how")
    async def how(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_message(HOW_TO, ephemeral=True)

    @discord.ui.button(label="My team", style=discord.ButtonStyle.secondary, custom_id="bingo:team")
    async def team(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        bs: BingoStore = interaction.client.bingo
        team = await bs.team_of(interaction.user.id)
        if not team:
            await interaction.response.send_message("You aren't in a team yet. Ask an admin to add you.", ephemeral=True)
            return
        names, states, members = await bs.names(), await bs.states(), await bs.members()
        st = states[team]
        todo = [i for i in range(25) if i not in st.done]
        lines = [f"**{names[team]}**: {st.points} point{'s' if st.points != 1 else ''}, {len(st.done) - 1} tiles, {st.lines} lines.",
                 "Members: " + ", ".join(f"<@{u}>" for u in members[team]), "", "**Still open**"]
        for i in todo:
            have = st.progress.get(i, 0)
            extra = f" ({have}/{TILES[i][2]})" if TILES[i][2] > 1 else ""
            lines.append(f"• {TILES[i][0]}{extra}")
        pend = await bs.pending(team)
        if pend:
            lines += ["", "**Waiting for approval**: " + ", ".join(TILES[p[2]][0] for p in pend)]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @discord.ui.button(label="Tile list", style=discord.ButtonStyle.secondary, custom_id="bingo:tiles")
    async def tiles(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        lines = []
        for diff in ("Easy", "Medium", "Hard"):
            lines.append(f"**{diff} ({POINTS[diff]} pt{'s' if POINTS[diff] > 1 else ''})**")
            for name, d, target, how in TILES:
                if d == diff:
                    lines.append(f"• {name}" + (f" (team total {target})" if target > 1 else "") + f": {how}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


class ReviewView(discord.ui.View):
    """Persistent Approve / Reject buttons on every submission."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _review(self, interaction: discord.Interaction, status: str) -> None:
        if not await admin_only(interaction):
            return
        bs: BingoStore = interaction.client.bingo
        sub = await bs.sub_by_msg(interaction.message.id)
        if not sub or sub["status"] != "pending":
            await interaction.response.send_message("This submission was already handled.", ephemeral=True)
            return
        if sub["user_id"] == interaction.user.id and not is_admin_override(interaction):
            await interaction.response.send_message("Another admin has to review your own submission.", ephemeral=True)
            return
        await bs.review(sub["id"], status, interaction.user.id)
        e = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        ok = status == "approved"
        e.color = 0x2ECC71 if ok else 0x95A5A6
        e.set_footer(text=f"{'Approved' if ok else 'Rejected'} by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=e, view=None)
        await interaction.client.bingo_refresh()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="bingo:approve")
    async def approve(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._review(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="bingo:reject")
    async def reject(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._review(interaction, "rejected")


def is_admin_override(interaction: discord.Interaction) -> bool:
    """While testing with few admins, testers may approve their own submissions."""
    from .quest_comp import is_tester
    return is_tester(interaction.user)


# ------------------------------------------------------------ commands

async def tile_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    cur = current.lower()
    return [app_commands.Choice(name=tile_label(i)[:100], value=i) for i, t in enumerate(TILES)
            if i != FREE_INDEX and cur in t[0].lower()][:25]


@app_commands.command(name="bingo_submit", description="Clan bingo: submit a tile with a screenshot")
@app_commands.describe(tile="Start typing the tile name", screenshot="Screenshot with the drop message visible",
                       amount="For team totals (kills, clues, pieces): how many this screenshot shows")
@app_commands.autocomplete(tile=tile_autocomplete)
async def bingo_submit_cmd(interaction: discord.Interaction, tile: int, screenshot: discord.Attachment,
                           amount: app_commands.Range[int, 1, 1000] = 1) -> None:
    bs: BingoStore = interaction.client.bingo
    if not 0 <= tile < 25 or tile == FREE_INDEX:
        await interaction.response.send_message("Pick a tile from the list.", ephemeral=True)
        return
    team = await bs.team_of(interaction.user.id)
    if not team:
        await interaction.response.send_message("You aren't in a team yet. Ask an admin to add you.", ephemeral=True)
        return
    if not (screenshot.content_type or "").startswith("image/"):
        await interaction.response.send_message("The screenshot has to be an image.", ephemeral=True)
        return
    states = await bs.states()
    if tile in states[team].done:
        await interaction.response.send_message(f"Your team already has **{TILES[tile][0]}**.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    names = await bs.names()
    name, diff, target, _how = TILES[tile]
    if target == 1:
        amount = 1
    sub_id = await bs.add_sub(team, tile, interaction.user.id, amount)
    e = discord.Embed(title=f"Submission: {name}", color=TEAM_COLOR[team], description=(
        f"{interaction.user.mention} for **{names[team]}**\n{diff}, {POINTS[diff]} point{'s' if POINTS[diff] > 1 else ''}"
        + (f"\nAmount: **{amount}** (team now {states[team].progress.get(tile, 0)}/{target} approved)" if target > 1 else "")))
    e.set_image(url=f"attachment://{screenshot.filename}")
    e.set_footer(text="Waiting for an admin")
    ch = interaction.client.get_channel(BINGO_CHANNEL_ID) or await interaction.client.fetch_channel(BINGO_CHANNEL_ID)
    msg = await ch.send(embed=e, file=await screenshot.to_file(), view=ReviewView(),
                        allowed_mentions=discord.AllowedMentions.none())
    await bs.set_sub_msg(sub_id, msg.id)
    await interaction.followup.send(f"Submitted **{name}** for {names[team]}. An admin will check it: {msg.jump_url}",
                                    ephemeral=True)


# ---- admin panel

async def panel_text(bs: BingoStore) -> str:
    names, members, states = await bs.names(), await bs.members(), await bs.states()
    pend = await bs.pending()
    lines = ["**Clan bingo settings**"]
    for t in TEAMS:
        who = ", ".join(f"<@{u}>" for u in members[t]) or "nobody yet"
        lines.append(f"{TEAM_DOT[t]} **{names[t]}** ({len(members[t])}): {who}. "
                     f"{states[t].points} point{'s' if states[t].points != 1 else ''}.")
    lines.append(f"Waiting for approval: {len(pend)}.")
    if pend:
        lines += [f"• {names[p[1]]}: {TILES[p[2]][0]} by <@{p[3]}>" for p in pend[:10]]
    return "\n".join(lines)[:2000]


class BingoPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)
        for t in TEAMS:
            sel = discord.ui.UserSelect(placeholder=f"Add players to {DEFAULT_NAMES[t]}", min_values=1, max_values=10)
            sel.callback = self._make_add(t, sel)
            self.add_item(sel)

    def _make_add(self, team: str, sel: discord.ui.UserSelect):
        async def callback(interaction: discord.Interaction) -> None:
            if not await admin_only(interaction):
                return
            bs: BingoStore = interaction.client.bingo
            for u in sel.values:
                if not u.bot:
                    await bs.set_member(u.id, team)
            await interaction.client.bingo_refresh()
            await interaction.response.edit_message(content=await panel_text(bs), view=BingoPanel())
        return callback

    @discord.ui.button(label="Remove a player", style=discord.ButtonStyle.secondary, row=2)
    async def remove(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.edit_message(content="Pick the players to remove from their team:", view=RemoveView())

    @discord.ui.button(label="Rename teams", style=discord.ButtonStyle.secondary, row=2)
    async def rename(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.send_modal(RenameModal(await interaction.client.bingo.names()))

    @discord.ui.button(label="Repost board", style=discord.ButtonStyle.primary, row=2)
    async def repost(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await admin_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.client.bingo_refresh(repost=True)
        await interaction.followup.send("Board reposted.", ephemeral=True)

    @discord.ui.button(label="Reset bingo", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if await admin_only(interaction):
            await interaction.response.edit_message(content="**Submissions only** keeps the teams. **Everything** also "
                                                    "empties the teams. This can't be undone.", view=ResetView())


class RemoveView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)
        sel = discord.ui.UserSelect(placeholder="Players to remove", min_values=1, max_values=10)
        sel.callback = self.pick
        self.sel = sel
        self.add_item(sel)

    async def pick(self, interaction: discord.Interaction) -> None:
        if not await admin_only(interaction):
            return
        bs: BingoStore = interaction.client.bingo
        for u in self.sel.values:
            await bs.set_member(u.id, None)
        await interaction.client.bingo_refresh()
        await interaction.response.edit_message(content=await panel_text(bs), view=BingoPanel())


class RenameModal(discord.ui.Modal, title="Team names"):
    def __init__(self, names: dict[str, str]) -> None:
        super().__init__()
        self.inputs = {}
        for t in TEAMS:
            ti = discord.ui.TextInput(label=f"{DEFAULT_NAMES[t]} name", default=names[t], max_length=30)
            self.inputs[t] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bs: BingoStore = interaction.client.bingo
        for t, ti in self.inputs.items():
            await bs.set_name(t, str(ti.value).strip() or DEFAULT_NAMES[t])
        await interaction.client.bingo_refresh()
        await interaction.response.edit_message(content=await panel_text(bs), view=BingoPanel())


class ResetView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def _do(self, interaction: discord.Interaction, members_too: bool) -> None:
        if not await admin_only(interaction):
            return
        bs: BingoStore = interaction.client.bingo
        await bs.reset(members_too)
        await interaction.client.bingo_refresh()
        await interaction.response.edit_message(content=await panel_text(bs), view=BingoPanel())

    @discord.ui.button(label="Submissions only", style=discord.ButtonStyle.danger)
    async def subs(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, False)

    @discord.ui.button(label="Everything", style=discord.ButtonStyle.danger)
    async def everything(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=await panel_text(interaction.client.bingo), view=BingoPanel())


@app_commands.command(name="bingo_settings", description="Clan bingo: teams and board (admins)")
async def bingo_settings_cmd(interaction: discord.Interaction) -> None:
    if not await admin_only(interaction):
        return
    await interaction.response.send_message(await panel_text(interaction.client.bingo), view=BingoPanel(), ephemeral=True)


# ------------------------------------------------------------ bot hook

async def setup_bingo(bot) -> None:
    bot.bingo = BingoStore(bot.store)
    await bot.bingo.setup()
    bot.add_view(BoardView())
    bot.add_view(ReviewView())
    bot.tree.add_command(bingo_submit_cmd)
    bot.tree.add_command(bingo_settings_cmd)

    async def bingo_refresh(repost: bool = False) -> None:
        try:
            ch = bot.get_channel(BINGO_CHANNEL_ID) or await bot.fetch_channel(BINGO_CHANNEL_ID)
            embed, file = await board_message(bot.bingo)
            key = f"bingo:msg:board:{ch.id}"
            old = await bot.store.get(key)
            if old:
                try:
                    msg = await ch.fetch_message(int(old))
                    if repost:
                        await msg.delete()
                    else:
                        await msg.edit(embed=embed, attachments=[file], view=BoardView())
                        return
                except discord.HTTPException:
                    pass
            msg = await ch.send(embed=embed, file=file, view=BoardView())
            await bot.store.set(key, str(msg.id))
        except Exception:
            log.exception("Bingo board refresh failed")

    bot.bingo_refresh = bingo_refresh

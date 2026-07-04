from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord

from src.db.repo.economy_repo import GUILD_EN, GUILD_NL
from src.db.repo.leaderboard_posts_repo import LeaderboardPostsRepository
from src.db.repo.leaderboard_repo import LeaderboardRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaderboardConfig:
    platform: str
    channel_id: int
    english_game_keys: list[str]
    game_labels: dict[str, str] = field(default_factory=dict)
    limit: int = 10
    debounce_seconds: float = 10.0
    board_key: str = "english_dropdown_v1"
    include_all_guilds: bool = False


class LeaderboardPublisher:
    """
    One-message leaderboard with tabs:
      - Today
      - This Week
      - By Game
      - All Time

    board_key must be unique per channel so boards do not share a stored message ID.
    """

    def __init__(
        self,
        *,
        config: LeaderboardConfig,
        leaderboard_repo: LeaderboardRepository,
        posts_repo: LeaderboardPostsRepository,
    ) -> None:
        self._config = config
        self._leaderboard_repo = leaderboard_repo
        self._posts_repo = posts_repo

        self._bot: Optional[discord.Client] = None
        self._lock = asyncio.Lock()

        self._pending_task: Optional[asyncio.Task] = None
        self._scheduled_at: Optional[float] = None

    def _guild_id(self) -> str | None:
        if self._config.include_all_guilds:
            return None
        return GUILD_NL if self._config.board_key.startswith("dutch_") else GUILD_EN

    def _is_dutch(self) -> bool:
        return self._guild_id() == GUILD_NL

    def _is_combined(self) -> bool:
        return self._config.include_all_guilds

    def set_bot(self, bot: discord.Client) -> None:
        self._bot = bot
        logger.info(
            "LeaderboardPublisher attached to bot. channel_id=%s board_key=%s guild_id=%s",
            self._config.channel_id,
            self._config.board_key,
            self._guild_id() or "all",
        )
        try:
            bot.add_view(self.build_persistent_view())
        except Exception:
            logger.debug("LeaderboardPublisher: add_view failed (likely already registered)", exc_info=True)

    def build_persistent_view(self) -> discord.ui.View:
        return LeaderboardDropdownView(publisher=self)

    def schedule_refresh(self) -> None:
        if not self._bot:
            logger.warning("LeaderboardPublisher.schedule_refresh called but bot is not set.")
            return

        loop = getattr(self._bot, "loop", None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("LeaderboardPublisher.schedule_refresh: no running loop")
                return

        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

        delay = float(self._config.debounce_seconds)
        self._scheduled_at = loop.time() + delay
        logger.info("Leaderboard refresh scheduled in %.1fs (board_key=%s)", delay, self._config.board_key)

        async def runner() -> None:
            try:
                await asyncio.sleep(delay)
                await self.refresh_now(default_tab="today")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("LeaderboardPublisher debounced refresh crashed")

        try:
            self._pending_task = loop.create_task(runner())
        except Exception:
            self._pending_task = asyncio.create_task(runner())

    @staticmethod
    def _utc_start_of_today() -> datetime:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _utc_start_of_week_monday() -> datetime:
        today0 = LeaderboardPublisher._utc_start_of_today()
        return today0 - timedelta(days=today0.weekday())

    @staticmethod
    def _fmt_sqlite(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    async def refresh_now(self, default_tab: str = "today") -> None:
        async with self._lock:
            if not self._bot:
                logger.warning(
                    "LeaderboardPublisher.refresh_now called but bot is not set. board_key=%s",
                    self._config.board_key,
                )
                return

            channel = self._bot.get_channel(self._config.channel_id)
            if channel is None:
                try:
                    channel = await self._bot.fetch_channel(self._config.channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception("Failed to fetch leaderboard channel channel_id=%s", self._config.channel_id)
                    return

            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                logger.warning("Target leaderboard channel is not a text channel/thread: %r", channel)
                return

            embed = await self.build_embed(tab=default_tab)
            view = self.build_persistent_view()

            msg_id = await self._upsert_single_message(channel=channel, embed=embed, view=view)
            if msg_id:
                logger.info(
                    "Leaderboard message updated (message_id=%s board_key=%s)",
                    msg_id,
                    self._config.board_key,
                )
            else:
                logger.warning("Leaderboard message update failed board_key=%s", self._config.board_key)

    async def build_embed(self, tab: str) -> discord.Embed:
        tab = (tab or "today").lower().strip()
        if tab not in {"today", "week", "by_game", "all_time"}:
            tab = "today"

        dutch = self._is_dutch()
        combined = self._is_combined()
        guild_id = self._guild_id()

        if tab == "all_time":
            rows = await self._leaderboard_repo.get_global_leaderboard(
                limit=self._config.limit, guild_id=guild_id
            )
            return self._embed_all_time(rows, dutch=dutch, combined=combined)

        if tab == "by_game":
            boards = await self._leaderboard_repo.get_per_game_earned_leaderboards(
                game_keys=self._config.english_game_keys,
                limit_per_game=3,
                guild_id=guild_id,
            )
            return self._embed_by_game(boards, dutch=dutch)

        if tab == "week":
            since = self._fmt_sqlite(self._utc_start_of_week_monday())
            rows = await self._leaderboard_repo.get_english_earned_since(
                since_ts_utc=since,
                english_game_keys=self._config.english_game_keys,
                limit=self._config.limit,
                guild_id=guild_id,
            )
            title = self._period_title(period="week", dutch=dutch, combined=combined)
            return self._embed_period(
                title=title,
                since_label=since,
                rows=rows,
                dutch=dutch,
                combined=combined,
            )

        since = self._fmt_sqlite(self._utc_start_of_today())
        rows = await self._leaderboard_repo.get_english_earned_since(
            since_ts_utc=since,
            english_game_keys=self._config.english_game_keys,
            limit=self._config.limit,
            guild_id=guild_id,
        )
        title = self._period_title(period="today", dutch=dutch, combined=combined)
        return self._embed_period(
            title=title,
            since_label=since,
            rows=rows,
            dutch=dutch,
            combined=combined,
        )

    @staticmethod
    def _period_title(*, period: str, dutch: bool, combined: bool) -> str:
        if combined:
            return "Games - This Week" if period == "week" else "Games - Today"
        if dutch:
            return "Nederlandse Spellen - Deze Week" if period == "week" else "Nederlandse Spellen - Vandaag"
        return "English Games - This Week" if period == "week" else "English Games - Today"

    def _embed_period(
        self,
        *,
        title: str,
        since_label: str,
        rows: list[dict[str, Any]],
        dutch: bool = False,
        combined: bool = False,
    ) -> discord.Embed:
        if combined:
            desc = f"Beans earned across all Discord games since **{since_label} UTC**"
        else:
            desc = (
                f"Bonen verdiend in spellen sinds **{since_label} UTC**"
                if dutch else
                f"Beans earned in English games since **{since_label} UTC**"
            )
        embed = discord.Embed(title=title, description=desc)

        if not rows:
            value = self._empty_period_text(dutch=dutch, combined=combined)
            embed.add_field(
                name="Top spelers" if dutch else "Top players",
                value=value,
                inline=False,
            )
            return embed

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            discord_user_id = str(row["discord_user_id"])
            total_beans = int(row["total_beans"])
            unit = "bonen" if dutch else "beans"
            lines.append(f"**{i}.** <@{discord_user_id}> - **{total_beans}** {unit}")

        embed.add_field(
            name="Top spelers" if dutch else "Top players",
            value="\n".join(lines),
            inline=False,
        )
        embed.set_footer(
            text="Gebruik het menu om van tab te wisselen." if dutch else "Use the dropdown to switch tabs."
        )
        return embed

    @staticmethod
    def _empty_period_text(*, dutch: bool, combined: bool) -> str:
        if combined:
            return "Play any game to appear here!"
        return "Speel een spel om hier te verschijnen." if dutch else "Play an English game to appear here!"

    def _game_label(self, game_key: str) -> str:
        return self._config.game_labels.get(game_key) or game_key.replace("_", " ").title()

    def _embed_by_game(self, boards: dict[str, list[dict[str, Any]]], dutch: bool = False) -> discord.Embed:
        title = "Game scores per spel" if dutch else "Game Scores by Game"
        desc = "Alltime bonen verdiend per spel." if dutch else "All-time beans earned per game."
        embed = discord.Embed(title=title, description=desc)

        any_rows = False
        for game_key in self._config.english_game_keys:
            rows = boards.get(game_key, [])
            if not rows:
                embed.add_field(
                    name=self._game_label(game_key),
                    value="Nog geen scores." if dutch else "No scores yet.",
                    inline=False,
                )
                continue

            any_rows = True
            unit = "bonen" if dutch else "beans"
            lines = [
                f"**{i}.** <@{r['discord_user_id']}> - **{int(r['total_beans'])}** {unit}"
                for i, r in enumerate(rows, start=1)
            ]
            embed.add_field(name=self._game_label(game_key), value="\n".join(lines), inline=False)

        if not any_rows:
            embed.description = "Speel een spel om hier te verschijnen." if dutch else "Play a game to appear here."

        embed.set_footer(
            text="Gerangschikt op bonen per spel." if dutch else "Ranked by beans earned in each game."
        )
        return embed

    def _embed_all_time(
        self, rows: list[Any], dutch: bool = False, combined: bool = False
    ) -> discord.Embed:
        if combined:
            title = "Game Beans - All Time"
            desc = "Ranked by current bean balance across game servers."
        else:
            title = "Totale Bonen - Alltime" if dutch else "Global Beans - All Time"
            desc = "Gerangschikt op huidige bonensaldo." if dutch else "Ranked by current bean balance."
        embed = discord.Embed(title=title, description=desc)

        if not rows:
            embed.add_field(
                name="Top spelers" if dutch else "Top players",
                value="Nog geen data." if dutch else "No data yet.",
                inline=False,
            )
            return embed

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            name = row.display_name
            if isinstance(name, str) and name.isdigit():
                name = f"<@{name}>"
            unit = "bonen" if dutch else "beans"
            lines.append(f"**{i}.** {name} - **{row.balance}** {unit}")

        embed.add_field(
            name="Top spelers" if dutch else "Top players",
            value="\n".join(lines),
            inline=False,
        )
        embed.set_footer(
            text="Gebruik het menu om van tab te wisselen." if dutch else "Use the dropdown to switch tabs."
        )
        return embed

    async def _upsert_single_message(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> Optional[int]:
        platform = self._config.platform
        channel_id_str = str(self._config.channel_id)
        board_key = self._config.board_key

        msg_id = await self._posts_repo.get_message_id(
            platform=platform,
            channel_id=channel_id_str,
            board_key=board_key,
        )

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed, view=view)
                return msg.id
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Leaderboard message could not be edited; recreating. board_key=%s", board_key)

        sent = await channel.send(embed=embed, view=view)
        await self._posts_repo.upsert_message_id(
            platform=platform,
            channel_id=channel_id_str,
            board_key=board_key,
            message_id=sent.id,
        )
        return sent.id


class LeaderboardDropdown(discord.ui.Select):
    def __init__(self, publisher: LeaderboardPublisher) -> None:
        self._publisher = publisher
        dutch = publisher._is_dutch()
        combined = publisher._is_combined()

        if dutch:
            options = [
                discord.SelectOption(label="Per spel", value="by_game", description="Top spelers per spel"),
                discord.SelectOption(label="Vandaag", value="today", description="Spellen vandaag"),
                discord.SelectOption(label="Deze Week", value="week", description="Spellen deze week"),
                discord.SelectOption(label="Alltime", value="all_time", description="Totale bonen"),
            ]
            custom_id = "leaderboard:dropdown:nl:v1"
            placeholder = "Bekijk scorebord..."
        else:
            today_desc = "All games today" if combined else "English games today"
            week_desc = "All games this week" if combined else "English games this week"
            options = [
                discord.SelectOption(label="By Game", value="by_game", description="Top players per game"),
                discord.SelectOption(label="Today", value="today", description=today_desc),
                discord.SelectOption(label="This Week", value="week", description=week_desc),
                discord.SelectOption(label="All Time", value="all_time", description="Current bean balance"),
            ]
            custom_id = "leaderboard:dropdown:games:v1" if combined else "leaderboard:dropdown:v1"
            placeholder = "View leaderboard..."

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        tab = self.values[0]
        await interaction.response.defer()
        embed = await self._publisher.build_embed(tab=tab)
        try:
            await interaction.message.edit(embed=embed, view=self.view)  # type: ignore[union-attr]
        except discord.HTTPException:
            pass


class LeaderboardDropdownView(discord.ui.View):
    def __init__(self, publisher: LeaderboardPublisher) -> None:
        super().__init__(timeout=None)
        self.add_item(LeaderboardDropdown(publisher))

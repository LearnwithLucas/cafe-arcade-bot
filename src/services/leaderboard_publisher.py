from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord

from src.db.repo.leaderboard_repo import LeaderboardRepository
from src.db.repo.leaderboard_posts_repo import LeaderboardPostsRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaderboardConfig:
    platform: str
    channel_id: int
    english_game_keys: list[str]
    limit: int = 10
    debounce_seconds: float = 10.0
    board_key: str = "english_dropdown_v1"  # must be unique per publisher instance


class LeaderboardPublisher:
    """
    One-message leaderboard with dropdown:
      - Today (default)
      - This Week
      - All Time (global beans balance)

    Uses leaderboard_posts to persist the message ID across restarts.
    board_key must be unique per channel so Dutch and English don't share a message ID.
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

    def set_bot(self, bot: discord.Client) -> None:
        self._bot = bot
        logger.info(
            "LeaderboardPublisher attached to bot. channel_id=%s board_key=%s",
            self._config.channel_id,
            self._config.board_key,
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

    def _is_dutch(self) -> bool:
        return self._config.board_key == "dutch_dropdown_v1"

    async def refresh_now(self, default_tab: str = "today") -> None:
        async with self._lock:
            if not self._bot:
                logger.warning("LeaderboardPublisher.refresh_now called but bot is not set. board_key=%s", self._config.board_key)
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
                logger.info("Leaderboard message updated (message_id=%s board_key=%s)", msg_id, self._config.board_key)
            else:
                logger.warning("Leaderboard message update failed (no message_id returned) board_key=%s", self._config.board_key)

    async def build_embed(self, tab: str) -> discord.Embed:
        tab = (tab or "today").lower().strip()
        if tab not in {"today", "week", "all_time"}:
            tab = "today"

        dutch = self._is_dutch()

        if tab == "all_time":
            rows = await self._leaderboard_repo.get_global_leaderboard(limit=self._config.limit)
            return self._embed_all_time(rows, dutch=dutch)

        if tab == "week":
            since = self._fmt_sqlite(self._utc_start_of_week_monday())
            rows = await self._leaderboard_repo.get_english_earned_since(
                since_ts_utc=since,
                english_game_keys=self._config.english_game_keys,
                limit=self._config.limit,
            )
            title = "🗓️ Nederlandse Spellen — Deze Week" if dutch else "🗓️ English Games — This Week"
            return self._embed_period(title=title, since_label=since, rows=rows, dutch=dutch)

        # today default
        since = self._fmt_sqlite(self._utc_start_of_today())
        rows = await self._leaderboard_repo.get_english_earned_since(
            since_ts_utc=since,
            english_game_keys=self._config.english_game_keys,
            limit=self._config.limit,
        )
        title = "📅 Nederlandse Spellen — Vandaag" if dutch else "📅 English Games — Today"
        return self._embed_period(title=title, since_label=since, rows=rows, dutch=dutch)

    def _embed_period(
        self, *, title: str, since_label: str, rows: list[dict[str, Any]], dutch: bool = False
    ) -> discord.Embed:
        desc = (
            f"Bonen verdiend in spellen sinds **{since_label} UTC**"
            if dutch else
            f"Beans earned in English games since **{since_label} UTC**"
        )
        embed = discord.Embed(title=title, description=desc)

        if not rows:
            value = "Speel een spel om hier te verschijnen! 🎮" if dutch else "Play an English game to appear here!"
            embed.add_field(name="Top spelers" if dutch else "Top players", value=value, inline=False)
            return embed

        lines: list[str] = []
        for i, r in enumerate(rows, start=1):
            discord_user_id = str(r["discord_user_id"])
            total_beans = int(r["total_beans"])
            unit = "bonen" if dutch else "beans"
            lines.append(f"**{i}.** <@{discord_user_id}> — **{total_beans}** {unit}")

        embed.add_field(name="Top spelers" if dutch else "Top players", value="\n".join(lines), inline=False)
        embed.set_footer(text="Gebruik het menu om van periode te wisselen." if dutch else "Use the dropdown to switch period.")
        return embed

    def _embed_all_time(self, rows: list[Any], dutch: bool = False) -> discord.Embed:
        title = "🏆 Totale Bonen — Alltime" if dutch else "🏆 Global Beans — All Time"
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
            lines.append(f"**{i}.** {name} — **{row.balance}** {unit}")

        embed.add_field(name="Top spelers" if dutch else "Top players", value="\n".join(lines), inline=False)
        embed.set_footer(text="Gebruik het menu om van periode te wisselen." if dutch else "Use the dropdown to switch period.")
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

        if dutch:
            options = [
                discord.SelectOption(label="Vandaag", value="today", emoji="📅", description="Spellen — vandaag"),
                discord.SelectOption(label="Deze Week", value="week", emoji="🗓️", description="Spellen — deze week"),
                discord.SelectOption(label="Alltime", value="all_time", emoji="🏆", description="Totale bonen"),
            ]
            custom_id = "leaderboard:dropdown:nl:v1"
            placeholder = "Bekijk scorebord…"
        else:
            options = [
                discord.SelectOption(label="Today", value="today", emoji="📅", description="English games — today"),
                discord.SelectOption(label="This Week", value="week", emoji="🗓️", description="English games — this week"),
                discord.SelectOption(label="All Time", value="all_time", emoji="🏆", description="Global bean balance"),
            ]
            custom_id = "leaderboard:dropdown:v1"
            placeholder = "View leaderboard…"

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
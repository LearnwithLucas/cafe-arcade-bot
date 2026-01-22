from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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


class LeaderboardPublisher:
    """
    One-message leaderboard with dropdown:
      - Today (default)
      - This Week
      - All Time (global beans balance)

    Uses leaderboard_posts to persist the message ID across restarts.
    """

    BOARD_KEY = "english_dropdown_v1"

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

        # Debounce state
        self._pending_task: Optional[asyncio.Task] = None
        self._scheduled_at: Optional[float] = None  # loop.time()

    def set_bot(self, bot: discord.Client) -> None:
        self._bot = bot
        logger.info("LeaderboardPublisher attached to bot. Target channel_id=%s", self._config.channel_id)

        # Register persistent dropdown view so it stays interactive after restarts
        try:
            bot.add_view(self.build_persistent_view())
        except Exception:
            # add_view can only be called once per identical persistent view in some setups;
            # swallow safely but log.
            logger.debug("LeaderboardPublisher: add_view failed (likely already registered)", exc_info=True)

    def build_persistent_view(self) -> discord.ui.View:
        return LeaderboardDropdownView(publisher=self)

    def schedule_refresh(self) -> None:
        """
        Debounced refresh: schedule an update to occur debounce_seconds after the LAST change.
        If called repeatedly, it cancels the previous task and reschedules.
        """
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

        # Cancel previous pending refresh and reschedule
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

        delay = float(self._config.debounce_seconds)
        self._scheduled_at = loop.time() + delay
        logger.info("Leaderboard refresh scheduled in %.1fs", delay)

        async def runner() -> None:
            try:
                # Sleep until scheduled time (supports reschedule by cancel/recreate)
                await asyncio.sleep(delay)
                await self.refresh_now(default_tab="today")
            except asyncio.CancelledError:
                # Normal: a newer change re-scheduled the refresh
                return
            except Exception:
                logger.exception("LeaderboardPublisher debounced refresh crashed")

        # Create task on bot loop
        try:
            self._pending_task = loop.create_task(runner())
        except Exception:
            # Fallback
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
                logger.warning("LeaderboardPublisher.refresh_now called but bot is not set.")
                return

            # Fetch channel
            channel = self._bot.get_channel(self._config.channel_id)
            if channel is None:
                try:
                    channel = await self._bot.fetch_channel(self._config.channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception("Failed to fetch leaderboard channel")
                    return

            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                logger.warning("Target leaderboard channel is not a text channel/thread: %r", channel)
                return

            embed = await self.build_embed(tab=default_tab)
            view = self.build_persistent_view()

            msg_id = await self._upsert_single_message(channel=channel, embed=embed, view=view)
            if msg_id:
                logger.info("Leaderboard message updated (message_id=%s)", msg_id)
            else:
                logger.warning("Leaderboard message update failed (no message_id returned)")

    async def build_embed(self, tab: str) -> discord.Embed:
        tab = (tab or "today").lower().strip()
        if tab not in {"today", "week", "all_time"}:
            tab = "today"

        if tab == "all_time":
            rows = await self._leaderboard_repo.get_global_leaderboard(limit=self._config.limit)
            return self._embed_all_time(rows)

        if tab == "week":
            since = self._fmt_sqlite(self._utc_start_of_week_monday())
            rows = await self._leaderboard_repo.get_english_earned_since(
                since_ts_utc=since,
                english_game_keys=self._config.english_game_keys,
                limit=self._config.limit,
            )
            return self._embed_period(title="🗓️ English Games — This Week", since_label=since, rows=rows)

        # today default
        since = self._fmt_sqlite(self._utc_start_of_today())
        rows = await self._leaderboard_repo.get_english_earned_since(
            since_ts_utc=since,
            english_game_keys=self._config.english_game_keys,
            limit=self._config.limit,
        )
        return self._embed_period(title="📅 English Games — Today", since_label=since, rows=rows)

    def _embed_period(self, *, title: str, since_label: str, rows: list[dict[str, Any]]) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"Beans earned in English games since **{since_label} UTC**",
        )
        if not rows:
            embed.add_field(name="Top players", value="Play an English game to appear here!", inline=False)
            return embed

        lines: list[str] = []
        for i, r in enumerate(rows, start=1):
            discord_user_id = str(r["discord_user_id"])
            total_beans = int(r["total_beans"])
            lines.append(f"**{i}.** <@{discord_user_id}> — **{total_beans}** beans")

        embed.add_field(name="Top players", value="\n".join(lines), inline=False)
        embed.set_footer(text="Use the dropdown to switch period.")
        return embed

    def _embed_all_time(self, rows: list[Any]) -> discord.Embed:
        embed = discord.Embed(
            title="🏆 Global Beans — All Time",
            description="Ranked by current bean balance.",
        )
        if not rows:
            embed.add_field(name="Top players", value="No data yet.", inline=False)
            return embed

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            name = row.display_name
            if isinstance(name, str) and name.isdigit():
                name = f"<@{name}>"
            lines.append(f"**{i}.** {name} — **{row.balance}** beans")

        embed.add_field(name="Top players", value="\n".join(lines), inline=False)
        embed.set_footer(text="Use the dropdown to switch period.")
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

        msg_id = await self._posts_repo.get_message_id(
            platform=platform,
            channel_id=channel_id_str,
            board_key=self.BOARD_KEY,
        )

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed, view=view)
                return msg.id
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Leaderboard message exists but could not be edited; will recreate.")
                # fall through to recreate

        sent = await channel.send(embed=embed, view=view)
        await self._posts_repo.upsert_message_id(
            platform=platform,
            channel_id=channel_id_str,
            board_key=self.BOARD_KEY,
            message_id=sent.id,
        )
        return sent.id


class LeaderboardDropdown(discord.ui.Select):
    def __init__(self, publisher: LeaderboardPublisher) -> None:
        self._publisher = publisher
        options = [
            discord.SelectOption(label="Today", value="today", emoji="📅", description="English games — today"),
            discord.SelectOption(label="This Week", value="week", emoji="🗓️", description="English games — this week"),
            discord.SelectOption(label="All Time", value="all_time", emoji="🏆", description="Global bean balance"),
        ]
        super().__init__(
            placeholder="View leaderboard…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="leaderboard:dropdown:v1",
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

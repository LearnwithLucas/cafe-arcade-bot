from __future__ import annotations

import logging
from typing import Any

import discord

from src.config.settings import Settings
from src.platforms.discord.commands import setup as setup_commands
from src.platforms.discord.events import setup as setup_events
from src.services.hub_service import HubPublisher, StartHereView, BeginHierView

logger = logging.getLogger(__name__)


class ArcadeDiscordBot(discord.Client):
    def __init__(self, *, settings: Settings, services: dict[str, Any]) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self.services = services
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await setup_commands(self)
        await setup_events(self)

        # Register persistent views so buttons survive restarts
        self.add_view(StartHereView())
        self.add_view(BeginHierView())

        # Attach English leaderboard publisher
        publisher = self.services.get("leaderboard_publisher")
        if publisher:
            try:
                publisher.set_bot(self)
            except Exception:
                logger.exception("Failed to attach bot to English leaderboard publisher")

        # Attach Dutch leaderboard publisher
        dutch_publisher = self.services.get("dutch_leaderboard_publisher")
        if dutch_publisher:
            try:
                dutch_publisher.set_bot(self)
            except Exception:
                logger.exception("Failed to attach bot to Dutch leaderboard publisher")

        top = self.tree.get_commands()
        logger.info(
            "Tree contains %s top-level commands: %s",
            len(top),
            "|".join(c.name for c in top) if top else "(none)",
        )

        # Sync to English guild
        guild_id = self.settings.discord_guild_id
        try:
            if guild_id:
                gid = int(guild_id)
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s app commands to English guild %s", len(synced), gid)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %s app commands globally", len(synced))
        except Exception:
            logger.exception("Command sync failed for English guild")

        # Sync to Dutch guild
        dutch_guild_id = self.settings.dutch_guild_id
        if dutch_guild_id:
            try:
                dutch_guild = discord.Object(id=int(dutch_guild_id))
                self.tree.copy_global_to(guild=dutch_guild)
                synced_nl = await self.tree.sync(guild=dutch_guild)
                logger.info("Synced %s app commands to Dutch guild %s", len(synced_nl), dutch_guild_id)
            except Exception:
                logger.exception("Command sync failed for Dutch guild")

    async def on_ready(self) -> None:
        logger.info("Discord bot ready: %s (id=%s)", self.user, self.user.id if self.user else "?")

        # Post/update hub messages in both servers
        hub = HubPublisher(bot=self)
        try:
            await hub.publish_english()
        except Exception:
            logger.exception("Hub: failed to publish English hub")
        try:
            await hub.publish_dutch()
        except Exception:
            logger.exception("Hub: failed to publish Dutch hub")


def build_discord_bot(*, settings: Settings, services: dict[str, Any]) -> ArcadeDiscordBot:
    return ArcadeDiscordBot(settings=settings, services=services)
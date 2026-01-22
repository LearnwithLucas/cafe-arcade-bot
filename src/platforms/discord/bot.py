from __future__ import annotations

import logging
from typing import Any

import discord

from src.config.settings import Settings
from src.platforms.discord.commands import setup as setup_commands
from src.platforms.discord.events import setup as setup_events

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

        # Attach bot to leaderboard publisher + register persistent dropdown view
        publisher = self.services.get("leaderboard_publisher")
        if publisher:
            try:
                publisher.set_bot(self)
                self.add_view(publisher.build_persistent_view())
            except Exception:
                logger.exception("Failed to attach bot to leaderboard publisher / add view")

        top = self.tree.get_commands()
        logger.info(
            "Tree contains %s top-level commands: %s",
            len(top),
            "|".join(c.name for c in top) if top else "(none)",
        )

        guild_id = self.settings.discord_guild_id
        try:
            if guild_id:
                gid = int(guild_id)
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s app commands to guild %s", len(synced), gid)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %s app commands globally", len(synced))
        except Exception:
            logger.exception("Command sync failed")

    async def on_ready(self) -> None:
        logger.info("Discord bot ready: %s (id=%s)", self.user, self.user.id if self.user else "?")


def build_discord_bot(*, settings: Settings, services: dict[str, Any]) -> ArcadeDiscordBot:
    return ArcadeDiscordBot(settings=settings, services=services)

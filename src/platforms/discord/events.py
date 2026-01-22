from __future__ import annotations

import logging
from typing import Any

import discord

logger = logging.getLogger(__name__)


async def setup(bot: discord.Client) -> None:
    services: dict[str, Any] = getattr(bot, "services", {})

    @bot.event
    async def on_message(message: discord.Message) -> None:
        # Ignore bots (including ourselves)
        if message.author.bot:
            return

        # Ignore DMs
        if message.guild is None:
            return

        # discord.Client doesn't normally have process_commands (that's commands.Bot),
        # but keep this safe hook in case you ever swap implementations.
        if hasattr(bot, "process_commands"):
            try:
                await bot.process_commands(message)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("process_commands failed")

        logger.debug(
            "on_message: channel=%s author=%s content=%r",
            getattr(message.channel, "name", "?"),
            message.author.id,
            message.content,
        )

        # -----------------------------
        # GeoGuessr (chat-consuming games)
        # Prefer explicit calls so we can short-circuit (only one game should consume a message).
        # -----------------------------
        try:
            geo_flags = services.get("geo_flags")
            if geo_flags and hasattr(geo_flags, "handle_discord_message"):
                consumed = await geo_flags.handle_discord_message(message)
                if consumed:
                    return
        except Exception:
            logger.exception("Error in geo_flags.handle_discord_message")

        try:
            geo_language = services.get("geo_language")
            if geo_language and hasattr(geo_language, "handle_discord_message"):
                consumed = await geo_language.handle_discord_message(message)
                if consumed:
                    return
        except Exception:
            logger.exception("Error in geo_language.handle_discord_message")

        # -----------------------------
        # Everything else via registry
        # (Wordle/WordChain/Unscramble/etc)
        # -----------------------------
        registry = services.get("game_registry")
        if registry:
            try:
                await registry.handle_discord_message(message)
            except Exception:
                logger.exception("Error in game_registry.handle_discord_message")

    @bot.event
    async def on_error(event_method: str, /, *args: Any, **kwargs: Any) -> None:
        logger.exception("Unhandled exception in Discord event: %s", event_method)

    logger.info("Discord events registered (on_message, on_error)")

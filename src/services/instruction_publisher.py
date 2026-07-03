from __future__ import annotations

import logging
from collections.abc import Iterable

import discord
from telegram import Bot
from telegram.error import TelegramError

from src.config.channels import (
    DISCORD_GAME_INSTRUCTIONS,
    DiscordInstruction,
    build_telegram_instruction_text,
)

logger = logging.getLogger(__name__)


class DiscordInstructionPublisher:
    def __init__(
        self,
        *,
        bot: discord.Client,
        instructions: Iterable[DiscordInstruction] = DISCORD_GAME_INSTRUCTIONS,
    ) -> None:
        self._bot = bot
        self._instructions = tuple(instructions)

    async def publish_all(self) -> None:
        for instruction in self._instructions:
            await self._publish_one(instruction)

    async def _publish_one(self, instruction: DiscordInstruction) -> None:
        channel = self._bot.get_channel(instruction.channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(instruction.channel_id)
            except Exception:
                logger.warning(
                    "Instructions: could not fetch Discord channel %s",
                    instruction.channel_id,
                )
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning(
                "Instructions: channel %s does not support instruction posts",
                instruction.channel_id,
            )
            return

        embed = self._build_embed(instruction)
        existing = await self._find_existing(channel=channel, marker=instruction.marker)

        if existing:
            try:
                await existing.edit(embed=embed)
                logger.info(
                    "Instructions: updated Discord instructions in channel %s",
                    instruction.channel_id,
                )
                await self._pin(channel=channel, message=existing)
                return
            except Exception:
                logger.warning(
                    "Instructions: could not edit message in channel %s; recreating",
                    instruction.channel_id,
                )

        try:
            message = await channel.send(embed=embed)
            logger.info(
                "Instructions: posted Discord instructions in channel %s",
                instruction.channel_id,
            )
            await self._pin(channel=channel, message=message)
        except Exception:
            logger.exception(
                "Instructions: failed to post Discord instructions in channel %s",
                instruction.channel_id,
            )

    def _build_embed(self, instruction: DiscordInstruction) -> discord.Embed:
        lines = [instruction.body, "", "**Commands**"]
        lines.extend(f"`{command}`" for command in instruction.commands)
        if instruction.notes:
            lines.extend(("", "**Notes**"))
            lines.extend(instruction.notes)

        embed = discord.Embed(title=instruction.title, description="\n".join(lines))
        embed.set_footer(text=instruction.marker)
        return embed

    async def _find_existing(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        marker: str,
    ) -> discord.Message | None:
        try:
            async for message in channel.history(limit=50):
                if self._bot.user and message.author.id != self._bot.user.id:
                    continue
                for embed in message.embeds:
                    if embed.footer and marker in (embed.footer.text or ""):
                        return message
        except discord.Forbidden:
            logger.warning(
                "Instructions: missing Read Message History permission in channel %s",
                channel.id,
            )
        except Exception:
            logger.exception(
                "Instructions: failed to inspect message history in channel %s",
                channel.id,
            )
        return None

    async def _pin(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        message: discord.Message,
    ) -> None:
        try:
            pins = await channel.pins()
            if any(pin.id == message.id for pin in pins):
                return
            await message.pin()
            logger.info("Instructions: pinned message %s in channel %s", message.id, channel.id)
        except discord.Forbidden:
            logger.warning(
                "Instructions: missing Manage Messages permission in channel %s",
                channel.id,
            )
        except Exception:
            logger.exception("Instructions: failed to pin message in channel %s", channel.id)


class TelegramInstructionPublisher:
    def __init__(self, *, bot: Bot, chat_ids: Iterable[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(dict.fromkeys(chat_ids))

    async def publish_all(self) -> None:
        if not self._chat_ids:
            logger.info("Instructions: no Telegram instruction chats configured")
            return

        me = await self._bot.get_me()
        text = build_telegram_instruction_text(me.username)
        marker = "instructions:telegram:main:v1"

        for chat_id in self._chat_ids:
            await self._publish_one(chat_id=chat_id, text=text, marker=marker)

    async def _publish_one(self, *, chat_id: int, text: str, marker: str) -> None:
        try:
            chat = await self._bot.get_chat(chat_id)
            pinned = chat.pinned_message
            if pinned and pinned.text and marker in pinned.text:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pinned.message_id,
                    text=text,
                )
                logger.info("Instructions: updated Telegram instructions in chat %s", chat_id)
                return

            message = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_notification=True,
            )
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
            logger.info("Instructions: posted and pinned Telegram instructions in chat %s", chat_id)
        except TelegramError:
            logger.exception("Instructions: failed to post Telegram instructions in chat %s", chat_id)

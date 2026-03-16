from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


# ---- English hub ----

EN_HUB_CHANNEL_ID = 1482998106898563092  # #start-here


class StartHereView(discord.ui.View):
    """Persistent 'Start playing' button for the English hub."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start playing",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="hub:start_playing:en:v1",
    )
    async def start_playing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "**Here's how to start each game:**\n\n"
            "🧩 **Wordle** → go to <#1481745735652474920> and type `/games wordle_start`\n"
            "🔀 **Unscramble** → go to <#1481745817021845607> and type `/games unscramble_start`\n"
            "⛓️ **Word Chain** → go to <#1481745881123520573> and type `/games wordchain_start`\n\n"
            "💰 Earn beans by playing — check `/cafe beans` for your balance.\n"
            "🏆 See the leaderboard in <#1481746468737126564>.",
            ephemeral=True,
        )


def build_en_hub_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎮 Welcome to Practice and Play",
        description=(
            "Play word games, earn beans ☕, and climb the leaderboard.\n"
            "All games are free — no sign-up needed.\n\n"
            "**Games**\n"
            "🧩 **Wordle** | <#1481745735652474920>\n"
            "Guess the 5-letter word in 12 tries.\n"
            "Start: `/games wordle_start`\n\n"
            "🔀 **Unscramble** | <#1481745817021845607>\n"
            "Unscramble the word before your 3 guesses run out.\n"
            "Start: `/games unscramble_start`\n\n"
            "⛓️ **Word Chain** | <#1481745881123520573>\n"
            "Build a chain — each word starts with the last letter of the previous one.\n"
            "Start: `/games wordchain_start`\n\n"
            "**Economy**\n"
            "☕ Earn beans by playing and spending them in `/shop`.\n"
            "Claim daily beans with `/cafe daily` in <#1481764812248842280>.\n\n"
            "🏆 Leaderboard: <#1481746468737126564>"
        ),
    )
    embed.set_footer(text="Press the button below to get started.")
    return embed


# ---- Dutch hub ----

NL_HUB_CHANNEL_ID = 1482998054121377903  # #begin-hier


class BeginHierView(discord.ui.View):
    """Persistent 'Begin hier' button for the Dutch hub."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Begin met spelen",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="hub:start_playing:nl:v1",
    )
    async def begin_spelen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "**Zo start je elk spel:**\n\n"
            "🧩 **Woordle** → ga naar <#1482763022173995119> en typ `/games wordle_nl_start`\n"
            "🔀 **Ontwar het Woord** → ga naar <#1482763069238153419> en typ `/games ontwar_start`\n"
            "⛓️ **Woordketting** → ga naar <#1482763114842816765> en typ `/games woordketting_start`\n\n"
            "💰 Verdien bonen door te spelen — check `/cafe bonen` voor je saldo.\n"
            "🏆 Bekijk het scorebord in <#1433723125056667759>.",
            ephemeral=True,
        )


def build_nl_hub_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎮 Welkom bij Oefenen en Spelen",
        description=(
            "Speel woordspellen, verdien bonen ☕ en klim op het scorebord.\n"
            "Alle spellen zijn gratis — geen aanmelding nodig.\n\n"
            "**Spellen**\n"
            "🧩 **Woordle** | <#1482763022173995119>\n"
            "Raad het 5-letter woord in 12 pogingen.\n"
            "Start: `/games wordle_nl_start`\n\n"
            "🔀 **Ontwar het Woord** | <#1482763069238153419>\n"
            "Ontwar het woord voordat je 3 pogingen op zijn.\n"
            "Start: `/games ontwar_start`\n\n"
            "⛓️ **Woordketting** | <#1482763114842816765>\n"
            "Bouw een ketting — elk woord begint met de laatste letter van het vorige.\n"
            "Start: `/games woordketting_start`\n\n"
            "**Economie**\n"
            "☕ Verdien bonen door te spelen en geef ze uit in `/winkel`.\n"
            "Claim dagelijkse bonen met `/cafe dagelijks` in <#1482763329293520967>.\n\n"
            "🏆 Scorebord: <#1433723125056667759>"
        ),
    )
    embed.set_footer(text="Druk op de knop hieronder om te beginnen.")
    return embed


# ---- Publisher ----

class HubPublisher:
    """
    Posts and pins the hub message in #start-here (EN) and #begin-hier (NL).
    - On startup: posts or edits the existing hub message, pins it, unpins old messages.
    - On manual refresh: same behaviour.
    """

    def __init__(self, *, bot: discord.Client) -> None:
        self._bot = bot

    async def publish_english(self) -> None:
        await self._publish(
            channel_id=EN_HUB_CHANNEL_ID,
            embed=build_en_hub_embed(),
            view=StartHereView(),
            marker="hub:en:v1",
        )

    async def publish_dutch(self) -> None:
        await self._publish(
            channel_id=NL_HUB_CHANNEL_ID,
            embed=build_nl_hub_embed(),
            view=BeginHierView(),
            marker="hub:nl:v1",
        )

    async def _publish(
        self,
        *,
        channel_id: int,
        embed: discord.Embed,
        view: discord.ui.View,
        marker: str,
    ) -> None:
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception:
                logger.warning("Hub: could not fetch channel %s", channel_id)
                return

        if not isinstance(channel, discord.TextChannel):
            logger.warning("Hub: channel %s is not a TextChannel", channel_id)
            return

        # Find existing hub message (bot-authored, contains marker in footer)
        existing: Optional[discord.Message] = None
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id == self._bot.user.id:  # type: ignore[union-attr]
                    if msg.embeds and msg.embeds[0].footer and marker in (msg.embeds[0].footer.text or ""):
                        existing = msg
                        break
        except Exception:
            logger.warning("Hub: could not read history for channel %s", channel_id)

        # Add marker to embed footer
        embed.set_footer(text=f"{embed.footer.text or ''} | {marker}")

        if existing:
            try:
                await existing.edit(embed=embed, view=view)
                logger.info("Hub: updated existing message in channel %s", channel_id)
            except Exception:
                logger.warning("Hub: could not edit existing message, will recreate")
                existing = None

        if not existing:
            try:
                new_msg = await channel.send(embed=embed, view=view)
                logger.info("Hub: posted new message in channel %s", channel_id)
                await self._pin_and_clean(channel=channel, keep_msg=new_msg)
            except Exception:
                logger.exception("Hub: failed to post message in channel %s", channel_id)
                return
        else:
            # Ensure it's pinned even if we just edited
            await self._pin_and_clean(channel=channel, keep_msg=existing)

    async def _pin_and_clean(
        self,
        *,
        channel: discord.TextChannel,
        keep_msg: discord.Message,
    ) -> None:
        try:
            pins = await channel.pins()

            # Unpin all bot-authored pins that aren't the current hub message
            for pin in pins:
                if pin.id != keep_msg.id and pin.author.id == self._bot.user.id:  # type: ignore[union-attr]
                    try:
                        await pin.unpin()
                        logger.info("Hub: unpinned old message %s in channel %s", pin.id, channel.id)
                    except Exception:
                        logger.warning("Hub: could not unpin message %s", pin.id)

            # Pin the new message if not already pinned
            if not any(p.id == keep_msg.id for p in pins):
                await keep_msg.pin()
                logger.info("Hub: pinned message %s in channel %s", keep_msg.id, channel.id)

        except discord.Forbidden:
            logger.warning("Hub: missing Manage Messages permission in channel %s", channel.id)
        except Exception:
            logger.exception("Hub: pin/unpin failed in channel %s", channel.id)

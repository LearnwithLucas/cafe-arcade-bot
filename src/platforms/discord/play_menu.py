from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

from src.config.channels import (
    DUTCH_BIJVOEGLIJK_CHANNEL_ID,
    DUTCH_DE_OF_HET_CHANNEL_ID,
    DUTCH_NIET_GEEN_CHANNEL_ID,
    DUTCH_UNSCRAMBLE_CHANNEL_ID,
    DUTCH_WORDLE_CHANNEL_ID,
    DUTCH_WORD_CHAIN_CHANNEL_ID,
    GEO_FLAGS_CHANNEL_ID,
    GEO_LANGUAGE_CHANNEL_ID,
    UNSCRAMBLE_CHANNEL_ID,
    WORDLE_CHANNEL_ID,
    WORD_CHAIN_CHANNEL_ID,
)


@dataclass(frozen=True)
class PlayOption:
    key: str
    label: str
    channel_id: int
    command: str
    summary: str
    row: int


EN_OPTIONS: tuple[PlayOption, ...] = (
    PlayOption("wordle", "Wordle", WORDLE_CHANNEL_ID, "/games wordle_start", "Daily 5-letter word puzzle.", 0),
    PlayOption("unscramble", "Unscramble", UNSCRAMBLE_CHANNEL_ID, "/games unscramble_start", "Make the word from shuffled letters.", 0),
    PlayOption("wordchain", "Word Chain", WORD_CHAIN_CHANNEL_ID, "/games wordchain_start", "Build a chain from the last letter.", 0),
    PlayOption("geo_flags", "Geo Flags", GEO_FLAGS_CHANNEL_ID, "/geoguessr flags_start", "Guess the country from a flag.", 1),
    PlayOption("geo_language", "Geo Language", GEO_LANGUAGE_CHANNEL_ID, "/geoguessr language_start", "Guess the language from a clue.", 1),
)

NL_OPTIONS: tuple[PlayOption, ...] = (
    PlayOption("wordle_nl", "Woordle", DUTCH_WORDLE_CHANNEL_ID, "/games wordle_nl_start", "Raad het 5-letter woord.", 0),
    PlayOption("ontwar", "Ontwar", DUTCH_UNSCRAMBLE_CHANNEL_ID, "/games ontwar_start", "Zet de letters in de goede volgorde.", 0),
    PlayOption("woordketting", "Woordketting", DUTCH_WORD_CHAIN_CHANNEL_ID, "/games woordketting_start", "Maak een ketting van woorden.", 0),
    PlayOption("niet_geen", "Niet vs Geen", DUTCH_NIET_GEEN_CHANNEL_ID, "/nietgeen", "Oefen wanneer je niet of geen gebruikt.", 1),
    PlayOption("bijvoeglijk", "Bijvoeglijk", DUTCH_BIJVOEGLIJK_CHANNEL_ID, "/bijvoeglijk_start", "Oefen -e of geen -e.", 1),
    PlayOption("de_of_het", "De of Het", DUTCH_DE_OF_HET_CHANNEL_ID, "/deofhet_start", "Oefen Nederlandse lidwoorden.", 1),
)


def _is_dutch_guild(bot: discord.Client, interaction: discord.Interaction) -> bool:
    dutch_guild_id = getattr(getattr(bot, "settings", None), "dutch_guild_id", None)
    return dutch_guild_id is not None and interaction.guild_id == int(dutch_guild_id)


def _channel_url(guild_id: int | None, channel_id: int) -> str | None:
    if guild_id is None or channel_id <= 0:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def _options_for(is_nl: bool) -> tuple[PlayOption, ...]:
    return NL_OPTIONS if is_nl else EN_OPTIONS


def build_play_embed(*, is_nl: bool) -> discord.Embed:
    if is_nl:
        embed = discord.Embed(
            title="Wat wil je spelen?",
            description="Kies een spel. Als je niet in het juiste kanaal bent, geef ik je rustig de juiste plek.",
        )
        embed.set_footer(text="Alleen jij ziet dit menu.")
        return embed

    embed = discord.Embed(
        title="What do you want to play?",
        description="Choose a game. If you are not in the right channel, I will point you there first.",
    )
    embed.set_footer(text="Only you can see this menu.")
    return embed


def build_wrong_channel_embed(option: PlayOption, *, is_nl: bool) -> discord.Embed:
    if is_nl:
        embed = discord.Embed(
            title=f"{option.label} heeft een eigen kanaal",
            description=(
                f"Ga naar <#{option.channel_id}> en gebruik `{option.command}`.\n\n"
                f"{option.summary}"
            ),
        )
        embed.set_footer(text="Dit voorkomt dat spellen door elkaar lopen.")
        return embed

    embed = discord.Embed(
        title=f"{option.label} has its own channel",
        description=(
            f"Go to <#{option.channel_id}> and use `{option.command}`.\n\n"
            f"{option.summary}"
        ),
    )
    embed.set_footer(text="This keeps games from mixing together.")
    return embed


class GoToGameView(discord.ui.View):
    def __init__(self, *, guild_id: int | None, option: PlayOption, is_nl: bool) -> None:
        super().__init__(timeout=180)
        url = _channel_url(guild_id, option.channel_id)
        if url:
            self.add_item(
                discord.ui.Button(
                    label="Ga naar kanaal" if is_nl else "Go to channel",
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )


class PlayButton(discord.ui.Button):
    def __init__(self, option: PlayOption) -> None:
        super().__init__(
            label=option.label,
            style=discord.ButtonStyle.primary if option.row == 0 else discord.ButtonStyle.secondary,
            custom_id=f"play:{option.key}:v1",
            row=option.row,
        )
        self.option = option

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, PlayMenuView):
            await interaction.response.defer(ephemeral=True)
            return
        await view.start_game(interaction, self.option)


class PlayMenuView(discord.ui.View):
    def __init__(self, *, services: dict[str, Any], is_nl: bool) -> None:
        super().__init__(timeout=180)
        self.services = services
        self.is_nl = is_nl
        for option in _options_for(is_nl):
            self.add_item(PlayButton(option))

    async def start_game(self, interaction: discord.Interaction, option: PlayOption) -> None:
        if interaction.channel_id != option.channel_id:
            await interaction.response.send_message(
                embed=build_wrong_channel_embed(option, is_nl=self.is_nl),
                view=GoToGameView(guild_id=interaction.guild_id, option=option, is_nl=self.is_nl),
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Gebruik dit in een server tekstkanaal." if self.is_nl else "Use this in a server text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        started = await _start_option(
            interaction=interaction,
            services=self.services,
            option=option,
            is_nl=self.is_nl,
        )
        if started:
            text = "Gestart. Kijk in het kanaal om te spelen." if self.is_nl else "Started. Check the channel to play."
            await interaction.followup.send(text, ephemeral=True)


async def _start_option(
    *,
    interaction: discord.Interaction,
    services: dict[str, Any],
    option: PlayOption,
    is_nl: bool,
) -> bool:
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False

    if option.key == "wordle":
        game = services.get("wordle")
        if not game:
            await interaction.followup.send("Wordle is not available.", ephemeral=True)
            return False
        await game.start_in_channel(channel_id=interaction.channel_id)
        await channel.send(
            embed=discord.Embed(
                title="Wordle - Daily",
                description="5-letter words, 12 guesses. Type your first guess in this channel.",
            )
        )
        return True

    if option.key == "wordle_nl":
        game = services.get("wordle_nl")
        if not game:
            await interaction.followup.send("Woordle is niet beschikbaar.", ephemeral=True)
            return False
        await game.start_in_channel(channel_id=interaction.channel_id)
        await channel.send(
            embed=discord.Embed(
                title="Woordle - Dagelijks",
                description="5-letter woorden, 12 pogingen. Typ je eerste woord in dit kanaal.",
            )
        )
        return True

    if option.key == "unscramble":
        game = services.get("unscramble")
        if not game:
            await interaction.followup.send("Unscramble is not available.", ephemeral=True)
            return False
        await game.start_for_user(channel=channel, user=interaction.user)
        return True

    if option.key == "ontwar":
        game = services.get("unscramble_nl")
        if not game:
            await interaction.followup.send("Ontwar het Woord is niet beschikbaar.", ephemeral=True)
            return False
        await game.start_for_user(channel=channel, user=interaction.user)
        return True

    if option.key == "wordchain":
        game = services.get("word_chain")
        if not game:
            await interaction.followup.send("Word Chain is not available.", ephemeral=True)
            return False
        status_msg = await channel.send(
            embed=discord.Embed(
                title="Word Chain",
                description="Type a word to begin. Each word must start with the previous word's last letter.",
            )
        )
        await game.start_in_channel(channel_id=interaction.channel_id, status_message_id=status_msg.id)
        return True

    if option.key == "woordketting":
        game = services.get("word_chain_nl")
        if not game:
            await interaction.followup.send("Woordketting is niet beschikbaar.", ephemeral=True)
            return False
        status_msg = await channel.send(
            embed=discord.Embed(
                title="Woordketting",
                description="Typ je eerste woord. Elk woord begint met de laatste letter van het vorige woord.",
            )
        )
        await game.start_in_channel(channel_id=interaction.channel_id, status_message_id=status_msg.id)
        return True

    if option.key == "geo_flags":
        game = services.get("geo_flags")
        if not game:
            await interaction.followup.send("Geo Flags is not available.", ephemeral=True)
            return False
        await game.start_for_user(channel=channel, user=interaction.user)
        return True

    if option.key == "geo_language":
        game = services.get("geo_language")
        if not game:
            await interaction.followup.send("Geo Language is not available.", ephemeral=True)
            return False
        await game.start_for_user(channel=channel, user=interaction.user)
        return True

    if option.key == "niet_geen":
        game = services.get("niet_geen")
        if not game:
            await interaction.followup.send("Niet vs Geen is niet beschikbaar.", ephemeral=True)
            return False
        await game.start_game(channel, interaction.user)
        return True

    if option.key == "bijvoeglijk":
        game = services.get("bijvoeglijk_e_quiz")
        if not game:
            await interaction.followup.send("Bijvoeglijk is niet beschikbaar.", ephemeral=True)
            return False
        await game.start(channel)
        return True

    if option.key == "de_of_het":
        game = services.get("de_of_het_quiz")
        if not game:
            await interaction.followup.send("De of Het is niet beschikbaar.", ephemeral=True)
            return False
        await game.start(channel)
        return True

    await interaction.followup.send(
        "Dit spel is nog niet gekoppeld aan /play." if is_nl else "This game is not wired into /play yet.",
        ephemeral=True,
    )
    return False


def register_play_command(bot: discord.Client, services: dict[str, Any]) -> None:
    @bot.tree.command(name="play", description="Choose a game from a simple menu")
    async def cmd_play(interaction: discord.Interaction) -> None:
        is_nl = _is_dutch_guild(bot, interaction)
        await interaction.response.send_message(
            embed=build_play_embed(is_nl=is_nl),
            view=PlayMenuView(services=services, is_nl=is_nl),
            ephemeral=True,
        )

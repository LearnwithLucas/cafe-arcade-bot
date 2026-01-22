from __future__ import annotations

import discord
from discord import app_commands


class GeoGuessrCommands(app_commands.Group):
    """
    Slash commands group:

      /geoguessr flags_start
      /geoguessr flags_stop
      /geoguessr flags_help

      /geoguessr language_start
      /geoguessr language_stop
      /geoguessr language_help

    These are panel-based games (per-user) and consume typed chat messages.
    """

    def __init__(self, *, flags_game, language_game) -> None:
        super().__init__(name="geoguessr", description="Geo mini-games (flags & language)")
        self._flags = flags_game
        self._lang = language_game

    # ----------------------
    # FLAGS
    # ----------------------
    @app_commands.command(name="flags_start", description="Start Geo Flags (creates your personal panel)")
    async def flags_start(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._flags.start_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🏁 Geo Flags started! Check the channel for your panel.", ephemeral=True)

    @app_commands.command(name="flags_stop", description="Stop your current Geo Flags round")
    async def flags_stop(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._flags.stop_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🛑 Geo Flags stopped (your round was cleared).", ephemeral=True)

    @app_commands.command(name="flags_help", description="Show Geo Flags rules")
    async def flags_help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🏁 GeoGuessr — Flags",
            description=(
                "**How it works**\n"
                "• Start with `/geoguessr flags_start`\n"
                "• You’ll get a panel with a flag prompt\n"
                "• Type the country name in the channel\n"
                "• Limited guesses\n\n"
                "✅ Correct = beans + Play Again\n"
                "❌ Fail = Play Again\n"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------
    # LANGUAGE
    # ----------------------
    @app_commands.command(name="language_start", description="Start Geo Language (creates your personal panel)")
    async def language_start(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._lang.start_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🗣️ Geo Language started! Check the channel for your panel.", ephemeral=True)

    @app_commands.command(name="language_stop", description="Stop your current Geo Language round")
    async def language_stop(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._lang.stop_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🛑 Geo Language stopped (your round was cleared).", ephemeral=True)

    @app_commands.command(name="language_help", description="Show Geo Language rules")
    async def language_help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🗣️ GeoGuessr — Language",
            description=(
                "**How it works**\n"
                "• Start with `/geoguessr language_start`\n"
                "• You’ll get a panel with a language prompt\n"
                "• Type the language name in the channel\n"
                "• Limited guesses\n\n"
                "✅ Correct = beans + Play Again\n"
                "❌ Fail = Play Again\n"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

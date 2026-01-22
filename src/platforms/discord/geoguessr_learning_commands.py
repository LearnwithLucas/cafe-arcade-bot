from __future__ import annotations

from typing import Literal, Optional

import discord
from discord import app_commands

from src.games.geoguessr.learning import GeoLearningGame


QuizType = Literal["flags", "scripts"]


def _map_quiz_type(t: QuizType) -> str:
    if t == "flags":
        return "flag"
    if t == "scripts":
        return "script"
    raise ValueError(f"Unknown quiz type: {t}")


class GeoLearningCommands(app_commands.Group):
    """
    Slash commands group:

      /geo-learning start flags|scripts
      /geo-learning stop
      /geo-learning skip
      /geo-learning score

    Integration:
      - instantiate in app.py with game injected
      - add to bot.tree: bot.tree.add_command(GeoLearningCommands(game))
    """

    def __init__(self, game: GeoLearningGame):
        super().__init__(name="geo-learning", description="Geo learning quizzes (flags & scripts)")
        self._game = game

    @app_commands.command(name="start", description="Start a geo-learning quiz in this channel")
    @app_commands.describe(quiz="Choose what to practice")
    async def start(self, interaction: discord.Interaction, quiz: QuizType):
        await interaction.response.defer(thinking=False, ephemeral=True)

        qtype = _map_quiz_type(quiz)
        prompt = await self._game.start(interaction.channel, qtype=qtype)

        # Post prompt publicly, but confirm privately.
        await interaction.followup.send("Started! I posted the first question in the channel.", ephemeral=True)
        await interaction.channel.send(prompt)

    @app_commands.command(name="stop", description="Stop the quiz in this channel")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False, ephemeral=True)

        await self._game.stop(interaction.channel_id)
        await interaction.followup.send("Stopped the quiz in this channel.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current question")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False, ephemeral=True)

        prompt = await self._game.skip(interaction.channel_id)
        if not prompt:
            await interaction.followup.send("No active quiz in this channel.", ephemeral=True)
            return

        await interaction.followup.send("Skipped! I posted a new question.", ephemeral=True)
        await interaction.channel.send(prompt)

    @app_commands.command(name="score", description="Show the current session scoreboard")
    async def score(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False, ephemeral=True)

        text = await self._game.score_text(interaction.channel_id)
        if not text:
            await interaction.followup.send("No active quiz in this channel.", ephemeral=True)
            return

        # scoreboard can be public; but keep ephemeral by default to reduce noise.
        await interaction.followup.send(text, ephemeral=True)

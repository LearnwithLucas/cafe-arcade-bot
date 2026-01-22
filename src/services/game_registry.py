from __future__ import annotations

from typing import Any

import discord

from src.games.base import Game


class GameRegistry:
    def __init__(self) -> None:
        self._games: list[Game] = []

    def register(self, game: Game) -> None:
        self._games.append(game)

    async def handle_discord_message(self, message: discord.Message) -> bool:
        """
        Try all registered games. The first one that consumes the message wins.
        """
        for game in self._games:
            consumed = await game.handle_discord_message(message)
            if consumed:
                return True
        return False

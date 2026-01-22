from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class DiscordMessageLike(Protocol):
    content: str
    author: Any
    channel: Any


class Game(ABC):
    key: str

    @abstractmethod
    async def handle_discord_message(self, message: DiscordMessageLike) -> bool:
        """
        Return True if this game consumed the message (handled it),
        False if ignored.
        """
        raise NotImplementedError

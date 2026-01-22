from __future__ import annotations

from typing import Optional

from src.db.repo.users_repo import UsersRepository
from src.db.repo.leaderboard_repo import LeaderboardRepository
from src.domain.models import LeaderboardRow


class LeaderboardService:
    """
    High-level leaderboard operations.

    Global leaderboard is beans-based and lives in SQLite.
    """

    def __init__(
        self,
        *,
        users_repo: UsersRepository,
        leaderboard_repo: LeaderboardRepository,
    ) -> None:
        self._users_repo = users_repo
        self._leaderboard_repo = leaderboard_repo

    async def get_global_leaderboard(self, *, limit: int = 10) -> list[LeaderboardRow]:
        return await self._leaderboard_repo.get_global_leaderboard(limit=limit)

    async def get_rank_discord(
        self,
        *,
        discord_user_id: int,
        display_name: Optional[str] = None,
    ) -> dict[str, int] | None:
        """
        Returns rank+balance for a Discord user (or None if user doesn't exist yet).
        """
        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=discord_user_id,
            display_name=display_name,
        )
        return await self._leaderboard_repo.get_user_rank(user_id=user.id)

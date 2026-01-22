from __future__ import annotations

import logging
from typing import Optional

from src.db.repo.users_repo import UsersRepository
from src.db.repo.economy_repo import EconomyRepository
from src.services.leaderboard_publisher import LeaderboardPublisher

logger = logging.getLogger(__name__)


class EconomyService:
    def __init__(
        self,
        *,
        users_repo: UsersRepository,
        economy_repo: EconomyRepository,
        leaderboard_publisher: Optional[LeaderboardPublisher] = None,
    ) -> None:
        self._users_repo = users_repo
        self._economy_repo = economy_repo
        self._publisher = leaderboard_publisher

    async def get_balance_discord(self, *, user_id: int, display_name: str | None = None) -> int:
        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=user_id,
            display_name=display_name,
        )
        return await self._economy_repo.get_balance(user.id)

    async def award_beans_discord(
        self,
        *,
        user_id: int,
        amount: int,
        reason: str,
        game_key: str | None = None,
        display_name: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """
        Adds (or subtracts) beans and returns the new balance.
        Triggers a debounced leaderboard refresh.
        """
        if amount == 0:
            return await self.get_balance_discord(user_id=user_id, display_name=display_name)

        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=user_id,
            display_name=display_name,
        )

        new_balance = await self._economy_repo.apply_transaction(
            user_id=user.id,
            delta=int(amount),
            reason=reason,
            game_key=game_key,
            metadata=metadata,
        )

        logger.info(
            "Awarded %s beans to user_id=%s (discord=%s) reason=%s",
            amount,
            user.id,
            user_id,
            reason,
        )

        # Debounced auto-refresh of the leaderboard channel
        if self._publisher:
            try:
                self._publisher.schedule_refresh()
            except Exception:
                logger.exception("Failed scheduling leaderboard refresh")

        return new_balance

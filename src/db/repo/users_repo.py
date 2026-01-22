from __future__ import annotations

import logging
from typing import Optional

from src.db.connection import Database
from src.domain.models import User
from src.domain.errors import NotFound

logger = logging.getLogger(__name__)


class UsersRepository:
    """
    Repository for user persistence.

    Users are logical identities that may be linked to:
    - Discord user IDs
    - Telegram user IDs
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -------------------------
    # Fetching
    # -------------------------

    async def get_by_id(self, user_id: int) -> User:
        row = await self._db.fetchone(
            """
            SELECT id, discord_user_id, telegram_user_id, display_name, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        )
        if not row:
            raise NotFound(f"User id={user_id} not found")

        return self._row_to_user(row)

    async def get_by_discord_id(self, discord_user_id: int) -> Optional[User]:
        row = await self._db.fetchone(
            """
            SELECT id, discord_user_id, telegram_user_id, display_name, created_at
            FROM users
            WHERE discord_user_id = ?
            """,
            (str(discord_user_id),),
        )
        return self._row_to_user(row) if row else None

    async def get_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        row = await self._db.fetchone(
            """
            SELECT id, discord_user_id, telegram_user_id, display_name, created_at
            FROM users
            WHERE telegram_user_id = ?
            """,
            (str(telegram_user_id),),
        )
        return self._row_to_user(row) if row else None

    # -------------------------
    # Creation / update
    # -------------------------

    async def create_discord_user(
        self,
        *,
        discord_user_id: int,
        display_name: str | None,
    ) -> User:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO users (discord_user_id, display_name)
                VALUES (?, ?)
                """,
                (str(discord_user_id), display_name),
            )
            cursor = await conn.execute("SELECT last_insert_rowid();")
            row = await cursor.fetchone()
            user_id = int(row[0])

            # Create bean account automatically
            await conn.execute(
                """
                INSERT INTO bean_accounts (user_id, balance)
                VALUES (?, 0)
                """,
                (user_id,),
            )

        logger.info("Created new Discord user id=%s discord_id=%s", user_id, discord_user_id)
        return await self.get_by_id(user_id)

    async def get_or_create_discord_user(
        self,
        *,
        discord_user_id: int,
        display_name: str | None,
    ) -> User:
        user = await self.get_by_discord_id(discord_user_id)
        if user:
            # Update display name if it changed
            if display_name and display_name != user.display_name:
                await self.update_display_name(user.id, display_name)
                return User(
                    id=user.id,
                    discord_user_id=user.discord_user_id,
                    telegram_user_id=user.telegram_user_id,
                    display_name=display_name,
                    created_at=user.created_at,
                )
            return user

        return await self.create_discord_user(
            discord_user_id=discord_user_id,
            display_name=display_name,
        )

    async def update_display_name(self, user_id: int, display_name: str) -> None:
        await self._db.execute(
            """
            UPDATE users
            SET display_name = ?
            WHERE id = ?
            """,
            (display_name, user_id),
        )

    # -------------------------
    # Mapping
    # -------------------------

    @staticmethod
    def _row_to_user(row) -> User:
        return User(
            id=int(row["id"]),
            discord_user_id=row["discord_user_id"],
            telegram_user_id=row["telegram_user_id"],
            display_name=row["display_name"],
            created_at=row["created_at"],
        )

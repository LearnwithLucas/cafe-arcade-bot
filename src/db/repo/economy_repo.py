from __future__ import annotations

from typing import Optional

from src.db.connection import Database

# Guild ID constants — used as the guild_id column value in DB
GUILD_EN = "en"
GUILD_NL = "nl"


class EconomyRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_balance(self, user_id: int, guild_id: str = GUILD_EN) -> int:
        row = await self._db.fetchone(
            "SELECT balance FROM bean_accounts WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        if not row:
            await self._db.execute(
                "INSERT OR IGNORE INTO bean_accounts (user_id, guild_id, balance) VALUES (?, ?, 0)",
                (user_id, guild_id),
            )
            return 0
        return int(row["balance"])

    async def apply_transaction(
        self,
        *,
        user_id: int,
        delta: int,
        reason: str,
        game_key: Optional[str] = None,
        metadata: Optional[str] = None,
        guild_id: str = GUILD_EN,
    ) -> int:
        """
        Atomically:
          - ensure bean_accounts row exists for (user_id, guild_id)
          - insert into bean_transactions with guild_id
          - update bean_accounts.balance for (user_id, guild_id)
        Returns the new balance.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO bean_accounts (user_id, guild_id, balance) VALUES (?, ?, 0)",
                (user_id, guild_id),
            )

            await conn.execute(
                """
                INSERT INTO bean_transactions (user_id, delta, reason, game_key, metadata, guild_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, int(delta), reason, game_key, metadata, guild_id),
            )

            await conn.execute(
                """
                UPDATE bean_accounts
                SET balance = balance + ?, updated_at = datetime('now')
                WHERE user_id = ? AND guild_id = ?
                """,
                (int(delta), user_id, guild_id),
            )

            cur = await conn.execute(
                "SELECT balance FROM bean_accounts WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id),
            )
            row = await cur.fetchone()
            return int(row["balance"]) if row else 0
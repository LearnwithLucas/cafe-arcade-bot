from __future__ import annotations

from typing import Optional

from src.db.connection import Database


class EconomyRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_balance(self, user_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT balance FROM bean_accounts WHERE user_id = ?",
            (user_id,),
        )
        if not row:
            await self._db.execute(
                "INSERT OR IGNORE INTO bean_accounts (user_id, balance) VALUES (?, 0)",
                (user_id,),
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
    ) -> int:
        """
        Atomically:
          - ensure bean_accounts row exists
          - insert into bean_transactions (including optional metadata)
          - update bean_accounts.balance
        Returns the new balance.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO bean_accounts (user_id, balance) VALUES (?, 0)",
                (user_id,),
            )

            await conn.execute(
                """
                INSERT INTO bean_transactions (user_id, delta, reason, game_key, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, int(delta), reason, game_key, metadata),
            )

            await conn.execute(
                """
                UPDATE bean_accounts
                SET balance = balance + ?, updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (int(delta), user_id),
            )

            cur = await conn.execute(
                "SELECT balance FROM bean_accounts WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            return int(row["balance"]) if row else 0

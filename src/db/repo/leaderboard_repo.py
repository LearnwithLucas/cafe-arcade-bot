from __future__ import annotations

from typing import Any

from src.db.connection import Database
from src.domain.models import LeaderboardRow


class LeaderboardRepository:
    """
    Leaderboards:
    - All-time global beans: ranked by bean_accounts.balance
    - Period leaderboards (today/week) for English games: ranked by SUM(bean_transactions.delta)
      filtered to english_game_keys and since_ts_utc (UTC).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -------------------------
    # All-time (global beans balance)
    # -------------------------

    async def get_global_leaderboard(self, *, limit: int = 10) -> list[LeaderboardRow]:
        limit = max(1, min(50, int(limit)))

        rows = await self._db.fetchall(
            """
            SELECT
                u.id AS user_id,
                COALESCE(u.display_name, u.discord_user_id, u.telegram_user_id, 'Unknown') AS display_name,
                a.balance AS balance
            FROM bean_accounts a
            JOIN users u ON u.id = a.user_id
            ORDER BY a.balance DESC, u.id ASC
            LIMIT ?
            """,
            (limit,),
        )

        return [
            LeaderboardRow(
                user_id=int(r["user_id"]),
                display_name=str(r["display_name"]),
                balance=int(r["balance"]),
            )
            for r in rows
        ]

    async def get_user_rank(self, *, user_id: int) -> dict[str, Any] | None:
        row = await self._db.fetchone(
            """
            SELECT
                a.user_id AS user_id,
                a.balance AS balance,
                (
                    SELECT COUNT(*)
                    FROM bean_accounts a2
                    WHERE a2.balance > a.balance
                ) + 1 AS rank
            FROM bean_accounts a
            WHERE a.user_id = ?
            """,
            (user_id,),
        )
        if not row:
            return None

        return {
            "user_id": int(row["user_id"]),
            "balance": int(row["balance"]),
            "rank": int(row["rank"]),
        }

    # -------------------------
    # Period leaderboards (English games beans earned)
    # -------------------------

    async def get_english_earned_since(
        self,
        *,
        since_ts_utc: str,
        english_game_keys: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Returns rows like:
          { "discord_user_id": "123...", "total_beans": 42 }

        This is computed from the economy ledger:
          SUM(bean_transactions.delta) since since_ts_utc
        filtered to english_game_keys, Discord users only.

        Notes:
        - We count only positive deltas as "earned" (so penalties won't reduce earnings).
        - SQLite datetime('now') is UTC; bean_transactions.created_at should be UTC if created that way.
        """
        limit = max(1, min(50, int(limit)))
        if not english_game_keys:
            return []

        placeholders = ",".join("?" for _ in english_game_keys)

        rows = await self._db.fetchall(
            f"""
            SELECT
                u.discord_user_id AS discord_user_id,
                SUM(CASE WHEN bt.delta > 0 THEN bt.delta ELSE 0 END) AS total_beans
            FROM bean_transactions bt
            JOIN users u ON u.id = bt.user_id
            WHERE bt.created_at >= ?
              AND bt.game_key IN ({placeholders})
              AND u.discord_user_id IS NOT NULL
            GROUP BY u.discord_user_id
            ORDER BY total_beans DESC
            LIMIT ?
            """,
            (since_ts_utc, *english_game_keys, limit),
        )

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "discord_user_id": str(r["discord_user_id"]),
                    "total_beans": int(r["total_beans"] or 0),
                }
            )
        return out

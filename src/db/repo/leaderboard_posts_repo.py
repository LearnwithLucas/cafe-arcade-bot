from __future__ import annotations

from typing import Optional

from src.db.connection import Database


class LeaderboardPostsRepository:
    """
    Stores the message IDs for leaderboard embeds so we can edit in-place.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_message_id(
        self,
        *,
        platform: str,
        channel_id: str,
        board_key: str,
    ) -> Optional[int]:
        row = await self._db.fetchone(
            """
            SELECT message_id
            FROM leaderboard_posts
            WHERE platform = ? AND channel_id = ? AND board_key = ?
            """,
            (platform, channel_id, board_key),
        )
        if not row:
            return None
        try:
            return int(row["message_id"])
        except (TypeError, ValueError):
            return None

    async def upsert_message_id(
        self,
        *,
        platform: str,
        channel_id: str,
        board_key: str,
        message_id: int,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO leaderboard_posts (platform, channel_id, board_key, message_id, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(platform, channel_id, board_key)
            DO UPDATE SET message_id = excluded.message_id, updated_at = datetime('now')
            """,
            (platform, channel_id, board_key, str(message_id)),
        )

from __future__ import annotations

import json
from typing import Any, Optional

from src.db.connection import Database
from src.domain.models import ActiveGameSession


class GamesRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_active_session(
        self,
        *,
        platform: str,
        location_id: str,
        thread_id: str | None,
        game_key: str,
    ) -> Optional[ActiveGameSession]:
        row = await self._db.fetchone(
            """
            SELECT id, platform, location_id, thread_id, game_key, status, state, created_at, updated_at, expires_at
            FROM active_game_sessions
            WHERE platform = ?
              AND location_id = ?
              AND (thread_id IS ? OR thread_id = ?)
              AND game_key = ?
              AND status = 'active'
            LIMIT 1
            """,
            (platform, location_id, thread_id, thread_id, game_key),
        )
        if not row:
            return None

        return ActiveGameSession(
            id=row["id"],
            platform=row["platform"],
            location_id=row["location_id"],
            thread_id=row["thread_id"],
            game_key=row["game_key"],
            status=row["status"],
            state_json=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    async def upsert_active_session(
        self,
        *,
        session_id: str,
        platform: str,
        location_id: str,
        thread_id: str | None,
        game_key: str,
        state: dict[str, Any],
        expires_at: str | None = None,
    ) -> None:
        state_json = json.dumps(state, ensure_ascii=False)

        await self._db.execute(
            """
            INSERT INTO active_game_sessions (id, platform, location_id, thread_id, game_key, status, state, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, datetime('now'), datetime('now'), ?)
            ON CONFLICT(id) DO UPDATE SET
                status = 'active',
                state = excluded.state,
                updated_at = datetime('now'),
                expires_at = excluded.expires_at
            """,
            (session_id, platform, location_id, thread_id, game_key, state_json, expires_at),
        )

    async def end_session(self, *, session_id: str, status: str = "ended") -> None:
        await self._db.execute(
            """
            UPDATE active_game_sessions
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (status, session_id),
        )

    async def end_active_in_location(
        self,
        *,
        platform: str,
        location_id: str,
        thread_id: str | None,
        game_key: str,
        status: str = "ended",
    ) -> int:
        """
        Ends any active session of a game in a channel/thread. Returns number of rows affected.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE active_game_sessions
                SET status = ?, updated_at = datetime('now')
                WHERE platform = ?
                  AND location_id = ?
                  AND (thread_id IS ? OR thread_id = ?)
                  AND game_key = ?
                  AND status = 'active'
                """,
                (status, platform, location_id, thread_id, thread_id, game_key),
            )
            return cursor.rowcount

    async def record_game_result(
        self,
        *,
        user_id: int,
        game_key: str,
        score: int | None,
        beans_earned: int,
        context_json: str | None = None,
    ) -> None:
        """
        Persist a completed game result to game_results.
        """
        await self._db.execute(
            """
            INSERT INTO game_results (user_id, game_key, score, beans_earned, context, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (user_id, game_key, score, beans_earned, context_json),
        )

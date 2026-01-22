from __future__ import annotations

import logging
from typing import Awaitable, Callable

from src.db.connection import Database

logger = logging.getLogger(__name__)


# -----------------------------
# Helpers
# -----------------------------
async def _table_exists(conn, table_name: str) -> bool:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
        (table_name,),
    )
    row = await cursor.fetchone()
    return row is not None


async def _column_exists(conn, table: str, column: str) -> bool:
    cursor = await conn.execute(f"PRAGMA table_info({table});")
    rows = await cursor.fetchall()
    return any(r["name"] == column for r in rows)


# -----------------------------
# Migrations
# -----------------------------
async def _migration_v1(conn) -> None:
    """
    Initial schema.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            platform_user_id TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(platform, platform_user_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bean_accounts (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bean_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            game_key TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # Legacy name kept for backwards compatibility (some older code used this).
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_sessions (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            location_id TEXT NOT NULL,
            thread_id TEXT,
            game_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            score INTEGER NOT NULL,
            beans_earned INTEGER NOT NULL,
            context_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            board_key TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(platform, channel_id, board_key)
        );
        """
    )


async def _migration_v2(conn) -> None:
    """
    Defensive fixes for existing databases.
    """
    if await _table_exists(conn, "bean_transactions"):
        if not await _column_exists(conn, "bean_transactions", "metadata"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN metadata TEXT;")

        if not await _column_exists(conn, "bean_transactions", "game_key"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN game_key TEXT;")

    if await _table_exists(conn, "user_identities"):
        if not await _column_exists(conn, "user_identities", "display_name"):
            await conn.execute("ALTER TABLE user_identities ADD COLUMN display_name TEXT;")


async def _migration_v3(conn) -> None:
    """
    Shop v1:
      - Persistent inventory per user
      - Daily use tracking per item (UTC day string)
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_inventory (
            user_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, item_key),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_item_uses (
            user_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            day_utc TEXT NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, item_key, day_utc),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )


async def _migration_v4(conn) -> None:
    """
    Compatibility + Shop catalog (recommended):

    1) Create active_game_sessions table used by GamesRepository (keeps game_sessions as legacy).
    2) Make game_results compatible with both 'context' and 'context_json' column names.
    3) Add shop_items catalog so shop can be server-configured without code changes.
    """
    # --- Active sessions table (used by current code) ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_game_sessions (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            location_id TEXT NOT NULL,
            thread_id TEXT,
            game_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT
        );
        """
    )

    # Optional: speed up active session lookups
    try:
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_active_game_sessions_lookup
            ON active_game_sessions(platform, location_id, thread_id, game_key, status);
            """
        )
    except Exception:
        pass

    # --- game_results column compatibility ---
    if await _table_exists(conn, "game_results"):
        # Newer code may insert into "context" (GamesRepository uses context)
        if not await _column_exists(conn, "game_results", "context"):
            await conn.execute("ALTER TABLE game_results ADD COLUMN context TEXT;")

        # Older/other code may still use context_json
        if not await _column_exists(conn, "game_results", "context_json"):
            await conn.execute("ALTER TABLE game_results ADD COLUMN context_json TEXT;")

    # --- Shop catalog (3 items live here, plus future items) ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_items (
            item_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price INTEGER NOT NULL,
            max_use_per_day INTEGER NOT NULL DEFAULT 0,
            max_inventory INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    # Optional indexes for speed
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shop_inventory_user ON shop_inventory(user_id);")
    except Exception:
        pass
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shop_item_uses_user_day ON shop_item_uses(user_id, day_utc);")
    except Exception:
        pass


MIGRATIONS: list[tuple[int, Callable[[object], Awaitable[None]]]] = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
]


# -----------------------------
# Runner
# -----------------------------
async def run_migrations(db: Database) -> None:
    logger.info("Running DB migrations (if needed)")

    async with db.transaction() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        cursor = await conn.execute("SELECT MAX(version) AS v FROM schema_migrations;")
        row = await cursor.fetchone()
        current_version = int(row["v"]) if row and row["v"] is not None else 0

        for version, fn in MIGRATIONS:
            if version <= current_version:
                continue

            logger.info("Applying migration v%s", version)
            await fn(conn)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?);",
                (version,),
            )

    logger.info("DB migrations complete")

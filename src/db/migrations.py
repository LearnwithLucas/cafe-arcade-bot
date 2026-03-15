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
    if await _table_exists(conn, "bean_transactions"):
        if not await _column_exists(conn, "bean_transactions", "metadata"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN metadata TEXT;")
        if not await _column_exists(conn, "bean_transactions", "game_key"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN game_key TEXT;")
    if await _table_exists(conn, "user_identities"):
        if not await _column_exists(conn, "user_identities", "display_name"):
            await conn.execute("ALTER TABLE user_identities ADD COLUMN display_name TEXT;")


async def _migration_v3(conn) -> None:
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
    try:
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_active_game_sessions_lookup
            ON active_game_sessions(platform, location_id, thread_id, game_key, status);
            """
        )
    except Exception:
        pass
    if await _table_exists(conn, "game_results"):
        if not await _column_exists(conn, "game_results", "context"):
            await conn.execute("ALTER TABLE game_results ADD COLUMN context TEXT;")
        if not await _column_exists(conn, "game_results", "context_json"):
            await conn.execute("ALTER TABLE game_results ADD COLUMN context_json TEXT;")
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
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shop_inventory_user ON shop_inventory(user_id);")
    except Exception:
        pass
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shop_item_uses_user_day ON shop_item_uses(user_id, day_utc);")
    except Exception:
        pass


async def _migration_v5(conn) -> None:
    if not await _table_exists(conn, "users"):
        return
    if not await _column_exists(conn, "users", "discord_user_id"):
        await conn.execute("ALTER TABLE users ADD COLUMN discord_user_id TEXT;")
    if not await _column_exists(conn, "users", "telegram_user_id"):
        await conn.execute("ALTER TABLE users ADD COLUMN telegram_user_id TEXT;")
    if not await _column_exists(conn, "users", "display_name"):
        await conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT;")
    if await _table_exists(conn, "user_identities"):
        await conn.execute(
            """
            UPDATE users SET discord_user_id = (
                SELECT ui.platform_user_id FROM user_identities ui
                WHERE ui.user_id = users.id AND ui.platform = 'discord' LIMIT 1
            ) WHERE discord_user_id IS NULL;
            """
        )
        await conn.execute(
            """
            UPDATE users SET telegram_user_id = (
                SELECT ui.platform_user_id FROM user_identities ui
                WHERE ui.user_id = users.id AND ui.platform = 'telegram' LIMIT 1
            ) WHERE telegram_user_id IS NULL;
            """
        )
        await conn.execute(
            """
            UPDATE users SET display_name = (
                SELECT ui.display_name FROM user_identities ui
                WHERE ui.user_id = users.id
                  AND ui.platform IN ('discord', 'telegram')
                  AND ui.display_name IS NOT NULL
                ORDER BY CASE ui.platform WHEN 'discord' THEN 0 ELSE 1 END
                LIMIT 1
            ) WHERE display_name IS NULL;
            """
        )
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_discord_user_id ON users(discord_user_id);")
    except Exception:
        pass
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_user_id ON users(telegram_user_id);")
    except Exception:
        pass


async def _migration_v6(conn) -> None:
    """
    Adds guild_id column to bean tables (default 'en' for all existing rows).
    """
    if await _table_exists(conn, "bean_accounts"):
        if not await _column_exists(conn, "bean_accounts", "guild_id"):
            await conn.execute("ALTER TABLE bean_accounts ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")
    if await _table_exists(conn, "bean_transactions"):
        if not await _column_exists(conn, "bean_transactions", "guild_id"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")
    if await _table_exists(conn, "shop_inventory"):
        if not await _column_exists(conn, "shop_inventory", "guild_id"):
            await conn.execute("ALTER TABLE shop_inventory ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")
    if await _table_exists(conn, "shop_item_uses"):
        if not await _column_exists(conn, "shop_item_uses", "guild_id"):
            await conn.execute("ALTER TABLE shop_item_uses ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bean_accounts_guild ON bean_accounts(user_id, guild_id);")
    except Exception:
        pass
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bean_transactions_guild ON bean_transactions(user_id, guild_id);")
    except Exception:
        pass


async def _migration_v7(conn) -> None:
    """
    Fixes bean_accounts primary key to be (user_id, guild_id) composite.

    The original table had user_id as sole PRIMARY KEY, which prevented
    a user from having separate English and Dutch balances. This migration:
      1. Renames the old table
      2. Creates a new one with composite PK (user_id, guild_id)
      3. Copies all existing rows (preserving English balances)
      4. Drops the old table
    """
    if not await _table_exists(conn, "bean_accounts"):
        # Fresh DB — create with correct schema from the start
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bean_accounts (
                user_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL DEFAULT 'en',
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, guild_id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        return

    # Check if the PK is already composite by checking if guild_id is part of PK
    # We detect this by trying to insert a duplicate (user_id, guild_id='nl') — instead
    # just always recreate safely via rename+copy.
    await conn.execute("ALTER TABLE bean_accounts RENAME TO bean_accounts_old;")

    await conn.execute(
        """
        CREATE TABLE bean_accounts (
            user_id INTEGER NOT NULL,
            guild_id TEXT NOT NULL DEFAULT 'en',
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, guild_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # Copy existing rows — guild_id column exists from v6 so this is safe
    await conn.execute(
        """
        INSERT INTO bean_accounts (user_id, guild_id, balance, created_at, updated_at)
        SELECT user_id, guild_id, balance, created_at, updated_at
        FROM bean_accounts_old;
        """
    )

    await conn.execute("DROP TABLE bean_accounts_old;")

    # Recreate index on new table
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bean_accounts_guild ON bean_accounts(user_id, guild_id);"
        )
    except Exception:
        pass

    logger.info("Migration v7: bean_accounts rebuilt with composite PK (user_id, guild_id)")


MIGRATIONS = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
    (6, _migration_v6),
    (7, _migration_v7),
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
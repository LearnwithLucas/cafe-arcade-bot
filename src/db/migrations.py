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
    Shop v1.
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
    Compatibility + Shop catalog.
    """
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
    """
    Users table compatibility.
    """
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
            UPDATE users
               SET discord_user_id = (
                     SELECT ui.platform_user_id
                       FROM user_identities ui
                      WHERE ui.user_id = users.id
                        AND ui.platform = 'discord'
                      LIMIT 1
                   )
             WHERE discord_user_id IS NULL;
            """
        )
        await conn.execute(
            """
            UPDATE users
               SET telegram_user_id = (
                     SELECT ui.platform_user_id
                       FROM user_identities ui
                      WHERE ui.user_id = users.id
                        AND ui.platform = 'telegram'
                      LIMIT 1
                   )
             WHERE telegram_user_id IS NULL;
            """
        )
        await conn.execute(
            """
            UPDATE users
               SET display_name = (
                     SELECT ui.display_name
                       FROM user_identities ui
                      WHERE ui.user_id = users.id
                        AND ui.platform IN ('discord', 'telegram')
                        AND ui.display_name IS NOT NULL
                      ORDER BY CASE ui.platform WHEN 'discord' THEN 0 ELSE 1 END
                      LIMIT 1
                   )
             WHERE display_name IS NULL;
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
    Guild-scoped economy.

    Adds guild_id to bean_accounts and bean_transactions so Dutch and English
    members have separate balances and transaction histories.

    - guild_id TEXT NULL — NULL means English server (legacy rows preserved as-is)
    - bean_accounts primary key changes from (user_id) to (user_id, guild_id)
      via a new guild-aware table.
    - Dutch shop inventory is also scoped by guild.
    """
    # --- bean_accounts: add guild_id column ---
    if await _table_exists(conn, "bean_accounts"):
        if not await _column_exists(conn, "bean_accounts", "guild_id"):
            await conn.execute("ALTER TABLE bean_accounts ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")

    # --- bean_transactions: add guild_id column ---
    if await _table_exists(conn, "bean_transactions"):
        if not await _column_exists(conn, "bean_transactions", "guild_id"):
            await conn.execute("ALTER TABLE bean_transactions ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")

    # --- shop_inventory: add guild_id column ---
    if await _table_exists(conn, "shop_inventory"):
        if not await _column_exists(conn, "shop_inventory", "guild_id"):
            await conn.execute("ALTER TABLE shop_inventory ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")

    # --- shop_item_uses: add guild_id column ---
    if await _table_exists(conn, "shop_item_uses"):
        if not await _column_exists(conn, "shop_item_uses", "guild_id"):
            await conn.execute("ALTER TABLE shop_item_uses ADD COLUMN guild_id TEXT NOT NULL DEFAULT 'en';")

    # Indexes for guild-scoped queries
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bean_accounts_guild ON bean_accounts(user_id, guild_id);"
        )
    except Exception:
        pass
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bean_transactions_guild ON bean_transactions(user_id, guild_id);"
        )
    except Exception:
        pass


MIGRATIONS = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
    (6, _migration_v6),
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
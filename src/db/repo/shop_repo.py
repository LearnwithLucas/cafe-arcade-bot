from __future__ import annotations

from typing import Any

from src.db.connection import Database


class ShopRepository:
    """
    DB-backed shop storage.

    Tables used:
      - shop_items(item_key, name, description, price, max_use_per_day, max_inventory, created_at, updated_at)
      - shop_inventory(user_id, item_key, quantity, updated_at)
      - shop_item_uses(user_id, item_key, day_utc, used_count, updated_at)
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -----------------------
    # Catalog (shop_items)
    # -----------------------

    async def upsert_item(
        self,
        *,
        item_key: str,
        name: str,
        description: str,
        price: int,
        max_use_per_day: int,
        max_inventory: int,
    ) -> None:
        """
        Insert/update an item in the shop catalog.
        """
        await self._db.execute(
            """
            INSERT INTO shop_items (item_key, name, description, price, max_use_per_day, max_inventory, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(item_key) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                price = excluded.price,
                max_use_per_day = excluded.max_use_per_day,
                max_inventory = excluded.max_inventory,
                updated_at = datetime('now')
            """,
            (
                str(item_key),
                str(name),
                str(description),
                int(price),
                int(max_use_per_day),
                int(max_inventory),
            ),
        )

    async def get_item(self, *, item_key: str) -> dict[str, Any] | None:
        row = await self._db.fetchone(
            """
            SELECT item_key, name, description, price, max_use_per_day, max_inventory
            FROM shop_items
            WHERE item_key = ?
            """,
            (str(item_key),),
        )
        if not row:
            return None
        return {
            "item_key": str(row["item_key"]),
            "name": str(row["name"]),
            "description": str(row["description"]),
            "price": int(row["price"]),
            "max_use_per_day": int(row["max_use_per_day"]),
            "max_inventory": int(row["max_inventory"]),
        }

    async def list_items(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall(
            """
            SELECT item_key, name, description, price, max_use_per_day, max_inventory
            FROM shop_items
            ORDER BY price ASC, item_key ASC
            """
        )
        return [
            {
                "item_key": str(r["item_key"]),
                "name": str(r["name"]),
                "description": str(r["description"]),
                "price": int(r["price"]),
                "max_use_per_day": int(r["max_use_per_day"]),
                "max_inventory": int(r["max_inventory"]),
            }
            for r in rows
        ]

    # -----------------------
    # Inventory (shop_inventory)
    # -----------------------

    async def get_quantity(self, *, user_id: int, item_key: str) -> int:
        row = await self._db.fetchone(
            """
            SELECT quantity
            FROM shop_inventory
            WHERE user_id = ? AND item_key = ?
            """,
            (int(user_id), str(item_key)),
        )
        return int(row["quantity"]) if row else 0

    async def set_quantity(self, *, user_id: int, item_key: str, quantity: int) -> None:
        await self._db.execute(
            """
            INSERT INTO shop_inventory (user_id, item_key, quantity, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, item_key)
            DO UPDATE SET quantity = excluded.quantity, updated_at = datetime('now')
            """,
            (int(user_id), str(item_key), int(quantity)),
        )

    async def add_quantity(self, *, user_id: int, item_key: str, delta: int) -> int:
        """
        Adds delta (can be negative). Returns new quantity.
        NOTE: This does not clamp at 0. Callers should validate before subtracting.
        """
        async with self._db.transaction() as conn:
            # Ensure row exists
            await conn.execute(
                """
                INSERT OR IGNORE INTO shop_inventory (user_id, item_key, quantity, updated_at)
                VALUES (?, ?, 0, datetime('now'))
                """,
                (int(user_id), str(item_key)),
            )

            await conn.execute(
                """
                UPDATE shop_inventory
                SET quantity = quantity + ?, updated_at = datetime('now')
                WHERE user_id = ? AND item_key = ?
                """,
                (int(delta), int(user_id), str(item_key)),
            )

            cur = await conn.execute(
                """
                SELECT quantity
                FROM shop_inventory
                WHERE user_id = ? AND item_key = ?
                """,
                (int(user_id), str(item_key)),
            )
            row = await cur.fetchone()
            return int(row["quantity"]) if row else 0

    async def get_inventory(self, *, user_id: int) -> list[dict[str, int | str]]:
        rows = await self._db.fetchall(
            """
            SELECT item_key, quantity
            FROM shop_inventory
            WHERE user_id = ?
            ORDER BY item_key ASC
            """,
            (int(user_id),),
        )
        return [{"item_key": str(r["item_key"]), "quantity": int(r["quantity"])} for r in rows]

    async def get_inventory_with_catalog(self, *, user_id: int) -> list[dict[str, Any]]:
        """
        Convenience for UI: returns joined rows even if quantity is 0.
        """
        rows = await self._db.fetchall(
            """
            SELECT
                si.item_key AS item_key,
                si.name AS name,
                si.description AS description,
                si.price AS price,
                si.max_use_per_day AS max_use_per_day,
                si.max_inventory AS max_inventory,
                COALESCE(inv.quantity, 0) AS quantity
            FROM shop_items si
            LEFT JOIN shop_inventory inv
              ON inv.user_id = ? AND inv.item_key = si.item_key
            ORDER BY si.price ASC, si.item_key ASC
            """,
            (int(user_id),),
        )
        return [
            {
                "item_key": str(r["item_key"]),
                "name": str(r["name"]),
                "description": str(r["description"]),
                "price": int(r["price"]),
                "max_use_per_day": int(r["max_use_per_day"]),
                "max_inventory": int(r["max_inventory"]),
                "quantity": int(r["quantity"]),
            }
            for r in rows
        ]

    # -----------------------
    # Daily uses (shop_item_uses) — UTC day string
    # -----------------------

    async def get_used_today(self, *, user_id: int, item_key: str, day_utc: str) -> int:
        row = await self._db.fetchone(
            """
            SELECT used_count
            FROM shop_item_uses
            WHERE user_id = ? AND item_key = ? AND day_utc = ?
            """,
            (int(user_id), str(item_key), str(day_utc)),
        )
        return int(row["used_count"]) if row else 0

    async def increment_used_today(self, *, user_id: int, item_key: str, day_utc: str, delta: int) -> int:
        """
        Increments used_count for (user,item,day). Returns new used_count.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO shop_item_uses (user_id, item_key, day_utc, used_count, updated_at)
                VALUES (?, ?, ?, 0, datetime('now'))
                """,
                (int(user_id), str(item_key), str(day_utc)),
            )

            await conn.execute(
                """
                UPDATE shop_item_uses
                SET used_count = used_count + ?, updated_at = datetime('now')
                WHERE user_id = ? AND item_key = ? AND day_utc = ?
                """,
                (int(delta), int(user_id), str(item_key), str(day_utc)),
            )

            cur = await conn.execute(
                """
                SELECT used_count
                FROM shop_item_uses
                WHERE user_id = ? AND item_key = ? AND day_utc = ?
                """,
                (int(user_id), str(item_key), str(day_utc)),
            )
            row = await cur.fetchone()
            return int(row["used_count"]) if row else 0

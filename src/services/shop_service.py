from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.db.repo.shop_repo import ShopRepository
from src.db.repo.users_repo import UsersRepository
from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.rewards_service import RewardsService
from src.services.shop_items import ShopItems, ShopItem


def _utc_day_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ShopResult:
    ok: bool
    message: str
    new_balance: int | None = None
    new_quantity: int | None = None


class ShopService:
    """
    Buying + using shop items with:
      - DB catalog (shop_items)
      - DB inventory (shop_inventory)
      - DB daily-use tracking (shop_item_uses, UTC day)
      - EconomyService for bean transactions

    IMPORTANT:
      - We keep ShopItems as the in-code "source of truth" for starter items,
        but we upsert them into shop_items so you can render a proper shop UI.
      - rewards/cooldowns are accepted for future expansion (discounts, item effects, etc).
    """

    def __init__(
        self,
        *,
        users_repo: UsersRepository,
        economy: EconomyService,
        shop_repo: ShopRepository,
        rewards: RewardsService | None = None,
        cooldowns: Cooldowns | None = None,
    ) -> None:
        self._users_repo = users_repo
        self._economy = economy
        self._shop_repo = shop_repo

        # reserved for future effects (not used yet)
        self._rewards = rewards
        self._cooldowns = cooldowns

        self._catalog_seeded: bool = False

    # -----------------------
    # Catalog seeding
    # -----------------------

    async def _ensure_catalog_seeded(self) -> None:
        """
        Upserts your in-code catalog into DB (idempotent).
        This makes the system Render-safe and restart-safe without relying on memory.
        """
        if self._catalog_seeded:
            return

        items = ShopItems.all()
        for _, item in items.items():
            # Map ShopItem -> DB catalog fields
            await self._shop_repo.upsert_item(
                item_key=str(item.key),
                name=str(item.name),
                description=str(getattr(item, "description", "") or ""),
                price=int(getattr(item, "cost_beans", 0)),
                max_use_per_day=int(getattr(item, "max_uses_per_day", 0)),
                max_inventory=int(getattr(item, "max_stack", 0)),
            )

        self._catalog_seeded = True

    async def _get_item(self, item_key: str) -> tuple[dict[str, Any] | None, ShopItem | None]:
        """
        Returns:
          (db_item_row_or_none, code_item_or_none)
        """
        await self._ensure_catalog_seeded()
        db_item = await self._shop_repo.get_item(item_key=str(item_key))
        code_item = ShopItems.get(item_key)
        return db_item, code_item

    # -----------------------
    # Read APIs
    # -----------------------

    async def inventory_discord(self, *, discord_user_id: int, display_name: str | None = None) -> list[dict[str, Any]]:
        await self._ensure_catalog_seeded()

        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=discord_user_id,
            display_name=display_name,
        )

        # Joined view: includes all shop items (even if quantity = 0)
        rows = await self._shop_repo.get_inventory_with_catalog(user_id=user.id)

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "key": str(r["item_key"]),
                    "name": str(r["name"]),
                    "description": str(r["description"]),
                    "price": int(r["price"]),
                    "quantity": int(r["quantity"]),
                    "max_stack": int(r["max_inventory"]),
                    "max_uses_per_day": int(r["max_use_per_day"]),
                }
            )

        out.sort(key=lambda x: (int(x.get("price", 0)), str(x.get("key", ""))))
        return out

    async def shop_list(self) -> list[dict[str, Any]]:
        await self._ensure_catalog_seeded()
        return await self._shop_repo.list_items()

    # -----------------------
    # Buy
    # -----------------------

    async def buy_discord(
        self,
        *,
        discord_user_id: int,
        display_name: str | None,
        item_key: str,
        quantity: int,
    ) -> ShopResult:
        db_item, code_item = await self._get_item(item_key)
        if not db_item and not code_item:
            return ShopResult(False, f"Unknown item: `{item_key}`")

        # Prefer DB fields; fallback to code fields
        key = str(db_item["item_key"]) if db_item else str(code_item.key)
        name = str(db_item["name"]) if db_item else str(code_item.name)
        price = int(db_item["price"]) if db_item else int(getattr(code_item, "cost_beans", 0))
        max_stack = int(db_item["max_inventory"]) if db_item else int(getattr(code_item, "max_stack", 0))

        if price <= 0 or max_stack <= 0:
            return ShopResult(False, f"{name} is not purchasable yet.")

        qty = max(1, min(99, int(quantity)))

        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=discord_user_id,
            display_name=display_name,
        )

        current_qty = await self._shop_repo.get_quantity(user_id=user.id, item_key=key)
        if current_qty >= max_stack:
            return ShopResult(False, f"You already have the max **{max_stack}** for {name}.")

        qty_to_add = min(qty, max_stack - current_qty)
        total_cost = int(price) * int(qty_to_add)

        balance = await self._economy.get_balance_discord(
            user_id=discord_user_id,
            display_name=display_name,
        )
        if balance < total_cost:
            return ShopResult(False, f"Not enough beans. Cost: **{total_cost}**, you have: **{balance}**.")

        # Charge beans (negative transaction)
        new_balance = await self._economy.award_beans_discord(
            user_id=discord_user_id,
            amount=-int(total_cost),
            reason="Shop purchase",
            game_key="shop",
            display_name=display_name,
            metadata=json.dumps({"item": key, "qty": qty_to_add, "cost": total_cost}, ensure_ascii=False),
        )

        new_qty = await self._shop_repo.add_quantity(user_id=user.id, item_key=key, delta=qty_to_add)

        return ShopResult(
            True,
            f"Purchased **{qty_to_add}×** {name} for **{total_cost} beans**.",
            new_balance=int(new_balance),
            new_quantity=int(new_qty),
        )

    # -----------------------
    # Use
    # -----------------------

    async def use_discord(
        self,
        *,
        discord_user_id: int,
        display_name: str | None,
        item_key: str,
        quantity: int,
    ) -> ShopResult:
        db_item, code_item = await self._get_item(item_key)
        if not db_item and not code_item:
            return ShopResult(False, f"Unknown item: `{item_key}`")

        key = str(db_item["item_key"]) if db_item else str(code_item.key)
        name = str(db_item["name"]) if db_item else str(code_item.name)

        max_uses_per_day = int(db_item["max_use_per_day"]) if db_item else int(getattr(code_item, "max_uses_per_day", 0))
        if max_uses_per_day <= 0:
            return ShopResult(False, f"{name} can’t be used yet.")

        qty = max(1, min(25, int(quantity)))

        user = await self._users_repo.get_or_create_discord_user(
            discord_user_id=discord_user_id,
            display_name=display_name,
        )

        current_qty = await self._shop_repo.get_quantity(user_id=user.id, item_key=key)
        if current_qty <= 0:
            return ShopResult(False, f"You don’t have any **{name}**.")

        qty_to_use = min(qty, current_qty)

        day = _utc_day_str()
        used_today = await self._shop_repo.get_used_today(user_id=user.id, item_key=key, day_utc=day)
        remaining_today = max(0, int(max_uses_per_day) - int(used_today))
        if remaining_today <= 0:
            return ShopResult(False, f"You’ve hit today’s limit for {name} (**{max_uses_per_day}/day**).")

        qty_to_use = min(qty_to_use, remaining_today)
        if qty_to_use <= 0:
            return ShopResult(False, f"You can’t use any more {name} today.")

        # Apply use: decrement inventory + increment daily uses
        new_qty = await self._shop_repo.add_quantity(user_id=user.id, item_key=key, delta=-qty_to_use)
        await self._shop_repo.increment_used_today(user_id=user.id, item_key=key, day_utc=day, delta=qty_to_use)

        # Flavor (no gameplay advantage yet)
        if key == "coffee":
            msg = f"☕ You drink **{qty_to_use}×** Coffee. Productivity +100% (emotionally)."
        elif key == "tea":
            msg = f"🍵 You sip **{qty_to_use}×** Tea. Inner peace +1."
        elif key == "cookie":
            msg = f"🍪 You eat **{qty_to_use}×** Cookie. Good vibes only."
        else:
            msg = f"Used **{qty_to_use}×** {name}."

        return ShopResult(True, msg, new_quantity=int(new_qty))

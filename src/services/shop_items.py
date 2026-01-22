from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class ShopItem:
    """
    Canonical in-code defaults for shop items.

    IMPORTANT:
      - These are the *default seeds* for the DB catalog (shop_items table).
      - Your live shop should read the catalog from SQLite (so you can rebalance later
        without redeploying), but we keep these defaults so a fresh DB “just works”.
    """

    key: str
    name: str
    description: str

    # Price in beans
    cost_beans: int

    # Inventory cap (max quantity user can hold)
    max_stack: int

    # Daily usage limit (max uses per UTC day)
    max_uses_per_day: int

    # ---- Compatibility aliases (DB naming) ----
    @property
    def price(self) -> int:
        return int(self.cost_beans)

    @property
    def max_inventory(self) -> int:
        return int(self.max_stack)

    @property
    def max_use_per_day(self) -> int:
        return int(self.max_uses_per_day)


class ShopItems:
    """
    Default item catalog (seed data).

    You can add/rebalance items here; then, on startup, seed these into the DB table
    `shop_items` via ShopRepository.upsert_item(...).

    After that, your shop UI/logic should read from the DB (recommended).
    """

    COFFEE = ShopItem(
        key="coffee",
        name="☕ Coffee",
        description="A warm coffee. Productivity +100% (emotionally).",
        cost_beans=25,
        max_stack=25,
        max_uses_per_day=3,
    )

    TEA = ShopItem(
        key="tea",
        name="🍵 Tea",
        description="A calm cup of tea. Inner peace +1.",
        cost_beans=25,
        max_stack=25,
        max_uses_per_day=3,
    )

    COOKIE = ShopItem(
        key="cookie",
        name="🍪 Cookie",
        description="A tasty cookie. Good vibes only.",
        cost_beans=10,
        max_stack=50,
        max_uses_per_day=5,
    )

    @classmethod
    def all(cls) -> Dict[str, ShopItem]:
        return {
            cls.COFFEE.key: cls.COFFEE,
            cls.TEA.key: cls.TEA,
            cls.COOKIE.key: cls.COOKIE,
        }

    @classmethod
    def list(cls) -> List[ShopItem]:
        return list(cls.all().values())

    @classmethod
    def keys(cls) -> Iterable[str]:
        return cls.all().keys()

    @classmethod
    def get(cls, key: str) -> ShopItem | None:
        return cls.all().get((key or "").strip().lower())

    @classmethod
    async def seed_defaults(cls, *, shop_repo) -> None:
        """
        Seed default items into the DB catalog.

        Expects shop_repo to implement:
          upsert_item(item_key, name, description, price, max_use_per_day, max_inventory)

        Safe to call on every startup.
        """
        for item in cls.list():
            await shop_repo.upsert_item(
                item_key=item.key,
                name=item.name,
                description=item.description,
                price=int(item.cost_beans),
                max_use_per_day=int(item.max_uses_per_day),
                max_inventory=int(item.max_stack),
            )

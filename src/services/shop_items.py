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
        without redeploying), but we keep these defaults so a fresh DB "just works".
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

    Item price guide:
      1–3 beans   — daily treats (cookie, tea, coffee)
      4–8 beans   — small rewards (snack, juice, phrasebook)
      10–15 beans — useful tools (dictionary, flashcards)
      20 beans    — prestige badge

    You can add/rebalance items here; then, on startup, seed these into the DB table
    `shop_items` via ShopRepository.upsert_item(...).
    """

    # ---- Food & Drinks ----

    COOKIE = ShopItem(
        key="cookie",
        name="🍪 Cookie",
        description="A tasty cookie. Good vibes only.",
        cost_beans=2,
        max_stack=50,
        max_uses_per_day=5,
    )

    COFFEE = ShopItem(
        key="coffee",
        name="☕ Coffee",
        description="A warm coffee. Productivity +100% (emotionally).",
        cost_beans=3,
        max_stack=25,
        max_uses_per_day=3,
    )

    TEA = ShopItem(
        key="tea",
        name="🍵 Tea",
        description="A calm cup of tea. Inner peace +1.",
        cost_beans=3,
        max_stack=25,
        max_uses_per_day=3,
    )

    STROOPWAFEL = ShopItem(
        key="stroopwafel",
        name="🧇 Stroopwafel",
        description="A Dutch classic. Place it on your coffee and wait. Patience is a language skill.",
        cost_beans=4,
        max_stack=25,
        max_uses_per_day=3,
    )

    JUICE = ShopItem(
        key="juice",
        name="🧃 Juice Box",
        description="A little juice box. Small but mighty.",
        cost_beans=5,
        max_stack=20,
        max_uses_per_day=2,
    )

    # ---- Language Learning ----

    PHRASEBOOK = ShopItem(
        key="phrasebook",
        name="📖 Phrasebook",
        description="A pocket phrasebook. Fits in your back pocket, weighs nothing, impresses everyone.",
        cost_beans=6,
        max_stack=10,
        max_uses_per_day=2,
    )

    FLASHCARDS = ShopItem(
        key="flashcards",
        name="🗂️ Flashcards",
        description="A fresh deck of flashcards. The old ones are full of doodles anyway.",
        cost_beans=10,
        max_stack=10,
        max_uses_per_day=1,
    )

    DICTIONARY = ShopItem(
        key="dictionary",
        name="📚 Dictionary",
        description="A proper dictionary. Heavy enough to press flowers, useful enough to actually open.",
        cost_beans=15,
        max_stack=5,
        max_uses_per_day=1,
    )

    FLUENCY_BADGE = ShopItem(
        key="fluency_badge",
        name="🏅 Fluency Badge",
        description="A badge that says you showed up and kept going. Wear it with pride.",
        cost_beans=20,
        max_stack=1,
        max_uses_per_day=1,
    )

    @classmethod
    def all(cls) -> Dict[str, ShopItem]:
        return {
            cls.COOKIE.key: cls.COOKIE,
            cls.COFFEE.key: cls.COFFEE,
            cls.TEA.key: cls.TEA,
            cls.STROOPWAFEL.key: cls.STROOPWAFEL,
            cls.JUICE.key: cls.JUICE,
            cls.PHRASEBOOK.key: cls.PHRASEBOOK,
            cls.FLASHCARDS.key: cls.FLASHCARDS,
            cls.DICTIONARY.key: cls.DICTIONARY,
            cls.FLUENCY_BADGE.key: cls.FLUENCY_BADGE,
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
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class ShopItem:
    key: str
    name: str
    description: str
    cost_beans: int
    max_stack: int
    max_uses_per_day: int

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
    English shop catalog (seed data for English server).
    """

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
        for item in cls.list():
            await shop_repo.upsert_item(
                item_key=item.key,
                name=item.name,
                description=item.description,
                price=int(item.cost_beans),
                max_use_per_day=int(item.max_uses_per_day),
                max_inventory=int(item.max_stack),
            )


class DutchShopItems:
    """
    Dutch shop catalog (seed data for Dutch server).
    Keys are prefixed with nl_ to avoid collisions with English items in the DB.
    """

    STROOPWAFEL = ShopItem(
        key="nl_stroopwafel",
        name="🧇 Stroopwafel",
        description="Een echte Nederlandse klassieker. Leg hem op je koffie en wacht geduldig. Geduld is ook een taalvaardigheid.",
        cost_beans=3,
        max_stack=30,
        max_uses_per_day=3,
    )

    HAGELSLAG = ShopItem(
        key="nl_hagelslag",
        name="🍫 Hagelslag",
        description="Chocoladehagelslag op wit brood. Nederlanders eten dit echt als lunch. Welkom in Nederland.",
        cost_beans=3,
        max_stack=30,
        max_uses_per_day=3,
    )

    DROP = ShopItem(
        key="nl_drop",
        name="🖤 Dropje",
        description="Zoet, zout of dubbelzout — drop is niet voor iedereen. Maar als je het lekker vindt, ben je écht ingeburgerd.",
        cost_beans=4,
        max_stack=25,
        max_uses_per_day=3,
    )

    KOFFIE = ShopItem(
        key="nl_koffie",
        name="☕ Koffie",
        description="Een bakje troost. Sterk, zwart, en geserveerd met een koekje dat je eigenlijk niet mag eten.",
        cost_beans=3,
        max_stack=25,
        max_uses_per_day=3,
    )

    BESCHUIT = ShopItem(
        key="nl_beschuit",
        name="🥐 Beschuit met muisjes",
        description="Roze of blauw — traditioneel voor een geboorte. Maar jij verdient hem gewoon omdat je Nederlands oefent.",
        cost_beans=5,
        max_stack=20,
        max_uses_per_day=2,
    )

    KAASJE = ShopItem(
        key="nl_kaasje",
        name="🧀 Kaasje",
        description="Een plakje gouda. Nederland exporteert kaas én taal. Jij importeert allebei.",
        cost_beans=5,
        max_stack=20,
        max_uses_per_day=2,
    )

    WOORDENBOEK = ShopItem(
        key="nl_woordenboek",
        name="📚 Woordenboek",
        description="Een echt Nederlands woordenboek. Dik, zwaar, en vol woorden die je nog niet kent. Dat is het punt.",
        cost_beans=15,
        max_stack=5,
        max_uses_per_day=1,
    )

    WOORDKAARTJES = ShopItem(
        key="nl_woordkaartjes",
        name="🗂️ Woordkaartjes",
        description="Een stapel woordkaartjes om nieuwe woorden te oefenen. De vorige stapel ligt ergens onder je bureau.",
        cost_beans=10,
        max_stack=10,
        max_uses_per_day=1,
    )

    SPREEKBADGE = ShopItem(
        key="nl_spreekbadge",
        name="🏅 Spreekbadge",
        description="Een badge voor iedereen die blijft oefenen, ook als het moeilijk is. Draag hem met trots.",
        cost_beans=20,
        max_stack=1,
        max_uses_per_day=1,
    )

    FIETS = ShopItem(
        key="nl_fiets",
        name="🚲 Fiets",
        description="Niet echt een fiets. Maar symbolisch: je gaat vooruit. Op je eigen tempo, zonder helm.",
        cost_beans=25,
        max_stack=1,
        max_uses_per_day=1,
    )

    @classmethod
    def all(cls) -> Dict[str, ShopItem]:
        return {
            cls.STROOPWAFEL.key: cls.STROOPWAFEL,
            cls.HAGELSLAG.key: cls.HAGELSLAG,
            cls.DROP.key: cls.DROP,
            cls.KOFFIE.key: cls.KOFFIE,
            cls.BESCHUIT.key: cls.BESCHUIT,
            cls.KAASJE.key: cls.KAASJE,
            cls.WOORDENBOEK.key: cls.WOORDENBOEK,
            cls.WOORDKAARTJES.key: cls.WOORDKAARTJES,
            cls.SPREEKBADGE.key: cls.SPREEKBADGE,
            cls.FIETS.key: cls.FIETS,
        }

    @classmethod
    def list(cls) -> List[ShopItem]:
        return list(cls.all().values())

    @classmethod
    def get(cls, key: str) -> ShopItem | None:
        return cls.all().get((key or "").strip().lower())

    @classmethod
    async def seed_defaults(cls, *, shop_repo) -> None:
        for item in cls.list():
            await shop_repo.upsert_item(
                item_key=item.key,
                name=item.name,
                description=item.description,
                price=int(item.cost_beans),
                max_use_per_day=int(item.max_uses_per_day),
                max_inventory=int(item.max_stack),
            )
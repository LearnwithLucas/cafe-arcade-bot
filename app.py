from __future__ import annotations

import asyncio
from pathlib import Path

from src.config.settings import Settings
from src.logging.setup import setup_logging

from src.db.connection import Database
from src.db.migrations import run_migrations

from src.db.repo.users_repo import UsersRepository
from src.db.repo.economy_repo import EconomyRepository
from src.db.repo.leaderboard_repo import LeaderboardRepository
from src.db.repo.leaderboard_posts_repo import LeaderboardPostsRepository
from src.db.repo.games_repo import GamesRepository

# --- Shop (NEW) ---
from src.db.repo.shop_repo import ShopRepository
from src.services.shop_service import ShopService
from src.services.shop_items import ShopItems

from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.game_registry import GameRegistry
from src.services.leaderboard_service import LeaderboardService
from src.services.leaderboard_publisher import LeaderboardPublisher, LeaderboardConfig
from src.services.rewards_service import RewardsService
from src.services.wordlist import WordList

from src.games.english.word_chain import WordChainGame
from src.games.english.wordle import WordleGame
from src.games.english.unscramble import UnscrambleGame

# --- GeoGuessr Learning (Flags & Scripts) ---
from src.services.geo_learning_bank import GeoLearningBank
from src.games.geoguessr.learning import GeoLearningGame

# --- GeoGuessr Arcade Games (Flags & Language) ---
from src.services.geo_quiz_bank import GeoQuizBank
from src.games.geoguessr.flags_game import GeoFlagsGame
from src.games.geoguessr.language_game import GeoLanguageGame

from src.platforms.discord.bot import build_discord_bot


# ---- Channel IDs (Discord) ----
WORD_CHAIN_CHANNEL_ID = 1458083222650097705
WORDLE_CHANNEL_ID = 1458083142211731547        # 🔤┃wordle
UNSCRAMBLE_CHANNEL_ID = 1458083175375831172    # 🔤┃unscramble
LEADERBOARD_CHANNEL_ID = 1458088882749968535

# Geo-learning channel (set to your real geo-learning channel id)
# Use 0 to disable geo-learning registration.
GEO_LEARNING_CHANNEL_ID = 0  # e.g. 123456789012345678

# GeoGuessr arcade channels (Flags / Language)
# Use 0 to disable registration until you set real IDs.
GEO_FLAGS_CHANNEL_ID = 1463481092966453343
GEO_LANGUAGE_CHANNEL_ID = 1463481137333796948

# ---- Assets ----
WORDS_TXT_PATH = Path("src/assets/words_en.txt")


async def main() -> None:
    settings = Settings.load()
    setup_logging(settings)

    # --- DB ---
    db = Database(settings.db_path)
    await db.connect()
    await run_migrations(db)

    # --- Load word list once ---
    wordlist = WordList.load_from_txt(WORDS_TXT_PATH)

    # --- Repositories ---
    users_repo = UsersRepository(db)
    economy_repo = EconomyRepository(db)
    leaderboard_repo = LeaderboardRepository(db)
    leaderboard_posts_repo = LeaderboardPostsRepository(db)
    games_repo = GamesRepository(db)

    # --- Shop repo (NEW) ---
    shop_repo = ShopRepository(db)

    # --- Leaderboard publisher (Discord channel auto-updates) ---
    english_game_keys = [
        "word_chain",
        "wordle",
        "unscramble",
    ]

    leaderboard_publisher = LeaderboardPublisher(
        config=LeaderboardConfig(
            platform="discord",
            channel_id=LEADERBOARD_CHANNEL_ID,
            english_game_keys=english_game_keys,
            limit=10,
            debounce_seconds=10.0,
        ),
        leaderboard_repo=leaderboard_repo,
        posts_repo=leaderboard_posts_repo,
    )

    # --- Services ---
    rewards = RewardsService()

    economy_service = EconomyService(
        users_repo=users_repo,
        economy_repo=economy_repo,
        leaderboard_publisher=leaderboard_publisher,
    )

    leaderboard_service = LeaderboardService(
        users_repo=users_repo,
        leaderboard_repo=leaderboard_repo,
    )

    cooldowns = Cooldowns()

    # --- Shop service (NEW) ---
    shop_service = ShopService(
        users_repo=users_repo,
        economy=economy_service,
        shop_repo=shop_repo,
    )

    # --- Seed shop catalog (idempotent, DB-backed; safe on Render restarts) ---
    # This requires migration v4 (shop_items table). If it fails, bot still runs.
    try:
        for it in ShopItems.all().values():
            await shop_repo.upsert_item(
                item_key=it.key,
                name=it.name,
                description=it.description,
                price=int(it.cost_beans),
                max_use_per_day=int(it.max_uses_per_day),
                max_inventory=int(it.max_stack),
            )
    except Exception:
        # Keep startup resilient on first deploy / partial DB states.
        pass

    game_registry = GameRegistry()

    # --- Games: Word Chain ---
    word_chain = WordChainGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist,
        allowed_channel_ids={WORD_CHAIN_CHANNEL_ID},
        leaderboard_publisher=leaderboard_publisher,
    )
    game_registry.register(word_chain)

    # --- Games: Wordle ---
    wordle = WordleGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist,
        allowed_channel_ids={WORDLE_CHANNEL_ID},
        leaderboard_publisher=leaderboard_publisher,
    )
    game_registry.register(wordle)

    # --- Games: Unscramble ---
    unscramble = UnscrambleGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist,
        allowed_channel_ids={UNSCRAMBLE_CHANNEL_ID},
        leaderboard_publisher=leaderboard_publisher,
    )
    game_registry.register(unscramble)

    # --- Games: Geo Learning (Flags & Scripts) ---
    geo_learning_bank = GeoLearningBank()
    geo_learning = None
    if int(GEO_LEARNING_CHANNEL_ID) > 0:
        geo_learning = GeoLearningGame(
            bank=geo_learning_bank,
            allowed_channel_ids={int(GEO_LEARNING_CHANNEL_ID)},
        )
        game_registry.register(geo_learning)

    # --- GeoGuessr arcade: shared dataset bank (Render-safe, local JSON + fallback) ---
    geo_quiz_bank = GeoQuizBank.load_from_assets()

    geo_flags = None
    if int(GEO_FLAGS_CHANNEL_ID) > 0:
        geo_flags = GeoFlagsGame(
            games_repo=games_repo,
            users_repo=users_repo,
            economy=economy_service,
            rewards=rewards,
            cooldowns=cooldowns,
            bank=geo_quiz_bank,
            allowed_channel_ids={int(GEO_FLAGS_CHANNEL_ID)},
            leaderboard_publisher=leaderboard_publisher,
        )
        game_registry.register(geo_flags)

    geo_language = None
    if int(GEO_LANGUAGE_CHANNEL_ID) > 0:
        geo_language = GeoLanguageGame(
            games_repo=games_repo,
            users_repo=users_repo,
            economy=economy_service,
            rewards=rewards,
            cooldowns=cooldowns,
            bank=geo_quiz_bank,
            allowed_channel_ids={int(GEO_LANGUAGE_CHANNEL_ID)},
            leaderboard_publisher=leaderboard_publisher,
        )
        game_registry.register(geo_language)

    # --- DI container ---
    services = {
        "db": db,
        "users_repo": users_repo,
        "economy": economy_service,
        "rewards": rewards,
        "cooldowns": cooldowns,
        "leaderboard": leaderboard_service,
        "leaderboard_publisher": leaderboard_publisher,
        "leaderboard_repo": leaderboard_repo,
        "leaderboard_posts_repo": leaderboard_posts_repo,
        "games_repo": games_repo,
        "game_registry": game_registry,
        "wordlist": wordlist,
        "word_chain": word_chain,
        "wordle": wordle,
        "unscramble": unscramble,
        # shop (NEW)
        "shop_repo": shop_repo,
        "shop": shop_service,
        # geo learning
        "geo_learning_bank": geo_learning_bank,
        "geo_learning": geo_learning,
        # geo arcade
        "geo_quiz_bank": geo_quiz_bank,
        "geo_flags": geo_flags,
        "geo_language": geo_language,
    }

    # --- Discord bot ---
    discord_bot = build_discord_bot(settings=settings, services=services)

    try:
        await discord_bot.start(settings.discord_token)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

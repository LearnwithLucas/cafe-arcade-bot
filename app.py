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
from src.db.repo.shop_repo import ShopRepository

from src.services.shop_service import ShopService
from src.services.shop_items import ShopItems, DutchShopItems
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

from src.services.geo_learning_bank import GeoLearningBank
from src.games.geoguessr.learning import GeoLearningGame

from src.services.geo_quiz_bank import GeoQuizBank
from src.games.geoguessr.flags_game import GeoFlagsGame
from src.games.geoguessr.language_game import GeoLanguageGame

from src.games.dutch.wordle_nl import DutchWordleGame
from src.games.dutch.unscramble_nl import DutchUnscrambleGame
from src.games.dutch.word_chain_nl import DutchWordChainGame
from src.games.dutch.niet_geen import NietGeenGame

from src.platforms.discord.bot import build_discord_bot


# ---- Channel IDs (Discord — English server) ----
WORD_CHAIN_CHANNEL_ID = 1481745881123520573
WORDLE_CHANNEL_ID = 1481745735652474920
UNSCRAMBLE_CHANNEL_ID = 1481745817021845607
LEADERBOARD_CHANNEL_ID = 1481746468737126564
GEO_LEARNING_CHANNEL_ID = 0
GEO_FLAGS_CHANNEL_ID = 1481763185668395263
GEO_LANGUAGE_CHANNEL_ID = 1481763326164865087

# ---- Channel IDs (Discord — Dutch server) ----
DUTCH_WORDLE_CHANNEL_ID = 1482763022173995119
DUTCH_UNSCRAMBLE_CHANNEL_ID = 1482763069238153419
DUTCH_WORD_CHAIN_CHANNEL_ID = 1482763114842816765

# ---- Assets ----
WORDS_TXT_PATH = Path("src/assets/words_en.txt")
WORDS_NL_TXT_PATH = Path("src/assets/words_nl.txt")
WORDS_NL_COMPLEET_PATH = Path("src/assets/words_nl_compleet.txt")

async def main() -> None:
    settings = Settings.load()
    setup_logging(settings)

    # --- DB ---
    db = Database(settings.db_path)
    await db.connect()
    await run_migrations(db)

    # --- Load word list ---
    wordlist = WordList.load_from_txt(WORDS_TXT_PATH)

    # --- Load Dutch word list ---
    wordlist_nl = WordList.load_from_txt(WORDS_NL_TXT_PATH)

    # --- Load Dutch complete word list (for Woordketting) ---
    wordlist_nl_compleet = WordList.load_from_txt(WORDS_NL_COMPLEET_PATH)

    # --- Repositories ---
    users_repo = UsersRepository(db)
    economy_repo = EconomyRepository(db)
    leaderboard_repo = LeaderboardRepository(db)
    leaderboard_posts_repo = LeaderboardPostsRepository(db)
    games_repo = GamesRepository(db)
    shop_repo = ShopRepository(db)

    # --- English leaderboard publisher ---
    english_game_keys = ["word_chain", "wordle", "unscramble"]
    dutch_game_keys = ["word_chain_nl", "wordle_nl", "unscramble_nl"]

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

    # --- Dutch leaderboard publisher (#voortgang) ---
    dutch_leaderboard_publisher = None
    if settings.dutch_guild_id and settings.dutch_channel_progress:
        dutch_leaderboard_publisher = LeaderboardPublisher(
            config=LeaderboardConfig(
                platform="discord",
                channel_id=int(settings.dutch_channel_progress),
                english_game_keys=dutch_game_keys,
                limit=10,
                debounce_seconds=10.0,
                board_key="dutch_dropdown_v1",
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

    # --- English shop service ---
    shop_service = ShopService(
        users_repo=users_repo,
        economy=economy_service,
        shop_repo=shop_repo,
    )

    # --- Dutch shop service (separate ShopService instance, same repo) ---
    dutch_shop_service = ShopService(
        users_repo=users_repo,
        economy=economy_service,
        shop_repo=shop_repo,
    )

    # --- Seed English shop catalog ---
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
        pass

    # --- Seed Dutch shop catalog ---
    try:
        for it in DutchShopItems.all().values():
            await shop_repo.upsert_item(
                item_key=it.key,
                name=it.name,
                description=it.description,
                price=int(it.cost_beans),
                max_use_per_day=int(it.max_uses_per_day),
                max_inventory=int(it.max_stack),
            )
    except Exception:
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

    # --- Games: Dutch Wordle ---
    wordle_nl = DutchWordleGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist_nl,
        allowed_channel_ids={DUTCH_WORDLE_CHANNEL_ID},
        leaderboard_publisher=dutch_leaderboard_publisher,
    )
    game_registry.register(wordle_nl)

    # --- Games: Dutch Unscramble ---
    unscramble_nl = DutchUnscrambleGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist_nl,
        allowed_channel_ids={DUTCH_UNSCRAMBLE_CHANNEL_ID},
        leaderboard_publisher=dutch_leaderboard_publisher,
    )
    game_registry.register(unscramble_nl)

    # --- Games: Dutch Word Chain ---
    word_chain_nl = DutchWordChainGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        cooldowns=cooldowns,
        wordlist=wordlist_nl_compleet,
        allowed_channel_ids={DUTCH_WORD_CHAIN_CHANNEL_ID},
        leaderboard_publisher=dutch_leaderboard_publisher,
    )
    game_registry.register(word_chain_nl)

    # --- Games: Niet vs Geen ---
    niet_geen_game = NietGeenGame(
        games_repo=games_repo,
        users_repo=users_repo,
        economy=economy_service,
        rewards=rewards,
        allowed_channel_ids={1487175077702275273},
    )

    # --- Games: Geo Learning ---
    geo_learning_bank = GeoLearningBank()
    geo_learning = None
    if int(GEO_LEARNING_CHANNEL_ID) > 0:
        geo_learning = GeoLearningGame(
            bank=geo_learning_bank,
            allowed_channel_ids={int(GEO_LEARNING_CHANNEL_ID)},
        )
        game_registry.register(geo_learning)

    # --- GeoGuessr arcade ---
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
        "dutch_leaderboard_publisher": dutch_leaderboard_publisher,
        "leaderboard_repo": leaderboard_repo,
        "leaderboard_posts_repo": leaderboard_posts_repo,
        "games_repo": games_repo,
        "game_registry": game_registry,
        "wordlist": wordlist,
        "word_chain": word_chain,
        "wordle": wordle,
        "unscramble": unscramble,
        # English shop
        "shop_repo": shop_repo,
        "shop": shop_service,
        # Dutch shop
        "dutch_shop": dutch_shop_service,
        # Dutch games
        "wordle_nl": wordle_nl,
        "unscramble_nl": unscramble_nl,
        "word_chain_nl": word_chain_nl,
        "niet_geen": niet_geen_game,
        # geo
        "geo_learning_bank": geo_learning_bank,
        "geo_learning": geo_learning,
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
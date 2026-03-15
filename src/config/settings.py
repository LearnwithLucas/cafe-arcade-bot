from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """
    Global application settings loaded from environment variables.

    This class should remain dependency-free and side-effect free
    except for loading environment variables.
    """

    # Environment
    env: str
    log_level: str

    # Discord
    discord_token: str
    discord_guild_id: int | None

    # Dutch Discord server
    dutch_guild_id: int | None
    dutch_channel_beans: int | None
    dutch_channel_beans_help: int | None
    dutch_channel_beans_shop: int | None
    dutch_channel_progress: int | None
    dutch_channel_wordle: int | None
    dutch_channel_unscramble: int | None
    dutch_channel_word_chain: int | None

    # Telegram (optional for now)
    telegram_token: str | None

    # Database
    db_path: Path

    @classmethod
    def load(cls) -> "Settings":
        """
        Load settings from environment variables.
        """

        # Load .env for local development (noop in Render)
        load_dotenv()

        env = os.getenv("ENV", "development")
        log_level = os.getenv("LOG_LEVEL", "INFO")

        discord_token = os.getenv("DISCORD_TOKEN")
        if not discord_token:
            raise RuntimeError("DISCORD_TOKEN is required")

        def opt_int(name: str) -> int | None:
            raw = os.getenv(name, "").strip()
            return int(raw) if raw else None

        discord_guild_id = opt_int("DISCORD_GUILD_ID")

        # Dutch server
        dutch_guild_id = opt_int("DUTCH_GUILD_ID")
        dutch_channel_beans = opt_int("DUTCH_CHANNEL_BEANS")
        dutch_channel_beans_help = opt_int("DUTCH_CHANNEL_BEANS_HELP")
        dutch_channel_beans_shop = opt_int("DUTCH_CHANNEL_BEANS_SHOP")
        dutch_channel_progress = opt_int("DUTCH_CHANNEL_PROGRESS")
        dutch_channel_wordle = opt_int("DUTCH_CHANNEL_WORDLE")
        dutch_channel_unscramble = opt_int("DUTCH_CHANNEL_UNSCRAMBLE")
        dutch_channel_word_chain = opt_int("DUTCH_CHANNEL_WORD_CHAIN")

        telegram_token = os.getenv("TELEGRAM_TOKEN")

        db_path_raw = os.getenv("DB_PATH", "./data/arcade.sqlite")
        db_path = Path(db_path_raw)

        # Ensure parent directory exists (safe on Render & local)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            env=env,
            log_level=log_level,
            discord_token=discord_token,
            discord_guild_id=discord_guild_id,
            dutch_guild_id=dutch_guild_id,
            dutch_channel_beans=dutch_channel_beans,
            dutch_channel_beans_help=dutch_channel_beans_help,
            dutch_channel_beans_shop=dutch_channel_beans_shop,
            dutch_channel_progress=dutch_channel_progress,
            dutch_channel_wordle=dutch_channel_wordle,
            dutch_channel_unscramble=dutch_channel_unscramble,
            dutch_channel_word_chain=dutch_channel_word_chain,
            telegram_token=telegram_token,
            db_path=db_path,
        )
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

        discord_guild_id_raw = os.getenv("DISCORD_GUILD_ID")
        discord_guild_id = (
            int(discord_guild_id_raw) if discord_guild_id_raw else None
        )

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
            telegram_token=telegram_token,
            db_path=db_path,
        )

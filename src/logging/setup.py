from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.settings import Settings


def setup_logging(settings: "Settings") -> None:
    """
    Configure application-wide logging.

    - Uses stdout (Render-friendly)
    - Avoids duplicate handlers
    - Sets sane defaults for noisy third-party libs
    """

    # Normalize level
    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()

    # If handlers already exist (e.g., hot reload / tests), don't double-add
    if root.handlers:
        root.setLevel(level)
        return

    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Reduce noise from common libraries
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # If you later enable Telegram:
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

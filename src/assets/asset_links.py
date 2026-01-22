from __future__ import annotations


class AssetLinks:
    """
    Central catalog for externally hosted assets (images, icons, banners).

    Goals:
      - Keep URLs out of command/game logic
      - Make visual changes easy (edit once, propagate everywhere)
      - Provide stable semantic names for assets

    Usage example:
      embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
    """

    # =========================================================
    # PRIMARY (CURRENTLY USED)
    # =========================================================
    # Your bean currency image (used in /core embeds)
    BEAN_CURRENCY_ICON = "https://learnwithlucas.com/wp-content/uploads/2025/10/bean_currency.png"

    # If you don't have per-game icons yet, re-use the bean icon so embeds never break.
    # You can swap these later without touching any code.
    # =========================================================

    # =====================
    # ENGLISH GAMES
    # =====================
    WORDLE_ICON = BEAN_CURRENCY_ICON
    UNSCRAMBLE_ICON = BEAN_CURRENCY_ICON
    WORD_CHAIN_ICON = BEAN_CURRENCY_ICON

    # =====================
    # GEOGUESSR / GEO ARCADE
    # =====================
    GEO_FLAGS_ICON = BEAN_CURRENCY_ICON
    GEO_LANGUAGE_ICON = BEAN_CURRENCY_ICON
    GEO_WORLD_ICON = BEAN_CURRENCY_ICON

    # =====================
    # CORE / SYSTEM
    # =====================
    DAILY_REWARD_ICON = BEAN_CURRENCY_ICON
    WORK_REWARD_ICON = BEAN_CURRENCY_ICON
    SHOP_ICON = BEAN_CURRENCY_ICON
    HELP_ICON = BEAN_CURRENCY_ICON

    # =====================
    # LEADERBOARD
    # =====================
    LEADERBOARD_ICON = BEAN_CURRENCY_ICON

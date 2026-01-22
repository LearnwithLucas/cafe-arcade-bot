from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from src.assets.asset_links import AssetLinks
from src.platforms.discord.geoguessr_commands import GeoGuessrCommands
from src.platforms.discord.geoguessr_learning_commands import GeoLearningCommands
from src.platforms.discord.inventory_commands import InventoryCommands
from src.platforms.discord.shop_commands import ShopCommands
from src.services.cooldowns import Cooldowns
from src.services.rewards_service import RewardKey, RewardsService

logger = logging.getLogger(__name__)


# =====================
# CHANNEL IDS (DISCORD)
# =====================
BEAN_COUNTER_CHANNEL_ID = 1454125725467541628  # 🧮┃bean-counter  (daily/work)
BEAN_HELP_CHANNEL_ID = 1454125616558506158  # ❓┃bean-help     (info)
BEAN_SHOP_CHANNEL_ID = 1454125852827582520  # 🛍️┃bean-shop     (shop)


def _has_service(services: dict[str, Any], key: str) -> bool:
    return services.get(key) is not None


# =====================
# CORE COMMANDS
# =====================
class CoreCommands(app_commands.Group):
    def __init__(self, bot: discord.Client, services: dict[str, Any]) -> None:
        super().__init__(name="core", description="Core bot commands")
        self.bot = bot
        self.services = services

    # -------------
    # Helpers
    # -------------

    def _in_channel(self, interaction: discord.Interaction, channel_id: int) -> bool:
        return int(getattr(interaction, "channel_id", 0) or 0) == int(channel_id)

    def _get_rewards(self) -> RewardsService | None:
        r = self.services.get("rewards")
        return r if isinstance(r, RewardsService) else None

    def _get_cooldowns(self) -> Cooldowns | None:
        c = self.services.get("cooldowns")
        return c if isinstance(c, Cooldowns) else None

    def _bean_embed(self, *, title: str, description: str) -> discord.Embed:
        embed = discord.Embed(title=title, description=description)
        embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
        return embed

    # -------------
    # Commands
    # -------------

    @app_commands.command(name="ping", description="Check if the bot is alive")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("🏓 Pong!", ephemeral=True)

    @app_commands.command(name="beans", description="Show your current bean balance")
    async def beans(self, interaction: discord.Interaction) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
        )
        await interaction.response.send_message(f"☕ You have **{balance}** beans.", ephemeral=True)

    @app_commands.command(name="wallet", description="Show your current bean balance (alias of /core beans)")
    async def wallet(self, interaction: discord.Interaction) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
        )
        await interaction.response.send_message(f"👛 Wallet: **{balance}** beans.", ephemeral=True)

    @app_commands.command(name="daily", description="Claim your daily beans (once per day)")
    async def daily(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_COUNTER_CHANNEL_ID):
            await interaction.response.send_message(f"Use this in <#{BEAN_COUNTER_CHANNEL_ID}> 🧮", ephemeral=True)
            return

        rewards = self._get_rewards()
        if rewards is None:
            await interaction.response.send_message("Rewards service not available.", ephemeral=True)
            return

        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        cooldowns = self._get_cooldowns()
        if cooldowns is None:
            await interaction.response.send_message("Cooldowns service not available.", ephemeral=True)
            return

        location_id = str(interaction.guild_id or interaction.channel_id)
        allowed, remaining = cooldowns.try_acquire(
            action="core.daily",
            user_id=interaction.user.id,
            location_id=location_id,
            cooldown_seconds=24 * 60 * 60,
        )
        if not allowed:
            hours = remaining // 3600
            mins = (remaining % 3600) // 60
            embed = self._bean_embed(title="⏳ Daily already claimed", description=f"Try again in **{hours}h {mins}m**.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        amount = int(rewards.amount(RewardKey.CORE_DAILY))
        new_balance = await economy.award_beans_discord(
            user_id=interaction.user.id,
            amount=amount,
            reason="Daily claim",
            game_key="core",
            display_name=interaction.user.display_name,
        )

        embed = self._bean_embed(
            title="🌞 Daily claimed!",
            description=(f"<@{interaction.user.id}> earned **{amount} beans**.\n👛 New balance: **{new_balance}** beans."),
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="work", description="Work for beans (once per hour)")
    async def work(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_COUNTER_CHANNEL_ID):
            await interaction.response.send_message(f"Use this in <#{BEAN_COUNTER_CHANNEL_ID}> 🧮", ephemeral=True)
            return

        rewards = self._get_rewards()
        if rewards is None:
            await interaction.response.send_message("Rewards service not available.", ephemeral=True)
            return

        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        cooldowns = self._get_cooldowns()
        if cooldowns is None:
            await interaction.response.send_message("Cooldowns service not available.", ephemeral=True)
            return

        location_id = str(interaction.guild_id or interaction.channel_id)
        allowed, remaining = cooldowns.try_acquire(
            action="core.work",
            user_id=interaction.user.id,
            location_id=location_id,
            cooldown_seconds=60 * 60,
        )
        if not allowed:
            mins = remaining // 60
            secs = remaining % 60
            embed = self._bean_embed(title="⏳ Too soon to work again", description=f"Try again in **{mins}m {secs}s**.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        amount = int(rewards.amount(RewardKey.CORE_WORK))
        new_balance = await economy.award_beans_discord(
            user_id=interaction.user.id,
            amount=amount,
            reason="Work payout",
            game_key="core",
            display_name=interaction.user.display_name,
        )

        embed = self._bean_embed(
            title="🛠️ Work complete!",
            description=(f"<@{interaction.user.id}> earned **{amount} beans**.\n👛 New balance: **{new_balance}** beans."),
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="help", description="Info about beans, earning, and channels")
    async def help(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_HELP_CHANNEL_ID):
            await interaction.response.send_message(f"Use this in <#{BEAN_HELP_CHANNEL_ID}> ❓", ephemeral=True)
            return

        rewards = self._get_rewards()

        def amt(key: str, fallback: int) -> int:
            try:
                return int(rewards.amount(key)) if rewards else int(fallback)
            except Exception:
                return int(fallback)

        embed = self._bean_embed(
            title="☕ Beans — Help",
            description=(
                "Beans are earned by playing games and using the bean-counter commands.\n\n"
                f"**Where to use commands**\n"
                f"• 🧮 Bean Counter: <#{BEAN_COUNTER_CHANNEL_ID}> (`/core daily`, `/core work`)\n"
                f"• ❓ Bean Help: <#{BEAN_HELP_CHANNEL_ID}>\n"
                f"• 🛍️ Bean Shop: <#{BEAN_SHOP_CHANNEL_ID}> (`/shop`)\n\n"
                "**Current rewards**\n"
                f"• Wordle solve: **{amt(RewardKey.WORDLE_SOLVE, 20)}**\n"
                f"• Wordle fail: **{amt(RewardKey.WORDLE_FAIL_PER_GREEN, 2)}× best 🟩**\n"
                f"• Unscramble solve: **{amt(RewardKey.UNSCRAMBLE_SOLVE, 5)}**\n"
                f"• Unscramble fail: **{amt(RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED, 2)}× revealed letters**\n"
                f"• Word Chain: **{amt(RewardKey.WORD_CHAIN_ROUND_PAYOUT, 1)}× accepted words**\n"
                f"• Daily claim: **{amt(RewardKey.CORE_DAILY, 25)}** (once/day)\n"
                f"• Work: **{amt(RewardKey.CORE_WORK, 5)}** (once/hour)\n\n"
                "**GeoGuessr**\n"
                f"• Flags correct: **{amt(RewardKey.GEO_FLAGS_CORRECT, 2)}** (+streak bonus)\n"
                f"• Language correct: **{amt(RewardKey.GEO_LANGUAGE_CORRECT, 2)}** (+streak bonus)\n"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="shop", description="(Deprecated) Use /shop")
    async def shop(self, interaction: discord.Interaction) -> None:
        # NOTE: This is /core shop (not /shop). Keeping it as a guide is fine.
        embed = self._bean_embed(
            title="🛍️ Bean Shop",
            description=("Use the shop command:\n" "• `/shop`\n\n" f"Shop channel: <#{BEAN_SHOP_CHANNEL_ID}>"),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="Refresh the leaderboard panel (Today view)")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        publisher = self.services.get("leaderboard_publisher")
        if not publisher:
            await interaction.response.send_message("Leaderboard is not available.", ephemeral=True)
            return

        await interaction.response.send_message("Refreshing leaderboard…", ephemeral=True)
        await publisher.refresh_now(default_tab="today")
        await interaction.followup.send("✅ Leaderboard updated.", ephemeral=True)


# =====================
# GAMES COMMANDS
# =====================
class GamesCommands(app_commands.Group):
    def __init__(self, bot: discord.Client, services: dict[str, Any]) -> None:
        super().__init__(name="games", description="Game commands")
        self.bot = bot
        self.services = services

    @app_commands.command(name="wordchain_start", description="Start Word Chain in this channel")
    async def wordchain_start(self, interaction: discord.Interaction) -> None:
        word_chain = self.services.get("word_chain")
        if not word_chain:
            await interaction.response.send_message("Word Chain is not available.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔤 Word Chain",
            description=(
                "**Rules**\n"
                "• Send an English word\n"
                "• Next word must start with the **last letter**\n"
                "• No repeats\n"
                "• One mistake ends the round\n\n"
                "**Write your first word:**"
            ),
        )
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await word_chain.start_in_channel(channel_id=interaction.channel_id, status_message_id=msg.id)

    @app_commands.command(name="wordle_start", description="Start today’s Wordle")
    async def wordle_start(self, interaction: discord.Interaction) -> None:
        wordle = self.services.get("wordle")
        if not wordle:
            await interaction.response.send_message("Wordle is not available.", ephemeral=True)
            return

        await wordle.start_in_channel(channel_id=interaction.channel_id)

        embed = discord.Embed(
            title="🧩 Wordle — Daily",
            description=(
                "• 5-letter words\n"
                "• 12 guesses\n\n"
                "🟩 correct spot\n"
                "🟨 wrong spot\n"
                "🟥 not in word\n\n"
                "**Hint:** `/games wordle_hint` (once)\n\n"
                "**Type your first guess:**"
            ),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wordle_hint", description="Use your one Wordle hint")
    async def wordle_hint(self, interaction: discord.Interaction) -> None:
        wordle = self.services.get("wordle")
        if not wordle:
            await interaction.response.send_message("Wordle is not available.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, msg = await wordle.use_hint(
            channel=interaction.channel,
            channel_id=interaction.channel_id,
            player_id=interaction.user.id,
        )
        await interaction.followup.send(("💡 " if ok else "❌ ") + msg, ephemeral=True)

    @app_commands.command(name="wordle_restart", description="Restart Wordle in this channel")
    async def wordle_restart(self, interaction: discord.Interaction) -> None:
        wordle = self.services.get("wordle")
        if not wordle:
            await interaction.response.send_message("Wordle is not available.", ephemeral=True)
            return

        await wordle.restart_in_channel(channel_id=interaction.channel_id)
        await interaction.response.send_message("🔁 Wordle restarted.", ephemeral=False)

    @app_commands.command(name="unscramble_start", description="Start Unscramble (creates your own panel)")
    async def unscramble_start(self, interaction: discord.Interaction) -> None:
        unscramble = self.services.get("unscramble")
        if not unscramble:
            await interaction.response.send_message("Unscramble is not available.", ephemeral=True)
            return

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Unscramble can only be used in server text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await unscramble.start_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🔀 Unscramble started! Check the channel for your puzzle panel.", ephemeral=True)

    @app_commands.command(name="unscramble_hint", description="Reveal the first letter (once per round)")
    async def unscramble_hint(self, interaction: discord.Interaction) -> None:
        unscramble = self.services.get("unscramble")
        if not unscramble:
            await interaction.response.send_message("Unscramble is not available.", ephemeral=True)
            return

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Unscramble can only be used in server text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await unscramble.hint_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("💡 Hint applied (if you had an active round).", ephemeral=True)

    @app_commands.command(name="unscramble_stop", description="Stop your current Unscramble round")
    async def unscramble_stop(self, interaction: discord.Interaction) -> None:
        unscramble = self.services.get("unscramble")
        if not unscramble:
            await interaction.response.send_message("Unscramble is not available.", ephemeral=True)
            return

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Unscramble can only be used in server text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await unscramble.stop_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🛑 Unscramble stopped (your active round was cleared).", ephemeral=True)

    @app_commands.command(name="unscramble_restart", description="Restart your Unscramble round (new word)")
    async def unscramble_restart(self, interaction: discord.Interaction) -> None:
        unscramble = self.services.get("unscramble")
        if not unscramble:
            await interaction.response.send_message("Unscramble is not available.", ephemeral=True)
            return

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Unscramble can only be used in server text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await unscramble.start_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🔁 Unscramble restarted! New puzzle panel posted/updated.", ephemeral=True)

    @app_commands.command(name="unscramble_help", description="Show Unscramble rules")
    async def unscramble_help(self, interaction: discord.Interaction) -> None:
        rewards = self.services.get("rewards")
        solve_amt = 5
        fail_mult = 2
        try:
            if isinstance(rewards, RewardsService):
                solve_amt = int(rewards.amount(RewardKey.UNSCRAMBLE_SOLVE))
                fail_mult = int(rewards.amount(RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED))
        except Exception:
            pass

        embed = discord.Embed(
            title="🔀 Unscramble — Help",
            description=(
                "Unscramble the scrambled word.\n\n"
                "**Rules**\n"
                "• 3 guesses max\n"
                "• Each wrong guess reveals one correct letter\n"
                "• Hint reveals the **first letter** (once)\n\n"
                "**Rewards**\n"
                f"Solved: **{solve_amt} beans**\n"
                f"Fail: **{fail_mult} bean(s) per revealed letter**"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================
# SETUP
# =====================
async def setup(bot: discord.Client) -> None:
    services: dict[str, Any] = getattr(bot, "services", {})

    existing = {c.name for c in bot.tree.get_commands()}

    if "core" not in existing:
        bot.tree.add_command(CoreCommands(bot, services))
    if "games" not in existing:
        bot.tree.add_command(GamesCommands(bot, services))

    # Shop (/shop)
    if "shop" not in existing:
        if _has_service(services, "shop"):
            bot.tree.add_command(ShopCommands(services=services, shop_channel_id=BEAN_SHOP_CHANNEL_ID))
        else:
            logger.warning("shop service not found; /shop commands not registered")

    # Inventory (/inventory) — allowed everywhere by default (pass inventory_channel_id=... to restrict)
    if "inventory" not in existing:
        if _has_service(services, "shop"):
            bot.tree.add_command(InventoryCommands(services=services))
        else:
            logger.warning("shop service not found; /inventory not registered")

    # GeoGuessr learning (/geo-learning ...)
    if "geo-learning" not in existing:
        geo_learning = services.get("geo_learning")
        if geo_learning:
            bot.tree.add_command(GeoLearningCommands(geo_learning))
        else:
            logger.warning("geo_learning service not found; /geo-learning commands not registered")

    # GeoGuessr arcade (/geoguessr ...)
    if "geoguessr" not in existing:
        geo_flags = services.get("geo_flags")
        geo_language = services.get("geo_language")
        if geo_flags and geo_language:
            bot.tree.add_command(GeoGuessrCommands(flags_game=geo_flags, language_game=geo_language))
        else:
            logger.warning("geo_flags or geo_language not found; /geoguessr commands not registered")

    logger.info(
        "Discord commands registered: %s",
        " | ".join(c.name for c in bot.tree.get_commands()) or "(none)",
    )

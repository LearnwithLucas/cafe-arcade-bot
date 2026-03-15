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
from src.platforms.discord.admin_commands import AdminCommands
from src.db.repo.economy_repo import GUILD_EN, GUILD_NL


logger = logging.getLogger(__name__)


# =====================
# CHANNEL IDS (DISCORD)
# =====================
BEAN_COUNTER_CHANNEL_IDS = {
    1481764812248842280,  # 🧮 bean-counter (English)
    1482763329293520967,  # 🧮 bonen-teller (Dutch)
}
BEAN_HELP_CHANNEL_IDS = {
    1481764898496053480,  # ❓ bean-help (English)
    1482763682856439828,  # ❓ bonen-hulp (Dutch)
}
BEAN_SHOP_CHANNEL_IDS = {
    1481764947787780298,  # 🛍️ bean-shop (English)
    1482763754138501250,  # 🛍️ bonen-winkel (Dutch)
}

DUTCH_BEAN_COUNTER_IDS = {1482763329293520967}
DUTCH_BEAN_HELP_IDS = {1482763682856439828}
DUTCH_BEAN_SHOP_IDS = {1482763754138501250}


def _has_service(services: dict[str, Any], key: str) -> bool:
    return services.get(key) is not None


def _is_dutch_guild(bot: discord.Client, interaction: discord.Interaction) -> bool:
    dutch_guild_id = getattr(getattr(bot, "settings", None), "dutch_guild_id", None)
    return dutch_guild_id is not None and interaction.guild_id == int(dutch_guild_id)


# =====================
# CAFE COMMANDS (/cafe)
# =====================
class CafeCommands(app_commands.Group):
    def __init__(self, bot: discord.Client, services: dict[str, Any]) -> None:
        super().__init__(name="cafe", description="Café bot commands")
        self.bot = bot
        self.services = services

    def _in_channel(self, interaction: discord.Interaction, channel_ids: set[int]) -> bool:
        return int(getattr(interaction, "channel_id", 0) or 0) in channel_ids

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

    def _guild_id(self, interaction: discord.Interaction) -> str:
        return GUILD_NL if _is_dutch_guild(self.bot, interaction) else GUILD_EN

    # -------------
    # Shared commands (both servers, auto-detect guild)
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
            guild_id=self._guild_id(interaction),
        )
        await interaction.response.send_message(f"☕ You have **{balance}** beans.", ephemeral=True)

    @app_commands.command(name="wallet", description="Show your current bean balance")
    async def wallet(self, interaction: discord.Interaction) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return
        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            guild_id=self._guild_id(interaction),
        )
        await interaction.response.send_message(f"👛 Wallet: **{balance}** beans.", ephemeral=True)

    @app_commands.command(name="daily", description="Claim your daily beans (once per day)")
    async def daily(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_COUNTER_CHANNEL_IDS):
            await interaction.response.send_message(
                "Use this in #bean-counter or #bonen-teller 🧮", ephemeral=True,
            )
            return
        await self._do_daily(interaction, guild_id=self._guild_id(interaction))

    @app_commands.command(name="work", description="Work for beans (once per hour)")
    async def work(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_COUNTER_CHANNEL_IDS):
            await interaction.response.send_message(
                "Use this in #bean-counter or #bonen-teller 🧮", ephemeral=True,
            )
            return
        await self._do_work(interaction, guild_id=self._guild_id(interaction))

    @app_commands.command(name="help", description="Show all bot commands and rewards")
    async def help(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, BEAN_HELP_CHANNEL_IDS):
            await interaction.response.send_message(
                "Use this in #bean-help or #bonen-hulp ❓", ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="☕ Café Bot — Help",
            description=(
                "**Economy**\n"
                "`/cafe daily` — 25 beans (once/day)\n"
                "`/cafe work` — 5 beans (once/hour)\n"
                "`/cafe beans` — check balance\n\n"
                "**Games**\n"
                "`/games wordle_start` — daily Wordle\n"
                "`/games unscramble_start` — unscramble a word\n"
                "`/games wordchain_start` — start word chain\n\n"
                "**Shop**\n"
                "`/shop` — browse and buy items\n"
                "`/inventory` — view owned items\n\n"
                "**Leaderboard**\n"
                "`/cafe leaderboard` — refresh the leaderboard\n"
            ),
        )
        embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="leaderboard", description="Refresh the leaderboard panel")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        publisher = self.services.get("leaderboard_publisher")
        if _is_dutch_guild(self.bot, interaction):
            publisher = self.services.get("dutch_leaderboard_publisher") or publisher
        if not publisher:
            await interaction.response.send_message("Leaderboard not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await publisher.refresh_now()
            await interaction.followup.send("✅ Leaderboard refreshed.", ephemeral=True)
        except Exception:
            logger.exception("Failed to refresh leaderboard")
            await interaction.followup.send("❌ Failed to refresh leaderboard.", ephemeral=True)

    # -------------
    # Dutch commands (Dutch server only)
    # -------------

    @app_commands.command(name="dagelijks", description="Claim je dagelijkse bonen (één keer per dag)")
    async def dagelijks(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, DUTCH_BEAN_COUNTER_IDS):
            await interaction.response.send_message(
                "Gebruik dit in #bonen-teller 🧮", ephemeral=True,
            )
            return
        await self._do_daily(interaction, guild_id=GUILD_NL)

    @app_commands.command(name="werk", description="Verdien bonen door te werken (één keer per uur)")
    async def werk(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, DUTCH_BEAN_COUNTER_IDS):
            await interaction.response.send_message(
                "Gebruik dit in #bonen-teller 🧮", ephemeral=True,
            )
            return
        await self._do_work(interaction, guild_id=GUILD_NL)

    @app_commands.command(name="bonen", description="Bekijk je huidige bonensaldo")
    async def bonen(self, interaction: discord.Interaction) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service niet beschikbaar.", ephemeral=True)
            return
        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            guild_id=GUILD_NL,
        )
        await interaction.response.send_message(f"☕ Je hebt **{balance}** bonen.", ephemeral=True)

    @app_commands.command(name="portemonnee", description="Bekijk je bonensaldo")
    async def portemonnee(self, interaction: discord.Interaction) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service niet beschikbaar.", ephemeral=True)
            return
        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            guild_id=GUILD_NL,
        )
        await interaction.response.send_message(f"👛 Portemonnee: **{balance}** bonen.", ephemeral=True)

    @app_commands.command(name="hulp", description="Bekijk alle commando's en beloningen")
    async def hulp(self, interaction: discord.Interaction) -> None:
        if not self._in_channel(interaction, DUTCH_BEAN_HELP_IDS):
            await interaction.response.send_message(
                "Gebruik dit in #bonen-hulp ❓", ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="☕ Café Bot — Hulp",
            description=(
                "**Economie**\n"
                "`/cafe dagelijks` — 25 bonen (één keer per dag)\n"
                "`/cafe werk` — 5 bonen (één keer per uur)\n"
                "`/cafe bonen` — bekijk je saldo\n"
                "`/cafe portemonnee` — alias voor bonen\n\n"
                "**Spellen**\n"
                "`/games wordle_start` — dagelijkse Wordle\n"
                "`/games unscramble_start` — ontwar een woord\n"
                "`/games wordchain_start` — start woordketting\n\n"
                "**Winkel**\n"
                "`/winkel` — bekijk en koop items\n"
                "`/inventory` — bekijk je items\n\n"
                "**Scorebord**\n"
                "`/cafe scorebord` — vernieuw het scorebord\n"
            ),
        )
        embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="scorebord", description="Vernieuw het scorebord")
    async def scorebord(self, interaction: discord.Interaction) -> None:
        publisher = self.services.get("dutch_leaderboard_publisher") or self.services.get("leaderboard_publisher")
        if not publisher:
            await interaction.response.send_message("Scorebord niet beschikbaar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await publisher.refresh_now()
            await interaction.followup.send("✅ Scorebord vernieuwd.", ephemeral=True)
        except Exception:
            logger.exception("Failed to refresh Dutch leaderboard")
            await interaction.followup.send("❌ Vernieuwen mislukt.", ephemeral=True)

    # -------------
    # Shared logic
    # -------------

    async def _do_daily(self, interaction: discord.Interaction, guild_id: str) -> None:
        rewards = self._get_rewards()
        economy = self.services.get("economy")
        cooldowns = self._get_cooldowns()
        if not rewards or not economy or not cooldowns:
            await interaction.response.send_message("Service not available.", ephemeral=True)
            return

        allowed, remaining = cooldowns.try_acquire(
            action=f"cafe.daily.{guild_id}",
            user_id=interaction.user.id,
            location_id=f"{guild_id}:{interaction.user.id}",
            cooldown_seconds=24 * 60 * 60,
        )
        if not allowed:
            hours = remaining // 3600
            mins = (remaining % 3600) // 60
            title = "⏳ Al geclaimd vandaag" if guild_id == GUILD_NL else "⏳ Daily already claimed"
            desc = f"Probeer het over **{hours}u {mins}m** opnieuw." if guild_id == GUILD_NL else f"Try again in **{hours}h {mins}m**."
            await interaction.response.send_message(embed=self._bean_embed(title=title, description=desc), ephemeral=True)
            return

        amount = int(rewards.amount(RewardKey.CORE_DAILY))
        new_balance = await economy.award_beans_discord(
            user_id=interaction.user.id,
            amount=amount,
            reason="Daily claim",
            game_key="cafe",
            display_name=interaction.user.display_name,
            guild_id=guild_id,
        )

        if guild_id == GUILD_NL:
            embed = self._bean_embed(
                title="🌞 Dagelijkse beloning geclaimd!",
                description=f"<@{interaction.user.id}> verdiende **{amount} bonen**.\n👛 Nieuw saldo: **{new_balance}** bonen.",
            )
        else:
            embed = self._bean_embed(
                title="🌞 Daily claimed!",
                description=f"<@{interaction.user.id}> earned **{amount} beans**.\n👛 New balance: **{new_balance}** beans.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    async def _do_work(self, interaction: discord.Interaction, guild_id: str) -> None:
        rewards = self._get_rewards()
        economy = self.services.get("economy")
        cooldowns = self._get_cooldowns()
        if not rewards or not economy or not cooldowns:
            await interaction.response.send_message("Service not available.", ephemeral=True)
            return

        allowed, remaining = cooldowns.try_acquire(
            action=f"cafe.work.{guild_id}",
            user_id=interaction.user.id,
            location_id=f"{guild_id}:{interaction.user.id}",
            cooldown_seconds=60 * 60,
        )
        if not allowed:
            mins = remaining // 60
            secs = remaining % 60
            title = "⏳ Te vroeg om opnieuw te werken" if guild_id == GUILD_NL else "⏳ Too soon to work again"
            desc = f"Probeer het over **{mins}m {secs}s** opnieuw." if guild_id == GUILD_NL else f"Try again in **{mins}m {secs}s**."
            await interaction.response.send_message(embed=self._bean_embed(title=title, description=desc), ephemeral=True)
            return

        amount = int(rewards.amount(RewardKey.CORE_WORK)) if rewards else 5
        new_balance = await economy.award_beans_discord(
            user_id=interaction.user.id,
            amount=amount,
            reason="Work",
            game_key="cafe",
            display_name=interaction.user.display_name,
            guild_id=guild_id,
        )

        if guild_id == GUILD_NL:
            embed = self._bean_embed(
                title="💼 Gewerkt!",
                description=f"<@{interaction.user.id}> verdiende **{amount} bonen**.\n👛 Nieuw saldo: **{new_balance}** bonen.",
            )
        else:
            embed = self._bean_embed(
                title="💼 Worked!",
                description=f"<@{interaction.user.id}> earned **{amount} beans**.\n👛 New balance: **{new_balance}** beans.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=False)


# =====================
# GAMES COMMANDS
# =====================
class GamesCommands(app_commands.Group):
    def __init__(self, bot: discord.Client, services: dict[str, Any]) -> None:
        super().__init__(name="games", description="Play word games")
        self.bot = bot
        self.services = services

    @app_commands.command(name="wordchain_start", description="Start a Word Chain round")
    async def wordchain_start(self, interaction: discord.Interaction) -> None:
        word_chain = self.services.get("word_chain")
        if not word_chain:
            await interaction.response.send_message("Word Chain is not available.", ephemeral=True)
            return
        await word_chain.start(channel=interaction.channel, started_by=interaction.user)
        await interaction.response.send_message("⛓️ Word Chain started! Type a word to begin.", ephemeral=False)

    @app_commands.command(name="wordle_start", description="Start today's Wordle")
    async def wordle_start(self, interaction: discord.Interaction) -> None:
        wordle = self.services.get("wordle")
        if not wordle:
            await interaction.response.send_message("Wordle is not available.", ephemeral=True)
            return
        await wordle.start_in_channel(channel=interaction.channel, channel_id=interaction.channel_id)
        embed = discord.Embed(
            title="🧩 Wordle — Daily",
            description=(
                "• 5-letter words\n• 12 guesses\n\n"
                "🟩 correct spot\n🟨 wrong spot\n🟥 not in word\n\n"
                "**Hint:** `/games wordle_hint` (once)\n\n**Type your first guess:**"
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
            channel=interaction.channel, channel_id=interaction.channel_id, player_id=interaction.user.id,
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
        await interaction.followup.send("🛑 Unscramble stopped.", ephemeral=True)

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
                "Unscramble the scrambled word.\n\n**Rules**\n"
                "• 3 guesses max\n• Each wrong guess reveals one correct letter\n"
                "• Hint reveals the **first letter** (once)\n\n**Rewards**\n"
                f"Solved: **{solve_amt} beans**\nFail: **{fail_mult} bean(s) per revealed letter**"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)



    @app_commands.command(name="wordle_nl_start", description="Start het dagelijkse Woordle (Nederlands)")
    async def wordle_nl_start(self, interaction: discord.Interaction) -> None:
        wordle_nl = self.services.get("wordle_nl")
        if not wordle_nl:
            await interaction.response.send_message("Woordle NL is niet beschikbaar.", ephemeral=True)
            return
        await wordle_nl.start_in_channel(channel_id=interaction.channel_id)
        embed = discord.Embed(
            title="🧩 Woordle — Dagelijks",
            description=(
                "• 5-letter woorden\n"
                "• 12 pogingen\n\n"
                "🟩 goede plek\n"
                "🟨 verkeerde plek\n"
                "🟥 niet in woord\n\n"
                "**Hint:** `/games wordle_nl_hint` (één keer)\n\n"
                "**Typ je eerste woord:**"
            ),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wordle_nl_hint", description="Gebruik je Woordle hint")
    async def wordle_nl_hint(self, interaction: discord.Interaction) -> None:
        wordle_nl = self.services.get("wordle_nl")
        if not wordle_nl:
            await interaction.response.send_message("Woordle NL is niet beschikbaar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await wordle_nl.use_hint(
            channel=interaction.channel,
            channel_id=interaction.channel_id,
            player_id=interaction.user.id,
        )
        await interaction.followup.send(("💡 " if ok else "❌ ") + msg, ephemeral=True)

    @app_commands.command(name="wordle_nl_restart", description="Herstart Woordle in dit kanaal")
    async def wordle_nl_restart(self, interaction: discord.Interaction) -> None:
        wordle_nl = self.services.get("wordle_nl")
        if not wordle_nl:
            await interaction.response.send_message("Woordle NL is niet beschikbaar.", ephemeral=True)
            return
        await wordle_nl.restart_in_channel(channel_id=interaction.channel_id)
        await interaction.response.send_message("🔁 Woordle herstart.", ephemeral=False)

    @app_commands.command(name="ontwar_start", description="Start Ontwar het Woord (Nederlands)")
    async def ontwar_start(self, interaction: discord.Interaction) -> None:
        unscramble_nl = self.services.get("unscramble_nl")
        if not unscramble_nl:
            await interaction.response.send_message("Ontwar het Woord is niet beschikbaar.", ephemeral=True)
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Dit kan alleen in een server tekstkanaal.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await unscramble_nl.start_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🔀 Ontwar het Woord gestart! Bekijk het kanaal voor je puzzel.", ephemeral=True)

    @app_commands.command(name="ontwar_hint", description="Onthul de eerste letter (één keer)")
    async def ontwar_hint(self, interaction: discord.Interaction) -> None:
        unscramble_nl = self.services.get("unscramble_nl")
        if not unscramble_nl:
            await interaction.response.send_message("Ontwar het Woord is niet beschikbaar.", ephemeral=True)
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Dit kan alleen in een server tekstkanaal.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await unscramble_nl.hint_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("💡 Hint toegepast.", ephemeral=True)

    @app_commands.command(name="ontwar_stop", description="Stop je huidige Ontwar ronde")
    async def ontwar_stop(self, interaction: discord.Interaction) -> None:
        unscramble_nl = self.services.get("unscramble_nl")
        if not unscramble_nl:
            await interaction.response.send_message("Ontwar het Woord is niet beschikbaar.", ephemeral=True)
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Dit kan alleen in een server tekstkanaal.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await unscramble_nl.stop_for_user(channel=interaction.channel, user=interaction.user)
        await interaction.followup.send("🛑 Ontwar het Woord gestopt.", ephemeral=True)

    @app_commands.command(name="woordketting_start", description="Start een Woordketting ronde")
    async def woordketting_start(self, interaction: discord.Interaction) -> None:
        word_chain_nl = self.services.get("word_chain_nl")
        if not word_chain_nl:
            await interaction.response.send_message("Woordketting is niet beschikbaar.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔤 Woordketting — Live Ronde",
            description="**Typ je eerste woord:**",
        )
        await interaction.response.send_message(embed=embed)
        status_msg = await interaction.original_response()
        await word_chain_nl.start_in_channel(
            channel_id=interaction.channel_id,
            status_message_id=status_msg.id,
        )


# =====================
# SETUP
# =====================
async def setup(bot: discord.Client) -> None:
    services: dict[str, Any] = getattr(bot, "services", {})
    existing = {c.name for c in bot.tree.get_commands()}

    if "cafe" not in existing:
        bot.tree.add_command(CafeCommands(bot, services))
    if "games" not in existing:
        bot.tree.add_command(GamesCommands(bot, services))

    # English shop (/shop)
    if "shop" not in existing:
        if _has_service(services, "shop"):
            bot.tree.add_command(ShopCommands(
                services=services,
                shop_channel_ids=BEAN_SHOP_CHANNEL_IDS,
                command_name="shop",
            ))
        else:
            logger.warning("shop service not found; /shop commands not registered")

    # Dutch shop (/winkel)
    if "winkel" not in existing:
        if _has_service(services, "dutch_shop"):
            bot.tree.add_command(ShopCommands(
                services=services,
                shop_channel_ids=DUTCH_BEAN_SHOP_IDS,
                command_name="winkel",
                guild_id=GUILD_NL,
                service_key="dutch_shop",
            ))
        else:
            logger.warning("dutch_shop service not found; /winkel commands not registered")

    if "inventory" not in existing:
        if _has_service(services, "shop"):
            bot.tree.add_command(InventoryCommands(services=services))
        else:
            logger.warning("shop service not found; /inventory not registered")

    if "geo-learning" not in existing:
        geo_learning = services.get("geo_learning")
        if geo_learning:
            bot.tree.add_command(GeoLearningCommands(geo_learning))
        else:
            logger.warning("geo_learning service not found; /geo-learning commands not registered")

    if "geoguessr" not in existing:
        geo_flags = services.get("geo_flags")
        geo_language = services.get("geo_language")
        if geo_flags and geo_language:
            bot.tree.add_command(GeoGuessrCommands(flags_game=geo_flags, language_game=geo_language))
        else:
            logger.warning("geo_flags or geo_language not found; /geoguessr commands not registered")

    if "admin" not in existing:
        bot.tree.add_command(AdminCommands(services=services))

    logger.info(
        "Discord commands registered: %s",
        " | ".join(c.name for c in bot.tree.get_commands()) or "(none)",
    )
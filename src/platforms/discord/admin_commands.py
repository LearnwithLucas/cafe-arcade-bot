# ============================================================
# Bot name: Learn with Lucas — Game Bot (Berry)
# What this file does: Admin-only slash commands for managing
#                      user bean balances and managed bot posts
# Last updated: July 2026
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Awaitable

import discord
from discord import app_commands

from src.services.hub_service import HubPublisher
from src.services.instruction_publisher import DiscordInstructionPublisher

logger = logging.getLogger(__name__)

ADMIN_TESTING_CHANNEL_ID = 1205828956360548383
ADMIN_LOGS_CHANNEL_ID = 1340397297053339719


def is_admin():
    """Check that the user has Administrator permission in the server."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member):
            if interaction.user.guild_permissions.administrator:
                return True
        await interaction.response.send_message(
            "❌ You need Administrator permission to use this command.",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


class AdminCommands(app_commands.Group):
    def __init__(self, services: dict[str, Any]) -> None:
        super().__init__(name="admin", description="Admin commands for managing users")
        self.services = services

    def _in_admin_channel(self, interaction: discord.Interaction) -> bool:
        return int(getattr(interaction, "channel_id", 0) or 0) == ADMIN_TESTING_CHANNEL_ID

    async def _send_admin_log(self, interaction: discord.Interaction, message: str) -> None:
        channel = interaction.client.get_channel(ADMIN_LOGS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(ADMIN_LOGS_CHANNEL_ID)
            except Exception:
                logger.exception("Admin refresh: could not fetch admin logs channel")
                return

        if not hasattr(channel, "send"):
            logger.warning("Admin refresh: admin logs target is not messageable")
            return

        try:
            await channel.send(message)
        except Exception:
            logger.exception("Admin refresh: failed to send admin log")

    async def _run_refresh_step(
        self,
        *,
        label: str,
        work: Awaitable[None],
        refreshed: list[str],
        failed: list[str],
    ) -> None:
        try:
            await work
            refreshed.append(label)
        except Exception:
            logger.exception("Admin refresh: %s failed", label)
            failed.append(label)

    # -------------
    # /admin refresh
    # -------------
    @app_commands.command(name="refresh", description="Refresh managed bot posts and leaderboards")
    @is_admin()
    async def refresh(self, interaction: discord.Interaction) -> None:
        if not self._in_admin_channel(interaction):
            await interaction.response.send_message(
                "Use this in <#1205828956360548383> so admin actions stay in one place.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._send_admin_log(
            interaction,
            f"Admin refresh started by {interaction.user.mention} in <#1205828956360548383>.",
        )

        refreshed: list[str] = []
        failed: list[str] = []

        instructions = DiscordInstructionPublisher(bot=interaction.client)
        await self._run_refresh_step(
            label="pinned game instructions",
            work=instructions.publish_all(),
            refreshed=refreshed,
            failed=failed,
        )

        hub = HubPublisher(bot=interaction.client)
        await self._run_refresh_step(
            label="English start-here hub",
            work=hub.publish_english(),
            refreshed=refreshed,
            failed=failed,
        )
        await self._run_refresh_step(
            label="Dutch begin-hier hub",
            work=hub.publish_dutch(),
            refreshed=refreshed,
            failed=failed,
        )

        leaderboard = self.services.get("leaderboard_publisher")
        if leaderboard is not None:
            await self._run_refresh_step(
                label="English leaderboard",
                work=leaderboard.refresh_now(),
                refreshed=refreshed,
                failed=failed,
            )

        dutch_leaderboard = self.services.get("dutch_leaderboard_publisher")
        if dutch_leaderboard is not None:
            await self._run_refresh_step(
                label="Dutch leaderboard",
                work=dutch_leaderboard.refresh_now(),
                refreshed=refreshed,
                failed=failed,
            )

        status = "✅ Admin refresh complete." if not failed else "⚠️ Admin refresh finished with issues."
        details = [status]
        if refreshed:
            details.append("Refreshed: " + ", ".join(refreshed))
        if failed:
            details.append("Failed: " + ", ".join(failed))

        message = "\n".join(details)
        await interaction.followup.send(message, ephemeral=True)
        await self._send_admin_log(interaction, message)

    # -------------
    # /admin give @user amount
    # -------------
    @app_commands.command(name="give", description="Give beans to a user")
    @app_commands.describe(user="The user to give beans to", amount="How many beans to give")
    @is_admin()
    async def give(self, interaction: discord.Interaction, user: discord.Member, amount: int) -> None:
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
            return

        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        new_balance = await economy.award_beans_discord(
            user_id=user.id,
            amount=amount,
            reason=f"Admin grant by {interaction.user.display_name}",
            game_key="admin",
            display_name=user.display_name,
        )

        await interaction.response.send_message(
            f"✅ Gave **{amount} beans** to {user.mention}.\n👛 New balance: **{new_balance} beans**.",
            ephemeral=False,
        )

    # -------------
    # /admin take @user amount
    # -------------
    @app_commands.command(name="take", description="Take beans from a user")
    @app_commands.describe(user="The user to take beans from", amount="How many beans to take")
    @is_admin()
    async def take(self, interaction: discord.Interaction, user: discord.Member, amount: int) -> None:
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
            return

        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        # Deduct by awarding a negative amount
        new_balance = await economy.award_beans_discord(
            user_id=user.id,
            amount=-amount,
            reason=f"Admin deduction by {interaction.user.display_name}",
            game_key="admin",
            display_name=user.display_name,
        )

        await interaction.response.send_message(
            f"✅ Took **{amount} beans** from {user.mention}.\n👛 New balance: **{new_balance} beans**.",
            ephemeral=False,
        )

    # -------------
    # /admin set @user amount
    # -------------
    @app_commands.command(name="set", description="Set a user's bean balance to an exact amount")
    @app_commands.describe(user="The user to update", amount="The exact balance to set")
    @is_admin()
    async def set_balance(self, interaction: discord.Interaction, user: discord.Member, amount: int) -> None:
        if amount < 0:
            await interaction.response.send_message("❌ Balance cannot be negative.", ephemeral=True)
            return

        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        # Get current balance then adjust to reach the target
        current = await economy.get_balance_discord(
            user_id=user.id,
            display_name=user.display_name,
        )
        difference = amount - current

        if difference == 0:
            await interaction.response.send_message(
                f"ℹ️ {user.mention} already has **{amount} beans**. No change made.",
                ephemeral=True,
            )
            return

        new_balance = await economy.award_beans_discord(
            user_id=user.id,
            amount=difference,
            reason=f"Admin balance set by {interaction.user.display_name}",
            game_key="admin",
            display_name=user.display_name,
        )

        await interaction.response.send_message(
            f"✅ Set {user.mention}'s balance to **{new_balance} beans**.",
            ephemeral=False,
        )

    # -------------
    # /admin check @user
    # -------------
    @app_commands.command(name="check", description="Check a user's bean balance")
    @app_commands.describe(user="The user to check")
    @is_admin()
    async def check(self, interaction: discord.Interaction, user: discord.Member) -> None:
        economy = self.services.get("economy")
        if economy is None:
            await interaction.response.send_message("Economy service not available.", ephemeral=True)
            return

        balance = await economy.get_balance_discord(
            user_id=user.id,
            display_name=user.display_name,
        )

        await interaction.response.send_message(
            f"👛 {user.mention} has **{balance} beans**.",
            ephemeral=True,  # only visible to the admin
        )
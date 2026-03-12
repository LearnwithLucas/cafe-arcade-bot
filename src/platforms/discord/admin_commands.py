# ============================================================
# Bot name: Learn with Lucas — Game Bot (Berry)
# What this file does: Admin-only slash commands for managing
#                      user bean balances (/admin give, take, set)
# Last updated: March 2026
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

logger = logging.getLogger(__name__)


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
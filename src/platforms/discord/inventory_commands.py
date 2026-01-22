from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands

from src.assets.asset_links import AssetLinks

logger = logging.getLogger(__name__)


@dataclass
class _Selected:
    item_key: str | None = None


def _inv_embed(*, title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description)
    embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
    return embed


class InventoryCommands(app_commands.Command):
    """
    Single command: /inventory

    - Shows inventory as an interactive panel
    - Dropdown contains only items that:
        * quantity > 0
        * max_uses_per_day > 0 (usable)
    - Selecting an item immediately uses 1x
    - Confirmation is ephemeral
    - Inventory message stays open and refreshes after use
    """

    def __init__(self, *, services: dict[str, Any], inventory_channel_id: int | None = None) -> None:
        self._services = services
        self._inventory_channel_id = int(inventory_channel_id) if inventory_channel_id else None

        async def _callback(interaction: discord.Interaction) -> None:
            await self._open_inventory(interaction)

        super().__init__(
            name="inventory",
            description="Open your inventory (interactive)",
            callback=_callback,
        )

    def _in_allowed_channel(self, interaction: discord.Interaction) -> bool:
        if self._inventory_channel_id is None:
            return True
        return int(getattr(interaction, "channel_id", 0) or 0) == self._inventory_channel_id

    async def _render_inventory(
        self,
        *,
        discord_user_id: int,
        display_name: str | None,
    ) -> tuple[discord.Embed, list[dict[str, Any]]]:
        shop = self._services.get("shop")
        economy = self._services.get("economy")

        if not shop or not economy:
            return _inv_embed(title="🎒 Inventory", description="Inventory is not available right now."), []

        balance = await economy.get_balance_discord(user_id=discord_user_id, display_name=display_name)
        rows = await shop.inventory_discord(discord_user_id=discord_user_id, display_name=display_name)

        # Build pretty list (show all items, including qty=0, but dropdown will filter usable+owned)
        lines: list[str] = []
        for r in rows:
            key = str(r.get("key") or r.get("item_key") or "")
            name = str(r.get("name") or key)
            desc = str(r.get("description") or "")
            qty = int(r.get("quantity") or 0)
            max_stack = int(r.get("max_stack") or 0)
            max_uses = int(r.get("max_uses_per_day") or 0)

            extra: list[str] = []
            if max_stack > 0:
                extra.append(f"cap {max_stack}")
            if max_uses > 0:
                extra.append(f"{max_uses}/day")

            meta = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"**{name}** — you have **{qty}**{meta}\n> {desc}")

        desc_text = (
            f"👛 Balance: **{balance}** beans\n\n"
            "Select an item below to **use 1×**.\n\n"
            + ("\n\n".join(lines) if lines else "No items.")
        )

        return _inv_embed(title="🎒 Inventory", description=desc_text), rows

    async def _open_inventory(self, interaction: discord.Interaction) -> None:
        if not self._in_allowed_channel(interaction):
            await interaction.response.send_message(
                f"Use this in <#{self._inventory_channel_id}> 🎒" if self._inventory_channel_id else "Not allowed here.",
                ephemeral=True,
            )
            return

        shop = self._services.get("shop")
        if not shop:
            await interaction.response.send_message("Shop service not available.", ephemeral=True)
            return

        embed, rows = await self._render_inventory(
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
        )

        view = _InventoryView(
            services=self._services,
            owner_id=interaction.user.id,
            initial_rows=rows,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)


class _UseSelect(discord.ui.Select):
    def __init__(self, *, rows: list[dict[str, Any]], selected: _Selected) -> None:
        self._selected = selected

        # Filter: owned + usable
        usable_owned = []
        for r in rows:
            qty = int(r.get("quantity") or 0)
            max_uses = int(r.get("max_uses_per_day") or 0)
            key = str(r.get("key") or r.get("item_key") or "")
            if key and qty > 0 and max_uses > 0:
                usable_owned.append(r)

        options: list[discord.SelectOption] = []
        for r in usable_owned:
            key = str(r.get("key") or r.get("item_key") or "")
            name = str(r.get("name") or key)
            desc = str(r.get("description") or "")
            qty = int(r.get("quantity") or 0)
            label = (name[:90] if name else key)  # keep room for qty
            description = (f"Have {qty} — " + desc)[:100]
            options.append(discord.SelectOption(label=label, description=description or None, value=key))

        if not options:
            options = [
                discord.SelectOption(
                    label="No usable items",
                    value="__none__",
                    description="Buy items in /shop first",
                )
            ]

        super().__init__(
            placeholder="Use an item (uses 1×)…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0] if self.values else None
        if val == "__none__" or not val:
            self._selected.item_key = None
            await interaction.response.send_message("You have no usable items.", ephemeral=True)
            return

        self._selected.item_key = str(val)

        view = self.view
        if not isinstance(view, _InventoryView):
            await interaction.response.send_message("Inventory panel error.", ephemeral=True)
            return

        await view.use_selected(interaction)


class _InventoryView(discord.ui.View):
    def __init__(
        self,
        *,
        services: dict[str, Any],
        owner_id: int,
        initial_rows: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=60 * 15)

        self._services = services
        self._owner_id = int(owner_id)
        self._rows: list[dict[str, Any]] = list(initial_rows or [])
        self._selected = _Selected(item_key=None)

        self._select = _UseSelect(rows=self._rows, selected=self._selected)
        self.add_item(self._select)

    async def _refresh_panel(self, *, interaction: discord.Interaction) -> None:
        shop = self._services.get("shop")
        economy = self._services.get("economy")
        if not shop or not economy:
            return

        balance = await economy.get_balance_discord(
            user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
        )
        rows = await shop.inventory_discord(
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
        )
        self._rows = list(rows or [])

        # rebuild embed
        lines: list[str] = []
        for r in self._rows:
            key = str(r.get("key") or r.get("item_key") or "")
            name = str(r.get("name") or key)
            desc = str(r.get("description") or "")
            qty = int(r.get("quantity") or 0)
            max_stack = int(r.get("max_stack") or 0)
            max_uses = int(r.get("max_uses_per_day") or 0)

            extra: list[str] = []
            if max_stack > 0:
                extra.append(f"cap {max_stack}")
            if max_uses > 0:
                extra.append(f"{max_uses}/day")

            meta = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"**{name}** — you have **{qty}**{meta}\n> {desc}")

        desc_text = (
            f"👛 Balance: **{balance}** beans\n\n"
            "Select an item below to **use 1×**.\n\n"
            + ("\n\n".join(lines) if lines else "No items.")
        )
        embed = _inv_embed(title="🎒 Inventory", description=desc_text)

        # rebuild select (options depend on qty/max_uses)
        self.remove_item(self._select)
        self._select = _UseSelect(rows=self._rows, selected=self._selected)
        self.add_item(self._select)

        if interaction.message:
            await interaction.message.edit(embed=embed, view=self)

    async def use_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This inventory panel belongs to someone else.", ephemeral=True)
            return

        shop = self._services.get("shop")
        if not shop:
            await interaction.response.send_message("Shop service not available.", ephemeral=True)
            return

        item_key = self._selected.item_key
        if not item_key:
            await interaction.response.send_message("Select an item first.", ephemeral=True)
            return

        res = await shop.use_discord(
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
            item_key=str(item_key),
            quantity=1,
        )

        # Confirmation
        await interaction.response.send_message(("✅ " if res.ok else "❌ ") + res.message, ephemeral=True)

        # Refresh panel (keep open)
        try:
            await self._refresh_panel(interaction=interaction)
        except Exception:
            logger.exception("Failed to refresh inventory panel after use")

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True  # type: ignore[attr-defined]
        return

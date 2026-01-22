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


def _shop_embed(*, title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description)
    # Reuse bean icon for now (you can swap to AssetLinks.SHOP_ICON later if you add it)
    embed.set_thumbnail(url=AssetLinks.BEAN_CURRENCY_ICON)
    return embed


def _key_from_row(r: dict[str, Any]) -> str:
    return str(r.get("key") or r.get("item_key") or "").strip()


async def _render_shop(
    *,
    services: dict[str, Any],
    discord_user_id: int,
    display_name: str | None,
) -> tuple[discord.Embed, list[dict[str, Any]]]:
    """
    Renders the shop "catalog + your inventory" view.

    Returns:
      (embed, rows) where rows are the inventory_discord joined rows.
    """
    shop = services.get("shop")
    economy = services.get("economy")

    if not shop or not economy:
        embed = _shop_embed(
            title="🛍️ Bean Shop",
            description="Shop is not available right now.",
        )
        return embed, []

    balance = await economy.get_balance_discord(user_id=discord_user_id, display_name=display_name)
    rows = await shop.inventory_discord(discord_user_id=discord_user_id, display_name=display_name)

    lines: list[str] = []
    for r in rows:
        key = _key_from_row(r)
        name = str(r.get("name") or key)
        desc = str(r.get("description") or "")
        price = int(r.get("price") or 0)
        qty = int(r.get("quantity") or 0)
        max_stack = int(r.get("max_stack") or 0)
        max_uses = int(r.get("max_uses_per_day") or 0)

        extra: list[str] = []
        if max_stack > 0:
            extra.append(f"cap {max_stack}")
        if max_uses > 0:
            extra.append(f"{max_uses}/day")

        meta = f" ({', '.join(extra)})" if extra else ""
        price_str = f"**{price}** beans" if price > 0 else "**—**"
        lines.append(f"**{name}** — {price_str} | you have **{qty}**{meta}\n> {desc}")

    desc_text = (
        f"👛 Balance: **{balance}** beans\n\n"
        "Pick an item from the dropdown to buy **1×** instantly.\n\n"
        + ("\n\n".join(lines) if lines else "No items available.")
    )

    embed = _shop_embed(title="🛍️ Bean Shop", description=desc_text)
    return embed, rows


class ShopCommands(app_commands.Command):
    """
    Single command: /shop

    UX:
      - /shop posts an interactive shop panel
      - Dropdown = ONE-CLICK purchase (buys 1× immediately)
      - Ephemeral confirmation message
      - Shop message stays open and refreshes after purchase
      - Refresh button to redraw the panel
    """

    def __init__(self, *, services: dict[str, Any], shop_channel_id: int) -> None:
        self._services = services
        self._shop_channel_id = int(shop_channel_id)

        async def _callback(interaction: discord.Interaction) -> None:
            await self._open_shop(interaction)

        super().__init__(
            name="shop",
            description="Open the bean shop (interactive)",
            callback=_callback,
        )

    def _in_shop_channel(self, interaction: discord.Interaction) -> bool:
        return int(getattr(interaction, "channel_id", 0) or 0) == self._shop_channel_id

    async def _open_shop(self, interaction: discord.Interaction) -> None:
        if not self._in_shop_channel(interaction):
            await interaction.response.send_message(
                f"Use this in <#{self._shop_channel_id}> 🛍️",
                ephemeral=True,
            )
            return

        shop = self._services.get("shop")
        if not shop:
            await interaction.response.send_message("Shop service not available.", ephemeral=True)
            return

        embed, rows = await _render_shop(
            services=self._services,
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
        )

        view = _ShopView(
            services=self._services,
            shop_channel_id=self._shop_channel_id,
            owner_id=interaction.user.id,
            initial_rows=rows,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)


class _PurchaseSelect(discord.ui.Select):
    def __init__(self, *, rows: list[dict[str, Any]], selected: _Selected) -> None:
        self._selected = selected

        options: list[discord.SelectOption] = []
        for r in rows:
            key = _key_from_row(r)
            if not key:
                continue

            name = str(r.get("name") or key)
            price = int(r.get("price") or 0)
            qty = int(r.get("quantity") or 0)
            desc = str(r.get("description") or "")

            # Keep these short for Discord UI limits
            label = (f"{name} — {price} beans").strip()[:100]
            description = (f"Owned: {qty} • " + desc).strip()[:100]

            options.append(
                discord.SelectOption(
                    label=label,
                    description=description or None,
                    value=key,
                )
            )

        if not options:
            options = [discord.SelectOption(label="No items", value="__none__", description="Shop is empty")]

        super().__init__(
            placeholder="Buy 1× item…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0] if self.values else None
        if val == "__none__" or not val:
            self._selected.item_key = None
            await interaction.response.send_message("No items to buy.", ephemeral=True)
            return

        self._selected.item_key = str(val)

        view = self.view
        if not isinstance(view, _ShopView):
            await interaction.response.send_message("Shop UI error (view missing).", ephemeral=True)
            return

        await view.buy_one_and_refresh(interaction=interaction, item_key=str(val))


class _RefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _ShopView):
            await interaction.response.send_message("Shop UI error (view missing).", ephemeral=True)
            return
        await view.refresh(interaction=interaction, notice="🔄 Refreshed.")


class _ShopView(discord.ui.View):
    def __init__(
        self,
        *,
        services: dict[str, Any],
        shop_channel_id: int,
        owner_id: int,
        initial_rows: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=60 * 15)  # 15 min

        self._services = services
        self._shop_channel_id = int(shop_channel_id)
        self._owner_id = int(owner_id)

        self._selected = _Selected(item_key=None)
        self._rows: list[dict[str, Any]] = list(initial_rows or [])

        self._select = _PurchaseSelect(rows=self._rows, selected=self._selected)
        self.add_item(self._select)
        self.add_item(_RefreshButton())

    def _validate_owner_and_channel(self, interaction: discord.Interaction) -> str | None:
        if interaction.user.id != self._owner_id:
            return "This shop panel belongs to someone else."
        if int(getattr(interaction, "channel_id", 0) or 0) != self._shop_channel_id:
            return "Use the shop in the shop channel."
        return None

    async def refresh(self, *, interaction: discord.Interaction, notice: str | None = None) -> None:
        err = self._validate_owner_and_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        embed, rows = await _render_shop(
            services=self._services,
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
        )

        # rebuild dropdown options to reflect latest state
        await self._refresh_rows(rows)

        # edit the original message in place
        try:
            if interaction.response.is_done():
                # if we already responded elsewhere, just edit message and optionally follow up
                if interaction.message:
                    await interaction.message.edit(embed=embed, view=self)
                if notice:
                    await interaction.followup.send(notice, ephemeral=True)
            else:
                # respond by editing message (no extra messages in channel)
                if interaction.message:
                    await interaction.response.edit_message(embed=embed, view=self)
                else:
                    await interaction.response.send_message(embed=embed, view=self, ephemeral=False)
                if notice:
                    await interaction.followup.send(notice, ephemeral=True)
        except Exception:
            logger.exception("Failed to refresh shop panel")

    async def buy_one_and_refresh(self, *, interaction: discord.Interaction, item_key: str) -> None:
        err = self._validate_owner_and_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        shop = self._services.get("shop")
        if not shop:
            await interaction.response.send_message("Shop service not available.", ephemeral=True)
            return

        # Buy exactly 1 on selection (one-click)
        res = await shop.buy_discord(
            discord_user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", None),
            item_key=str(item_key),
            quantity=1,
        )

        # Ephemeral confirmation
        await interaction.response.send_message(("✅ " if res.ok else "❌ ") + res.message, ephemeral=True)

        # Refresh the main shop panel without closing it
        try:
            embed, rows = await _render_shop(
                services=self._services,
                discord_user_id=interaction.user.id,
                display_name=getattr(interaction.user, "display_name", None),
            )
            await self._refresh_rows(rows)

            if interaction.message:
                await interaction.message.edit(embed=embed, view=self)
        except Exception:
            logger.exception("Failed to refresh shop panel after purchase")

    async def _refresh_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows or [])

        keep = self._selected.item_key
        new_select = _PurchaseSelect(rows=self._rows, selected=self._selected)

        # reset selection if item disappeared
        if keep and not any(_key_from_row(r) == keep for r in self._rows):
            self._selected.item_key = None

        self.remove_item(self._select)
        self._select = new_select
        self.add_item(self._select)

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True  # type: ignore[attr-defined]
        return

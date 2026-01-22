from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import discord

from src.assets.asset_links import AssetLinks
from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.leaderboard_publisher import LeaderboardPublisher
from src.services.rewards_service import RewardsService, RewardKey
from src.services.geo_quiz_bank import GeoQuizBank, GeoQuizItem

logger = logging.getLogger(__name__)

GAME_KEY = "geo_language"

# Streak rules
MAX_WRONG_GUESSES = 1  # one mistake ends the streak (Word Chain style)
BONUS_EVERY = 10       # every 10 correct answers
BONUS_BEANS = 3        # bonus beans per milestone


def _norm_free_text(s: str) -> str:
    """
    Normalize a user answer that may contain spaces/punctuation.
    Keep letters only, lowercase.
    """
    s = (s or "").strip().lower()
    return "".join(ch for ch in s if ch.isalpha())


def _now_ts() -> float:
    return time.time()


@dataclass
class _Progress:
    prompt: str
    answer: str
    aliases: list[str]

    # streak stats
    streak: int
    wrong_guesses: int

    # session status
    finished: bool
    status_message_id: int | None
    started_at: float


class GeoLanguageGame:
    """
    Geo Language — streak mode (Word Chain style), per-user panel in a shared channel.

    Flow:
      - /geoguessr language_start creates your panel
      - You type the language (or accepted alias) in the channel
      - Correct:
          - +2 beans per correct
          - +3 bonus beans every 10 streak (10, 20, 30, ...)
          - streak continues with a NEW prompt immediately (panel updates)
      - Wrong:
          - streak ends immediately (like Word Chain)
          - sends a summary embed + Play Again button
    """

    key = GAME_KEY

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        cooldowns: Cooldowns,
        bank: GeoQuizBank,
        allowed_channel_ids: set[int],
        leaderboard_publisher: LeaderboardPublisher | None = None,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._economy = economy
        self._rewards = rewards
        self._cooldowns = cooldowns
        self._bank = bank
        self._allowed_channel_ids = allowed_channel_ids
        self._publisher = leaderboard_publisher

        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._edit_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_edit_at: dict[tuple[int, int], float] = {}

    # -------------------------
    # Locks
    # -------------------------

    def _lock_for_channel(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    def _lock_for_player(self, channel_id: int, player_id: int) -> asyncio.Lock:
        key = (channel_id, player_id)
        if key not in self._edit_locks:
            self._edit_locks[key] = asyncio.Lock()
        return self._edit_locks[key]

    # -------------------------
    # Rewards
    # -------------------------

    def _beans_per_correct(self) -> int:
        # Centralized in RewardsService (recommended key), fallback 2
        try:
            return int(self._rewards.amount(RewardKey.GEO_LANGUAGE_CORRECT))
        except Exception:
            return 2

    def _bonus_every(self) -> int:
        try:
            return int(self._rewards.amount(RewardKey.GEO_LANGUAGE_BONUS_EVERY))
        except Exception:
            return BONUS_EVERY

    def _bonus_amount(self) -> int:
        try:
            return int(self._rewards.amount(RewardKey.GEO_LANGUAGE_BONUS_AMOUNT))
        except Exception:
            return BONUS_BEANS

    # -------------------------
    # State helpers
    # -------------------------

    def _pick_item(self) -> GeoQuizItem:
        return self._bank.random_language()

    def _new_progress(self) -> _Progress:
        item = self._pick_item()
        return _Progress(
            prompt=str(item.prompt),
            answer=str(item.answer),
            aliases=list(item.aliases or []),
            streak=0,
            wrong_guesses=0,
            finished=False,
            status_message_id=None,
            started_at=_now_ts(),
        )

    @staticmethod
    def _progress_from_dict(d: dict[str, Any]) -> _Progress:
        return _Progress(
            prompt=str(d.get("prompt", "")),
            answer=str(d.get("answer", "")),
            aliases=list(d.get("aliases", []) or []),
            streak=int(d.get("streak", 0)),
            wrong_guesses=int(d.get("wrong_guesses", 0)),
            finished=bool(d.get("finished", False)),
            status_message_id=(int(d["status_message_id"]) if d.get("status_message_id") else None),
            started_at=float(d.get("started_at", _now_ts())),
        )

    @staticmethod
    def _progress_to_dict(p: _Progress) -> dict[str, Any]:
        return {
            "prompt": p.prompt,
            "answer": p.answer,
            "aliases": p.aliases,
            "streak": p.streak,
            "wrong_guesses": p.wrong_guesses,
            "finished": p.finished,
            "status_message_id": p.status_message_id,
            "started_at": p.started_at,
        }

    async def _get_or_create_session(self, *, channel_id: int) -> tuple[str, dict[str, Any]] | None:
        sess = await self._games_repo.get_active_session(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
        )
        if not sess:
            session_id = f"{self.key}:{channel_id}"
            await self._games_repo.upsert_active_session(
                session_id=session_id,
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
                state={"players": {}},
            )
            sess = await self._games_repo.get_active_session(
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
            )
            if not sess:
                return None

        return sess.id, json.loads(sess.state_json)

    async def _save_state(self, *, session_id: str, channel_id: int, state: dict[str, Any]) -> None:
        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )

    # -------------------------
    # Panel rendering
    # -------------------------

    def _build_panel_embed(self, *, player_id: int, p: _Progress) -> discord.Embed:
        status_line = "Type the **language** (or accepted country) in this channel."
        if p.finished:
            status_line = "🛑 Streak ended. Press Play again below."

        embed = discord.Embed(
            title="🗺️ GeoGuessr — Language (Streak)",
            description=(
                f"**Sample:**\n> {p.prompt}\n\n"
                f"**Streak:** **{p.streak}**\n\n"
                f"{status_line}"
            ),
        )
        embed.set_thumbnail(url=AssetLinks.GEO_LANGUAGE_ICON)
        embed.set_footer(text=f"Player: {player_id}")
        return embed

    async def _ensure_panel(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        p: _Progress,
    ) -> _Progress:
        if p.status_message_id:
            return p

        embed = self._build_panel_embed(player_id=player_id, p=p)
        msg = await channel.send(content=f"<@{player_id}>", embed=embed)
        p.status_message_id = msg.id
        self._last_edit_at[(channel_id, player_id)] = 0.0
        return p

    async def _edit_panel(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        p: _Progress,
        force: bool = False,
    ) -> None:
        if not p.status_message_id or not hasattr(channel, "fetch_message"):
            return

        key = (channel_id, player_id)
        now = time.time()
        last = self._last_edit_at.get(key, 0.0)
        if not force and (now - last) < 1.0:
            return

        lock = self._lock_for_player(channel_id, player_id)
        async with lock:
            now2 = time.time()
            last2 = self._last_edit_at.get(key, 0.0)
            if not force and (now2 - last2) < 1.0:
                return

            try:
                msg = await channel.fetch_message(int(p.status_message_id))  # type: ignore[attr-defined]
                embed = self._build_panel_embed(player_id=player_id, p=p)
                await msg.edit(content=f"<@{player_id}>", embed=embed)
                self._last_edit_at[key] = time.time()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                p.status_message_id = None

    # -------------------------
    # Public API for slash commands
    # -------------------------

    async def start_for_user(self, *, channel: discord.abc.Messageable, user: discord.abc.User) -> None:
        if not hasattr(channel, "id"):
            return
        channel_id = int(getattr(channel, "id"))

        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return
            sess_id, state = sess

            players: dict[str, Any] = state.get("players", {}) or {}
            p = self._new_progress()

            p = await self._ensure_panel(channel=channel, channel_id=channel_id, player_id=user.id, p=p)
            await self._edit_panel(channel=channel, channel_id=channel_id, player_id=user.id, p=p, force=True)

            players[str(user.id)] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(session_id=sess_id, channel_id=channel_id, state=state)

    async def stop_for_user(self, *, channel: discord.abc.Messageable, user: discord.abc.User) -> None:
        if not hasattr(channel, "id"):
            return
        channel_id = int(getattr(channel, "id"))

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return
            sess_id, state = sess

            players: dict[str, Any] = state.get("players", {}) or {}
            players.pop(str(user.id), None)
            state["players"] = players
            await self._save_state(session_id=sess_id, channel_id=channel_id, state=state)

    # -------------------------
    # Completion + Play Again
    # -------------------------

    async def _send_streak_end(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        final_streak: int,
        correct_answer: str,
    ) -> None:
        class PlayAgainView(discord.ui.View):
            def __init__(self, outer: "GeoLanguageGame") -> None:
                super().__init__(timeout=900)
                self._outer = outer

            @discord.ui.button(label="Play again", style=discord.ButtonStyle.success, emoji="🔁")
            async def play_again(  # type: ignore[override]
                self,
                interaction: discord.Interaction,
                button: discord.ui.Button,
            ) -> None:
                if interaction.user.id != player_id:
                    await interaction.response.send_message(
                        "This button is for the player who finished this streak.",
                        ephemeral=True,
                    )
                    return

                if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                    await interaction.response.send_message("Use this in a server channel.", ephemeral=True)
                    return

                await interaction.response.defer(ephemeral=True)
                await self._outer.start_for_user(channel=interaction.channel, user=interaction.user)
                await interaction.followup.send("✅ New language sample posted — streak restarted!", ephemeral=True)

        embed = discord.Embed(
            title="🛑 Geo Language — Streak ended",
            description=(
                f"Ended for <@{player_id}>.\n\n"
                f"**Correct answer was:** `{correct_answer}`\n"
                f"**Final streak:** **{final_streak}**\n\n"
                "Press **Play again** to start a new streak."
            ),
        )
        embed.set_thumbnail(url=AssetLinks.GEO_LANGUAGE_ICON)
        await channel.send(embed=embed, view=PlayAgainView(self))

    # -------------------------
    # Message handling
    # -------------------------

    async def handle_discord_message(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False

        channel_id = message.channel.id
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return False

        guess_norm = _norm_free_text(message.content or "")
        if not guess_norm:
            return False

        player_id = message.author.id

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return False
            sess_id, state = sess
            players: dict[str, Any] = state.get("players", {}) or {}
            pid = str(player_id)

            if pid not in players:
                return False

            p = self._progress_from_dict(players.get(pid, {}))
            if p.finished:
                return True

            p = await self._ensure_panel(channel=message.channel, channel_id=channel_id, player_id=player_id, p=p)

            accepted = {_norm_free_text(p.answer)}
            for a in p.aliases or []:
                accepted.add(_norm_free_text(str(a)))

            is_correct = guess_norm in accepted

            # delete user guess (keep channel clean)
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            if is_correct:
                p.streak += 1

                per_correct = self._beans_per_correct()
                bonus_every = max(1, self._bonus_every())
                bonus_amt = max(0, self._bonus_amount())
                bonus = bonus_amt if (p.streak % bonus_every == 0) else 0
                beans = int(per_correct + bonus)

                await self._economy.award_beans_discord(
                    user_id=player_id,
                    amount=beans,
                    reason="Geo Language streak correct",
                    game_key=self.key,
                    display_name=message.author.display_name,
                    metadata=json.dumps(
                        {
                            "prompt": p.prompt,
                            "answer": p.answer,
                            "streak": p.streak,
                            "per_correct": per_correct,
                            "bonus": bonus,
                        },
                        ensure_ascii=False,
                    ),
                )

                user = await self._users_repo.get_or_create_discord_user(
                    discord_user_id=player_id,
                    display_name=message.author.display_name,
                )
                await self._games_repo.record_game_result(
                    user_id=user.id,
                    game_key=self.key,
                    score=beans,
                    beans_earned=beans,
                    context_json=json.dumps(
                        {
                            "prompt": p.prompt,
                            "answer": p.answer,
                            "solved": True,
                            "streak": p.streak,
                            "per_correct": per_correct,
                            "bonus": bonus,
                        },
                        ensure_ascii=False,
                    ),
                )

                if self._publisher:
                    self._publisher.schedule_refresh()

                # swap to a new prompt (avoid immediate repeats when possible)
                item = self._pick_item()
                for _ in range(4):
                    if str(item.prompt) != p.prompt:
                        break
                    item = self._pick_item()

                p.prompt = str(item.prompt)
                p.answer = str(item.answer)
                p.aliases = list(item.aliases or [])
                p.wrong_guesses = 0

                players[pid] = self._progress_to_dict(p)
                state["players"] = players
                await self._save_state(session_id=sess_id, channel_id=channel_id, state=state)

                await self._edit_panel(
                    channel=message.channel,
                    channel_id=channel_id,
                    player_id=player_id,
                    p=p,
                    force=True,
                )

                if bonus > 0:
                    try:
                        await message.channel.send(
                            f"🎉 <@{player_id}> hit a **{p.streak}** language streak! **+{bonus} bonus beans**!"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                return True

            # wrong answer -> streak ends immediately
            p.wrong_guesses += 1
            p.finished = True

            players[pid] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(session_id=sess_id, channel_id=channel_id, state=state)

            await self._edit_panel(
                channel=message.channel,
                channel_id=channel_id,
                player_id=player_id,
                p=p,
                force=True,
            )

            # record failure (0 beans)
            try:
                user = await self._users_repo.get_or_create_discord_user(
                    discord_user_id=player_id,
                    display_name=message.author.display_name,
                )
                await self._games_repo.record_game_result(
                    user_id=user.id,
                    game_key=self.key,
                    score=0,
                    beans_earned=0,
                    context_json=json.dumps(
                        {
                            "prompt": p.prompt,
                            "answer": p.answer,
                            "solved": False,
                            "final_streak": p.streak,
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                logger.exception("GeoLanguage: failed recording loss")

            if self._publisher:
                self._publisher.schedule_refresh()

            await self._send_streak_end(
                channel=message.channel,
                channel_id=channel_id,
                player_id=player_id,
                final_streak=int(p.streak),
                correct_answer=str(p.answer),
            )

            return True

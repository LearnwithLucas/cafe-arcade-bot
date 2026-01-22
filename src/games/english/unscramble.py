from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import discord

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.wordlist import WordList
from src.services.leaderboard_publisher import LeaderboardPublisher
from src.services.rewards_service import RewardsService, RewardKey
from src.utils.text import normalize_word

logger = logging.getLogger(__name__)

MAX_GUESSES = 3
MIN_LEN = 4
MAX_LEN = 8

GAME_KEY = "unscramble"


@dataclass
class _Progress:
    answer: str
    scrambled: str
    revealed: list[str | None]  # correct letters revealed in correct positions
    guesses: int
    hint_used: bool
    finished: bool
    solved: bool
    status_message_id: int | None


def _mask(revealed: list[str | None]) -> str:
    return "".join(c if c else "⬜" for c in revealed)


def _count_revealed(revealed: list[str | None]) -> int:
    return sum(1 for c in revealed if c)


class UnscrambleGame:
    """
    Unscramble per player in a shared channel.

    Rules:
      - word length 4..8 from WordList
      - 3 guesses max
      - hint (once) reveals first letter
      - each wrong guess reveals 1 new correct letter in correct spot

    Rewards (centralized via RewardsService):
      - solved: rewards.amount(UNSCRAMBLE_SOLVE)
      - failed: rewards.amount(UNSCRAMBLE_FAIL_PER_REVEALED) * revealed_letters

    UX:
      - Each player has a single panel message (edited in place).
      - When the round ends (win/lose), the panel shows a Play Again button.
    """

    key = GAME_KEY

    # -------------------------
    # UI: Play Again View
    # -------------------------

    class _PlayAgainView(discord.ui.View):
        def __init__(self, *, game: "UnscrambleGame", owner_id: int) -> None:
            super().__init__(timeout=600)  # non-persistent is fine here
            self._game = game
            self._owner_id = owner_id

        @discord.ui.button(
            label="Play again",
            style=discord.ButtonStyle.primary,
            custom_id="unscramble:play_again:v1",
        )
        async def play_again(  # type: ignore[override]
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if interaction.user.id != self._owner_id:
                await interaction.response.send_message(
                    "This button is for the player who owns this panel.",
                    ephemeral=True,
                )
                return

            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "This can only be used in a server text channel.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            await self._game.start_for_user(channel=interaction.channel, user=interaction.user)
            await interaction.followup.send("🔁 New Unscramble started!", ephemeral=True)

    # -------------------------
    # Init
    # -------------------------

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        cooldowns: Cooldowns,
        wordlist: WordList,
        allowed_channel_ids: set[int],
        leaderboard_publisher: LeaderboardPublisher | None = None,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._economy = economy
        self._rewards = rewards
        self._cooldowns = cooldowns
        self._wordlist = wordlist
        self._allowed_channel_ids = allowed_channel_ids
        self._publisher = leaderboard_publisher

        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._edit_locks: dict[tuple[int, int], asyncio.Lock] = {}

        # Kept for observability; we do not skip edits based on this.
        self._last_edit_at: dict[tuple[int, int], float] = {}

    # -------------------------
    # Locks
    # -------------------------

    def _lock_for_channel(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    def _lock_for_player(self, channel_id: int, player_id: int) -> asyncio.Lock:
        k = (channel_id, player_id)
        if k not in self._edit_locks:
            self._edit_locks[k] = asyncio.Lock()
        return self._edit_locks[k]

    # -------------------------
    # Word selection
    # -------------------------

    def _pick_word(self) -> str:
        if hasattr(self._wordlist, "random_word"):
            try:
                return str(self._wordlist.random_word(min_len=MIN_LEN, max_len=MAX_LEN)).lower()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("WordList.random_word failed; falling back to sampling .words")

        candidates = [
            w
            for w in getattr(self._wordlist, "words", [])
            if isinstance(w, str) and w.isalpha() and MIN_LEN <= len(w) <= MAX_LEN
        ]
        return (random.choice(candidates) if candidates else "apple").lower()

    @staticmethod
    def _scramble(word: str) -> str:
        letters = list(word)
        if len(set(letters)) <= 1:
            return word
        for _ in range(12):
            random.shuffle(letters)
            s = "".join(letters)
            if s != word:
                return s
        return "".join(reversed(word))

    def _new_progress(self) -> _Progress:
        answer = self._pick_word()
        return _Progress(
            answer=answer,
            scrambled=self._scramble(answer),
            revealed=[None] * len(answer),
            guesses=0,
            hint_used=False,
            finished=False,
            solved=False,
            status_message_id=None,
        )

    # -------------------------
    # State (de)serialization
    # -------------------------

    @staticmethod
    def _progress_from_dict(d: dict[str, Any]) -> _Progress:
        revealed_raw = d.get("revealed", [])
        if not isinstance(revealed_raw, list):
            revealed_raw = []
        revealed: list[str | None] = []
        for x in revealed_raw:
            if x is None:
                revealed.append(None)
            else:
                revealed.append(str(x))

        return _Progress(
            answer=str(d.get("answer", "")),
            scrambled=str(d.get("scrambled", "")),
            revealed=revealed,
            guesses=int(d.get("guesses", 0)),
            hint_used=bool(d.get("hint_used", False)),
            finished=bool(d.get("finished", False)),
            solved=bool(d.get("solved", False)),
            status_message_id=(int(d["status_message_id"]) if d.get("status_message_id") else None),
        )

    @staticmethod
    def _progress_to_dict(p: _Progress) -> dict[str, Any]:
        return {
            "answer": p.answer,
            "scrambled": p.scrambled,
            "revealed": p.revealed,
            "guesses": p.guesses,
            "hint_used": p.hint_used,
            "finished": p.finished,
            "solved": p.solved,
            "status_message_id": p.status_message_id,
        }

    # -------------------------
    # Panel rendering
    # -------------------------

    def _build_panel_embed(self, *, player_id: int, p: _Progress) -> discord.Embed:
        left = MAX_GUESSES - p.guesses
        desc = (
            f"**Scrambled:** `{p.scrambled}`\n"
            f"**Progress:** `{_mask(p.revealed)}`\n"
            f"**Guesses:** {p.guesses}/{MAX_GUESSES} (left: {max(0, left)})\n\n"
            "Type your guess in this channel.\n\n"
            "Hint: `/games unscramble_hint` (reveals the first letter once)"
        )
        if p.finished:
            if p.solved:
                desc += "\n\n✅ **Solved!**"
            else:
                desc += f"\n\n❌ **Round over.** Answer: `{p.answer}`"

        embed = discord.Embed(title="🔀 Unscramble", description=desc)
        embed.set_footer(text=f"Player: {player_id}")
        return embed

    def _view_for_progress(self, *, player_id: int, p: _Progress) -> discord.ui.View | None:
        # Only show Play Again when the round has ended
        if p.finished:
            return self._PlayAgainView(game=self, owner_id=player_id)
        return None

    async def _create_panel(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        p: _Progress,
    ) -> _Progress:
        embed = self._build_panel_embed(player_id=player_id, p=p)
        view = self._view_for_progress(player_id=player_id, p=p)
        msg = await channel.send(content=f"<@{player_id}>", embed=embed, view=view)
        p.status_message_id = msg.id
        self._last_edit_at[(channel_id, player_id)] = time.time()
        return p

    async def _edit_panel_or_recreate(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        p: _Progress,
        force: bool = True,
    ) -> _Progress:
        """
        Edits the existing panel if possible; if not found / can't edit, recreates it immediately.
        Returns possibly-updated progress (with new status_message_id).
        """
        lock = self._lock_for_player(channel_id, player_id)
        async with lock:
            view = self._view_for_progress(player_id=player_id, p=p)

            # If we don't have a message to edit, create it.
            if not p.status_message_id or not hasattr(channel, "fetch_message"):
                return await self._create_panel(channel=channel, channel_id=channel_id, player_id=player_id, p=p)

            try:
                msg = await channel.fetch_message(int(p.status_message_id))  # type: ignore[attr-defined]
                embed = self._build_panel_embed(player_id=player_id, p=p)
                await msg.edit(content=f"<@{player_id}>", embed=embed, view=view)
                self._last_edit_at[(channel_id, player_id)] = time.time()
                return p
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                # Message is gone or cannot be edited -> recreate.
                p.status_message_id = None
                return await self._create_panel(channel=channel, channel_id=channel_id, player_id=player_id, p=p)

    # -------------------------
    # Session helpers
    # -------------------------

    async def start_in_channel(self, *, channel_id: int) -> str:
        session_id = f"{self.key}:{channel_id}"
        state = {"players": {}}
        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )
        return session_id

    async def _get_or_create_session(self, *, channel_id: int) -> tuple[str, dict[str, Any]] | None:
        sess = await self._games_repo.get_active_session(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
        )
        if not sess:
            await self.start_in_channel(channel_id=channel_id)
            sess = await self._games_repo.get_active_session(
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
            )
            if not sess:
                return None
        return sess.id, json.loads(sess.state_json)

    async def _save_state(self, *, sess_id: str, channel_id: int, state: dict[str, Any]) -> None:
        await self._games_repo.upsert_active_session(
            session_id=sess_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )

    async def _reset_player(self, *, channel_id: int, player_id: int) -> None:
        sess = await self._get_or_create_session(channel_id=channel_id)
        if not sess:
            return
        sess_id, state = sess
        players: dict[str, Any] = state.get("players", {}) or {}
        players[str(player_id)] = self._progress_to_dict(self._new_progress())
        state["players"] = players
        await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

    async def _remove_player(self, *, channel_id: int, player_id: int) -> None:
        sess = await self._get_or_create_session(channel_id=channel_id)
        if not sess:
            return
        sess_id, state = sess
        players: dict[str, Any] = state.get("players", {}) or {}
        pid = str(player_id)
        if pid in players:
            del players[pid]
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

    # -------------------------
    # Public API (commands.py)
    # -------------------------

    async def start_for_user(self, *, channel: discord.abc.Messageable, user: discord.abc.User) -> None:
        if not hasattr(channel, "id"):
            return
        channel_id = int(getattr(channel, "id"))

        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return

        async with self._lock_for_channel(channel_id):
            await self._get_or_create_session(channel_id=channel_id)
            await self._reset_player(channel_id=channel_id, player_id=user.id)

            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return
            sess_id, state = sess
            players: dict[str, Any] = state.get("players", {}) or {}

            p = self._progress_from_dict(players.get(str(user.id), {}))

            # When starting, ensure no "Play again" button is shown
            p.finished = False
            p.solved = False

            p = await self._edit_panel_or_recreate(channel=channel, channel_id=channel_id, player_id=user.id, p=p, force=True)

            players[str(user.id)] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

    async def hint_for_user(self, *, channel: discord.abc.Messageable, user: discord.abc.User) -> None:
        if not hasattr(channel, "id"):
            return
        channel_id = int(getattr(channel, "id"))
        await self.use_hint(channel=channel, channel_id=channel_id, player_id=user.id)

    async def stop_for_user(self, *, channel: discord.abc.Messageable, user: discord.abc.User) -> None:
        if not hasattr(channel, "id"):
            return
        channel_id = int(getattr(channel, "id"))
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return
        async with self._lock_for_channel(channel_id):
            await self._remove_player(channel_id=channel_id, player_id=user.id)

    async def use_hint(self, *, channel: discord.abc.Messageable, channel_id: int, player_id: int) -> tuple[bool, str]:
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return False, "This channel is not enabled for Unscramble."

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return False, "No active Unscramble session."

            sess_id, state = sess
            players: dict[str, Any] = state.get("players", {}) or {}
            pid = str(player_id)

            if pid not in players:
                return False, "You don't have an active round. Start one with /games unscramble_start."

            p = self._progress_from_dict(players.get(pid, {}))

            if p.finished:
                return False, "Round already ended. Start a new one with /games unscramble_start."

            if p.hint_used:
                return False, "Hint already used for this round."

            p.hint_used = True
            if p.revealed and p.revealed[0] is None:
                p.revealed[0] = p.answer[0]

            p = await self._edit_panel_or_recreate(channel=channel, channel_id=channel_id, player_id=player_id, p=p, force=True)

            players[pid] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

            return True, f"Hint used! First letter is **{p.answer[0].upper()}**."

    # -------------------------
    # Message handling (guesses)
    # -------------------------

    async def handle_discord_message(self, message: discord.Message) -> bool:
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False

        if self._allowed_channel_ids and message.channel.id not in self._allowed_channel_ids:
            return False

        guess = normalize_word(message.content)
        if not guess.isalpha():
            return False

        channel_id = message.channel.id
        player_id = message.author.id

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return False

            sess_id, state = sess
            players: dict[str, Any] = state.get("players", {}) or {}
            pid = str(player_id)

            # Only consume messages for users with active rounds
            if pid not in players:
                return False

            p = self._progress_from_dict(players.get(pid, {}))
            if p.finished:
                return False

            # Require same length guess
            if len(guess) != len(p.answer):
                return True

            # Apply guess
            p.guesses += 1

            if guess.lower() == p.answer.lower():
                p.finished = True
                p.solved = True
            else:
                for i in range(len(p.answer)):
                    if p.revealed[i] is None:
                        p.revealed[i] = p.answer[i]
                        break
                if p.guesses >= MAX_GUESSES:
                    p.finished = True
                    p.solved = False

            # Update panel (and if finished, attach Play Again button)
            p = await self._edit_panel_or_recreate(
                channel=message.channel,
                channel_id=channel_id,
                player_id=player_id,
                p=p,
                force=True,
            )

            # Persist state
            players[pid] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

            # Clean up guess message to reduce noise
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            # Handle finish: payout + notify + schedule leaderboard refresh
            if p.finished:
                if p.solved:
                    beans = self._rewards.amount(RewardKey.UNSCRAMBLE_SOLVE)
                    await self._economy.award_beans_discord(
                        user_id=player_id,
                        amount=beans,
                        reason="Unscramble payout",
                        game_key=self.key,
                        display_name=message.author.display_name,
                    )
                    if self._publisher:
                        self._publisher.schedule_refresh()

                    await message.channel.send(
                        f"<@{player_id}> ✅ Correct! The answer was `{p.answer}` — **{beans} beans** awarded.\n"
                        "Use the **Play again** button on your panel, or `/games unscramble_start`."
                    )
                else:
                    mult = self._rewards.amount(RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED)
                    beans = mult * _count_revealed(p.revealed)
                    if beans > 0:
                        await self._economy.award_beans_discord(
                            user_id=player_id,
                            amount=beans,
                            reason="Unscramble payout (failed)",
                            game_key=self.key,
                            display_name=message.author.display_name,
                        )
                        if self._publisher:
                            self._publisher.schedule_refresh()

                    await message.channel.send(
                        f"<@{player_id}> ❌ Out of guesses! The answer was `{p.answer}`.\n"
                        f"Beans: **{beans}**\n"
                        "Use the **Play again** button on your panel, or `/games unscramble_start`."
                    )

            return True

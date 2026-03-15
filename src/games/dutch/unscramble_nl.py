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
from src.db.repo.economy_repo import GUILD_NL

logger = logging.getLogger(__name__)

MAX_GUESSES = 3
MIN_LEN = 4
MAX_LEN = 8

GAME_KEY = "unscramble_nl"


@dataclass
class _Progress:
    answer: str
    scrambled: str
    revealed: list[str | None]
    guesses: int
    hint_used: bool
    finished: bool
    solved: bool
    status_message_id: int | None


def _mask(revealed: list[str | None]) -> str:
    return "".join(c if c else "⬜" for c in revealed)


def _count_revealed(revealed: list[str | None]) -> int:
    return sum(1 for c in revealed if c)


class DutchUnscrambleGame:
    """
    Dutch Unscramble — identical logic to UnscrambleGame but uses words_nl.txt
    and awards beans to guild_id='nl'.
    """

    key = GAME_KEY

    class _SpeelOpnieuwView(discord.ui.View):
        def __init__(self, *, game: "DutchUnscrambleGame", owner_id: int) -> None:
            super().__init__(timeout=600)
            self._game = game
            self._owner_id = owner_id

        @discord.ui.button(
            label="Opnieuw spelen",
            style=discord.ButtonStyle.primary,
            custom_id="ontwar_nl:speel_opnieuw:v1",
        )
        async def speel_opnieuw(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[override]
            if interaction.user.id != self._owner_id:
                await interaction.response.send_message(
                    "Deze knop is voor de speler die dit paneel heeft.",
                    ephemeral=True,
                )
                return
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "Dit kan alleen in een server tekstkanaal.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            await self._game.start_for_user(channel=interaction.channel, user=interaction.user)
            await interaction.followup.send("🔁 Nieuw Ontwar het Woord gestart!", ephemeral=True)

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
        self._last_edit_at: dict[tuple[int, int], float] = {}

    def _lock_for_channel(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    def _lock_for_player(self, channel_id: int, player_id: int) -> asyncio.Lock:
        k = (channel_id, player_id)
        if k not in self._edit_locks:
            self._edit_locks[k] = asyncio.Lock()
        return self._edit_locks[k]

    def _pick_word(self) -> str:
        if hasattr(self._wordlist, "random_word"):
            try:
                return str(self._wordlist.random_word(min_len=MIN_LEN, max_len=MAX_LEN)).lower()  # type: ignore[attr-defined]
            except Exception:
                pass
        candidates = [
            w for w in getattr(self._wordlist, "words", [])
            if isinstance(w, str) and w.isalpha() and MIN_LEN <= len(w) <= MAX_LEN
        ]
        return (random.choice(candidates) if candidates else "fiets").lower()

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

    @staticmethod
    def _progress_from_dict(d: dict[str, Any]) -> _Progress:
        revealed_raw = d.get("revealed", [])
        if not isinstance(revealed_raw, list):
            revealed_raw = []
        revealed: list[str | None] = [str(x) if x is not None else None for x in revealed_raw]
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

    def _build_panel_embed(
        self, *, player_id: int, p: _Progress, finished: bool = False
    ) -> discord.Embed:
        mask = _mask(p.revealed)
        n_revealed = _count_revealed(p.revealed)
        guesses_left = MAX_GUESSES - p.guesses

        if finished and p.solved:
            title = "✅ Ontward!"
            desc = f"Het woord was: **{p.answer}**\n\nGoed gedaan <@{player_id}>!"
        elif finished and not p.solved:
            title = "❌ Helaas!"
            desc = f"Het woord was: **{p.answer}**\n\nDruk op **Opnieuw spelen** om het nog een keer te proberen."
        else:
            title = "🔀 Ontwar het Woord"
            desc = (
                f"**Dooreen:** `{p.scrambled.upper()}`\n"
                f"**Onthuld:** `{mask.upper()}`\n"
                f"**Pogingen over:** {guesses_left}/{MAX_GUESSES}\n"
                f"**Letters onthuld:** {n_revealed}/{len(p.answer)}\n\n"
                "Typ het woord om te raden. Elk fout antwoord onthult een letter."
            )

        embed = discord.Embed(title=title, description=desc)
        embed.set_footer(text=f"Speler: {player_id}")
        return embed

    async def _edit_panel_or_recreate(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        p: _Progress,
        force: bool = False,
    ) -> _Progress:
        embed = self._build_panel_embed(player_id=player_id, p=p, finished=p.finished)

        view: discord.ui.View | None = None
        if p.finished:
            view = self._SpeelOpnieuwView(game=self, owner_id=player_id)

        if p.status_message_id:
            k = (channel_id, player_id)
            now = time.time()
            last = self._last_edit_at.get(k, 0.0)

            if force or (now - last) >= 1.0:
                lock = self._lock_for_player(channel_id, player_id)
                async with lock:
                    try:
                        msg = await channel.fetch_message(p.status_message_id)  # type: ignore[attr-defined]
                        await msg.edit(embed=embed, view=view)
                        self._last_edit_at[k] = time.time()
                        return p
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        p.status_message_id = None

        kwargs: dict[str, Any] = {"embed": embed}
        if view:
            kwargs["view"] = view
        msg = await channel.send(**kwargs)
        p.status_message_id = msg.id
        self._last_edit_at[(channel_id, player_id)] = time.time()
        return p

    async def _get_or_create_session(
        self, *, channel_id: int
    ) -> tuple[str, dict[str, Any]] | None:
        sess = await self._games_repo.get_active_session(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
        )
        if sess:
            try:
                return sess.id, json.loads(sess.state_json)
            except Exception:
                pass

        session_id = f"{self.key}:{channel_id}"
        state: dict[str, Any] = {"players": {}}
        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )
        return session_id, state

    async def _save_state(
        self, *, sess_id: str, channel_id: int, state: dict[str, Any]
    ) -> None:
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
            p.finished = False
            p.solved = False

            p = await self._edit_panel_or_recreate(
                channel=channel, channel_id=channel_id, player_id=user.id, p=p, force=True
            )

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

    async def use_hint(
        self, *, channel: discord.abc.Messageable, channel_id: int, player_id: int
    ) -> tuple[bool, str]:
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return False, "Dit kanaal is niet ingeschakeld voor Ontwar het Woord."

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_session(channel_id=channel_id)
            if not sess:
                return False, "Geen actieve sessie."

            sess_id, state = sess
            players: dict[str, Any] = state.get("players", {}) or {}
            pid = str(player_id)

            if pid not in players:
                return False, "Je hebt geen actieve ronde. Start met `/games ontwar_start`."

            p = self._progress_from_dict(players.get(pid, {}))

            if p.finished:
                return False, "Ronde al afgelopen. Start een nieuwe met `/games ontwar_start`."

            if p.hint_used:
                return False, "Hint al gebruikt voor deze ronde."

            p.hint_used = True
            if p.revealed and p.revealed[0] is None:
                p.revealed[0] = p.answer[0]

            p = await self._edit_panel_or_recreate(
                channel=channel, channel_id=channel_id, player_id=player_id, p=p, force=True
            )

            players[pid] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

            return True, f"Hint gebruikt! De eerste letter is **{p.answer[0].upper()}**."

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

            if pid not in players:
                return False

            p = self._progress_from_dict(players.get(pid, {}))
            if p.finished:
                return False

            if len(guess) != len(p.answer):
                return True

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

            p = await self._edit_panel_or_recreate(
                channel=message.channel,
                channel_id=channel_id,
                player_id=player_id,
                p=p,
                force=True,
            )

            players[pid] = self._progress_to_dict(p)
            state["players"] = players
            await self._save_state(sess_id=sess_id, channel_id=channel_id, state=state)

            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            if p.finished:
                if p.solved:
                    beans = self._rewards.amount(RewardKey.UNSCRAMBLE_SOLVE)
                    await self._economy.award_beans_discord(
                        user_id=player_id,
                        amount=beans,
                        reason="Ontwar het Woord uitbetaling",
                        game_key=self.key,
                        display_name=message.author.display_name,
                        guild_id=GUILD_NL,
                    )
                    if self._publisher:
                        self._publisher.schedule_refresh()
                    await message.channel.send(
                        f"<@{player_id}> ✅ Correct! Het woord was `{p.answer}` — **{beans} bonen** verdiend.\n"
                        "Gebruik de **Opnieuw spelen** knop, of `/games ontwar_start`."
                    )
                else:
                    mult = self._rewards.amount(RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED)
                    beans = mult * _count_revealed(p.revealed)
                    if beans > 0:
                        await self._economy.award_beans_discord(
                            user_id=player_id,
                            amount=beans,
                            reason="Ontwar het Woord uitbetaling (mislukt)",
                            game_key=self.key,
                            display_name=message.author.display_name,
                            guild_id=GUILD_NL,
                        )
                        if self._publisher:
                            self._publisher.schedule_refresh()
                    await message.channel.send(
                        f"<@{player_id}> ❌ Geen pogingen meer! Het woord was `{p.answer}`.\n"
                        f"Bonen: **{beans}**\n"
                        "Gebruik de **Opnieuw spelen** knop, of `/games ontwar_start`."
                    )

            return True

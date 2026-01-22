from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.leaderboard_publisher import LeaderboardPublisher
from src.services.rewards_service import RewardsService, RewardKey
from src.services.wordlist import WordList
from src.utils.text import is_valid_word_shape, normalize_word


EMOJI_GREEN = "🟩"
EMOJI_YELLOW = "🟨"
EMOJI_RED = "🟥"

MAX_GUESSES = 12


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _evaluate_guess(answer: str, guess: str) -> list[str]:
    """
    Wordle-style evaluation with duplicate handling.
    Returns list of emojis length 5.
    """
    answer = answer.lower()
    guess = guess.lower()

    res = [EMOJI_RED] * 5
    remaining: dict[str, int] = {}

    # Greens
    for i in range(5):
        a = answer[i]
        g = guess[i]
        if g == a:
            res[i] = EMOJI_GREEN
        else:
            remaining[a] = remaining.get(a, 0) + 1

    # Yellows
    for i in range(5):
        if res[i] == EMOJI_GREEN:
            continue
        g = guess[i]
        if remaining.get(g, 0) > 0:
            res[i] = EMOJI_YELLOW
            remaining[g] -= 1

    return res


def _count_greens(row: list[str]) -> int:
    return sum(1 for e in row if e == EMOJI_GREEN)


def _greens_positions_from_rows(rows: list[str]) -> set[int]:
    """
    rows are display strings like: "🟥🟨🟩🟥🟨 **guess**"
    We look at the first 5 emoji and mark indices that were green at least once.
    """
    greens: set[int] = set()
    for r in rows:
        emojis = str(r)[:5]
        for i, ch in enumerate(emojis):
            if ch == EMOJI_GREEN:
                greens.add(i)
    return greens


@dataclass
class _PlayerProgress:
    guesses: list[str]
    rows: list[str]  # each row string: "🟥🟨🟩🟥🟨 **word**"
    finished: bool
    solved: bool
    best_green: int
    status_message_id: int | None

    # Hint tracking
    hint_used: bool
    hint_pos: int | None  # 0..4
    hint_letter: str | None


class WordleGame:
    """
    Shared daily puzzle per channel, per-player progress.

    Rewards (centralized via RewardsService):
      - Win: rewards.amount(wordle.solve)
      - Fail: rewards.amount(wordle.fail_per_green) * best 🟩 count achieved

    Hint:
      - /games wordle_hint can be used ONE time per player per round
      - Reveals a correct letter in a position that has not been 🟩 yet
    """

    key = "wordle"

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        cooldowns: Cooldowns,
        wordlist: WordList,
        allowed_channel_ids: set[int] | None = None,
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

        self._channel_lock: dict[int, asyncio.Lock] = {}
        self._edit_lock: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_edit_at: dict[tuple[int, int], float] = {}

    def _lock_for_channel(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_lock:
            self._channel_lock[channel_id] = asyncio.Lock()
        return self._channel_lock[channel_id]

    def _lock_for_player(self, channel_id: int, discord_user_id: int) -> asyncio.Lock:
        k = (channel_id, discord_user_id)
        if k not in self._edit_lock:
            self._edit_lock[k] = asyncio.Lock()
        return self._edit_lock[k]

    def _new_state(self, *, answer: str, date_str: str) -> dict[str, Any]:
        return {"date": date_str, "answer": answer, "players": {}}

    def _progress_from_dict(self, d: dict[str, Any]) -> _PlayerProgress:
        return _PlayerProgress(
            guesses=list(d.get("guesses", [])),
            rows=list(d.get("rows", [])),
            finished=bool(d.get("finished", False)),
            solved=bool(d.get("solved", False)),
            best_green=int(d.get("best_green", 0)),
            status_message_id=(int(d["status_message_id"]) if d.get("status_message_id") else None),
            hint_used=bool(d.get("hint_used", False)),
            hint_pos=(int(d["hint_pos"]) if d.get("hint_pos") is not None else None),
            hint_letter=(str(d["hint_letter"]) if d.get("hint_letter") is not None else None),
        )

    def _progress_to_dict(self, p: _PlayerProgress) -> dict[str, Any]:
        return {
            "guesses": p.guesses,
            "rows": p.rows,
            "finished": p.finished,
            "solved": p.solved,
            "best_green": p.best_green,
            "status_message_id": p.status_message_id,
            "hint_used": p.hint_used,
            "hint_pos": p.hint_pos,
            "hint_letter": p.hint_letter,
        }

    def _pick_random_5_letter_word(self) -> str:
        if hasattr(self._wordlist, "random_word"):
            w = getattr(self._wordlist, "random_word")(length=5)  # type: ignore[misc]
            return str(w).lower()

        for attr in ("words", "_words", "word_set"):
            if hasattr(self._wordlist, attr):
                data = getattr(self._wordlist, attr)
                try:
                    candidates = [x for x in data if isinstance(x, str) and len(x) == 5 and x.isalpha()]
                    if candidates:
                        return random.choice(candidates).lower()
                except TypeError:
                    pass

        return "speak"

    async def start_in_channel(self, *, channel_id: int) -> str:
        date_str = _utc_date_str()
        answer = self._pick_random_5_letter_word()

        await self._games_repo.end_active_in_location(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            status="ended",
        )

        session_id = f"{self.key}:{channel_id}:{date_str}"
        state = self._new_state(answer=answer, date_str=date_str)

        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )
        return session_id

    async def stop_in_channel(self, *, channel_id: int) -> bool:
        sess = await self._games_repo.get_active_session(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
        )
        if not sess:
            return False

        await self._games_repo.end_active_in_location(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            status="ended",
        )
        return True

    async def restart_in_channel(self, *, channel_id: int) -> str:
        """
        Force a new puzzle immediately (for testing/mod use).
        """
        date_str = _utc_date_str()
        answer = self._pick_random_5_letter_word()

        await self._games_repo.end_active_in_location(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            status="ended",
        )

        session_id = f"{self.key}:{channel_id}:{date_str}:r{int(time.time())}"
        state = self._new_state(answer=answer, date_str=date_str)

        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )
        return session_id

    async def _get_or_create_today_session(self, *, channel_id: int) -> tuple[str, dict[str, Any]] | None:
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

        state: dict[str, Any] = json.loads(sess.state_json)

        if state.get("date") != _utc_date_str():
            await self.start_in_channel(channel_id=channel_id)
            sess2 = await self._games_repo.get_active_session(
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
            )
            if not sess2:
                return None
            return sess2.id, json.loads(sess2.state_json)

        return sess.id, state

    def _build_player_embed(self, *, player_id: int, date_str: str, progress: _PlayerProgress) -> discord.Embed:
        attempts_used = len(progress.guesses)
        attempts_left = MAX_GUESSES - attempts_used

        board = "\n".join(progress.rows) if progress.rows else "—"

        desc = (
            f"**Daily puzzle (UTC):** `{date_str}`\n"
            f"**Attempts:** {attempts_used}/{MAX_GUESSES}  (left: {max(0, attempts_left)})\n"
        )

        if progress.hint_used and progress.hint_pos is not None and progress.hint_letter:
            desc += f"**Hint used:** Position **{progress.hint_pos + 1}** is **{progress.hint_letter.upper()}**\n"

        desc += f"\n{board}\n"

        if not progress.finished:
            desc += "\n**Type your next 5-letter guess:**"
        else:
            desc += "\n✅ **Solved!**" if progress.solved else "\n🛑 **Out of guesses.**"

        embed = discord.Embed(title="🧩 Wordle — Your Board", description=desc)
        embed.set_footer(text=f"Player: {player_id}")
        return embed

    async def _ensure_player_status_message(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        date_str: str,
        progress: _PlayerProgress,
    ) -> _PlayerProgress:
        if progress.status_message_id:
            return progress

        embed = self._build_player_embed(player_id=player_id, date_str=date_str, progress=progress)
        msg = await channel.send(content=f"<@{player_id}>", embed=embed)
        progress.status_message_id = msg.id
        self._last_edit_at[(channel_id, player_id)] = 0.0
        return progress

    async def _edit_player_status_message(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
        date_str: str,
        progress: _PlayerProgress,
        force: bool = False,
    ) -> None:
        if not progress.status_message_id or not hasattr(channel, "fetch_message"):
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
                msg = await channel.fetch_message(int(progress.status_message_id))  # type: ignore[attr-defined]
                embed = self._build_player_embed(player_id=player_id, date_str=date_str, progress=progress)
                await msg.edit(content=f"<@{player_id}>", embed=embed)
                self._last_edit_at[key] = time.time()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

    async def use_hint(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_id: int,
    ) -> tuple[bool, str]:
        """
        1 hint per player per round.
        Reveals answer[pos] where pos is NOT green yet for that player.
        """
        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_today_session(channel_id=channel_id)
            if not sess:
                return False, "No active Wordle session in this channel."

            sess_id, state = sess
            date_str = str(state["date"])
            answer = str(state["answer"]).lower()

            players: dict[str, Any] = state.get("players", {}) or {}
            pid = str(player_id)
            progress = self._progress_from_dict(players.get(pid, {}))

            if progress.finished:
                return False, "Your Wordle is already finished."
            if progress.hint_used:
                return False, "You already used your hint for this round."

            progress = await self._ensure_player_status_message(
                channel=channel,
                channel_id=channel_id,
                player_id=player_id,
                date_str=date_str,
                progress=progress,
            )

            greens = _greens_positions_from_rows(progress.rows)
            candidates = [i for i in range(5) if i not in greens]
            if not candidates:
                return False, "All positions are already green — no hint available."

            pos = random.choice(candidates)
            letter = answer[pos]

            progress.hint_used = True
            progress.hint_pos = pos
            progress.hint_letter = letter

            players[pid] = self._progress_to_dict(progress)
            state["players"] = players

            await self._games_repo.upsert_active_session(
                session_id=sess_id,
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
                state=state,
            )

            await self._edit_player_status_message(
                channel=channel,
                channel_id=channel_id,
                player_id=player_id,
                date_str=date_str,
                progress=progress,
                force=True,
            )

            return True, f"Position **{pos + 1}** is **{letter.upper()}**."

    async def _send_finished_embed(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        player_discord_id: int,
        date_str: str,
        answer: str,
        beans: int,
        attempts: int,
        best_green: int,
        solved: bool,
    ) -> None:
        """
        Sends a completion embed (both solved and failed) with a Play Again button.
        Play Again resets only this player for the same daily puzzle.
        """

        outer = self

        class PlayAgainView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)

            @discord.ui.button(label="Play again", style=discord.ButtonStyle.success, emoji="🔁")
            async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[override]
                if interaction.user.id != player_discord_id:
                    await interaction.response.send_message(
                        "This button is for the player who finished this round.",
                        ephemeral=True,
                    )
                    return

                async with outer._lock_for_channel(channel_id):
                    sess = await outer._get_or_create_today_session(channel_id=channel_id)
                    if not sess:
                        await interaction.response.send_message("Couldn’t restart Wordle right now.", ephemeral=True)
                        return

                    sess_id, state = sess
                    players = state.get("players", {}) or {}
                    players.pop(str(player_discord_id), None)
                    state["players"] = players

                    await outer._games_repo.upsert_active_session(
                        session_id=sess_id,
                        platform="discord",
                        location_id=str(channel_id),
                        thread_id=None,
                        game_key=outer.key,
                        state=state,
                    )

                await interaction.response.send_message("✅ Reset! Type your first 5-letter guess:", ephemeral=True)

        title = "🟩 Wordle Solved!" if solved else "🟥 Wordle Over!"
        status_line = "✅ **Solved**" if solved else "🛑 **Out of guesses**"

        embed = discord.Embed(
            title=title,
            description=(
                f"Ended for <@{player_discord_id}> — {status_line}\n\n"
                f"**Puzzle (UTC):** `{date_str}`\n"
                f"**Answer:** `{answer}`\n"
                f"**Attempts:** {attempts}/{MAX_GUESSES}\n"
                f"**Best 🟩 (correct spot):** {best_green}\n"
                f"**Beans awarded:** **{beans}**\n\n"
                "Press **Play again** to restart your board for today’s puzzle."
            ),
        )
        await channel.send(embed=embed, view=PlayAgainView())

    async def handle_discord_message(self, message: discord.Message) -> bool:
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False

        if self._allowed_channel_ids is not None and message.channel.id not in self._allowed_channel_ids:
            return False

        channel_id = message.channel.id

        guess = normalize_word(message.content)
        if not is_valid_word_shape(guess, min_len=5) or len(guess) != 5:
            return False

        if not self._wordlist.is_word(guess):
            return False

        async with self._lock_for_channel(channel_id):
            sess = await self._get_or_create_today_session(channel_id=channel_id)
            if not sess:
                return False

            sess_id, state = sess
            date_str = str(state["date"])
            answer = str(state["answer"]).lower()

            players: dict[str, Any] = state.get("players", {}) or {}
            pid_str = str(message.author.id)
            progress = self._progress_from_dict(players.get(pid_str, {}))

            if progress.finished:
                return True
            if guess in progress.guesses:
                return True

            progress = await self._ensure_player_status_message(
                channel=message.channel,
                channel_id=channel_id,
                player_id=message.author.id,
                date_str=date_str,
                progress=progress,
            )

            row = _evaluate_guess(answer, guess)
            row_str = "".join(row)

            progress.guesses.append(guess)
            progress.rows.append(f"{row_str} **{guess}**")
            progress.best_green = max(progress.best_green, _count_greens(row))

            solved = guess == answer
            out_of_guesses = len(progress.guesses) >= MAX_GUESSES

            if solved:
                progress.finished = True
                progress.solved = True
            elif out_of_guesses:
                progress.finished = True
                progress.solved = False

            players[pid_str] = self._progress_to_dict(progress)
            state["players"] = players

            await self._games_repo.upsert_active_session(
                session_id=sess_id,
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
                state=state,
            )

            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            await self._edit_player_status_message(
                channel=message.channel,
                channel_id=channel_id,
                player_id=message.author.id,
                date_str=date_str,
                progress=progress,
                force=progress.finished,
            )

            if progress.finished:
                if progress.solved:
                    beans = self._rewards.amount(RewardKey.WORDLE_SOLVE)
                else:
                    per_green = self._rewards.amount(RewardKey.WORDLE_FAIL_PER_GREEN)
                    beans = per_green * int(progress.best_green)

                user = await self._users_repo.get_or_create_discord_user(
                    discord_user_id=message.author.id,
                    display_name=message.author.display_name,
                )

                await self._economy.award_beans_discord(
                    user_id=message.author.id,
                    amount=beans,
                    reason="Wordle payout",
                    game_key=self.key,
                    display_name=message.author.display_name,
                    metadata=json.dumps(
                        {
                            "date": date_str,
                            "solved": progress.solved,
                            "attempts": len(progress.guesses),
                            "best_green": progress.best_green,
                            "hint_used": progress.hint_used,
                            "hint_pos": progress.hint_pos,
                        }
                    ),
                )

                await self._games_repo.record_game_result(
                    user_id=user.id,
                    game_key=self.key,
                    score=beans,
                    beans_earned=beans,
                    context_json=json.dumps(
                        {
                            "date": date_str,
                            "solved": progress.solved,
                            "attempts": len(progress.guesses),
                            "guesses": progress.guesses,
                            "rows": progress.rows,
                            "best_green": progress.best_green,
                            "hint_used": progress.hint_used,
                            "hint_pos": progress.hint_pos,
                        },
                        ensure_ascii=False,
                    ),
                )

                if self._publisher:
                    self._publisher.schedule_refresh()

                if progress.solved:
                    await message.channel.send(
                        f"✅ <@{message.author.id}> solved today’s Wordle in **{len(progress.guesses)}** guesses "
                        f"and earned **{beans} beans**!"
                    )
                else:
                    await message.channel.send(
                        f"🟥 <@{message.author.id}> used all {MAX_GUESSES} guesses. "
                        f"Best correct letters (right spot): **{progress.best_green}** → **{beans} beans**."
                    )

                await self._send_finished_embed(
                    channel=message.channel,
                    channel_id=channel_id,
                    player_discord_id=message.author.id,
                    date_str=date_str,
                    answer=answer,
                    beans=beans,
                    attempts=len(progress.guesses),
                    best_green=progress.best_green,
                    solved=progress.solved,
                )

            return True

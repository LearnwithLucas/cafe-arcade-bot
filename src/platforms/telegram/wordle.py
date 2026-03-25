from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.economy_service import EconomyService
from src.services.rewards_service import RewardsService, RewardKey
from src.services.wordlist import WordList

log = logging.getLogger("telegram.wordle")

EMOJI_GREEN = "\U0001f7e9"
EMOJI_YELLOW = "\U0001f7e8"
EMOJI_RED = "\U0001f7e5"
MAX_GUESSES = 12
PLATFORM = "telegram"


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _evaluate_guess(answer: str, guess: str) -> list[str]:
    answer = answer.lower()
    guess = guess.lower()
    res = [EMOJI_RED] * 5
    remaining: dict[str, int] = {}
    for i in range(5):
        a, g = answer[i], guess[i]
        if g == a:
            res[i] = EMOJI_GREEN
        else:
            remaining[a] = remaining.get(a, 0) + 1
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


class TelegramWordleGame:
    key = "wordle_tg"

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        wordlist: WordList,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._economy = economy
        self._rewards = rewards
        self._wordlist = wordlist

    def _pick_word(self) -> str:
        for attr in ("words", "_words", "word_set"):
            if hasattr(self._wordlist, attr):
                data = getattr(self._wordlist, attr)
                try:
                    candidates = [x for x in data if isinstance(x, str) and len(x) == 5 and x.isalpha()]
                    if candidates:
                        return random.choice(candidates).lower()
                except TypeError:
                    pass
        return "crane"

    async def _get_or_create_session(self, chat_id: int) -> tuple[str, dict[str, Any]] | None:
        location_id = str(chat_id)
        sess = await self._games_repo.get_active_session(
            platform=PLATFORM, location_id=location_id, thread_id=None, game_key=self.key
        )
        if sess:
            try:
                state = json.loads(sess.state_json)
                if state.get("date") == _utc_date_str():
                    return sess.id, state
            except Exception:
                pass

        # New session
        date_str = _utc_date_str()
        answer = self._pick_word()
        await self._games_repo.end_active_in_location(
            platform=PLATFORM, location_id=location_id, thread_id=None,
            game_key=self.key, status="ended"
        )
        session_id = f"{self.key}:{chat_id}:{date_str}"
        state = {"date": date_str, "answer": answer, "players": {}}
        await self._games_repo.upsert_active_session(
            session_id=session_id, platform=PLATFORM, location_id=location_id,
            thread_id=None, game_key=self.key, state=state
        )
        return session_id, state

    async def is_finished(self, chat_id: int, user_id: int) -> bool:
        result = await self._get_or_create_session(chat_id)
        if not result:
            return True
        _, state = result
        players = state.get("players", {})
        progress = players.get(str(user_id), {})
        return bool(progress.get("finished", False))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        result = await self._get_or_create_session(chat_id)
        if not result:
            await update.message.reply_text("Could not start Wordle. Try again.")
            return
        _, state = result
        await update.message.reply_text(
            f"Wordle started for {state['date']}.\n"
            "Guess the 5-letter word — you have 12 tries.\n"
            "Just type your guess."
        )

    async def cmd_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        result = await self._get_or_create_session(chat_id)
        if not result:
            await update.message.reply_text("No active Wordle. Use /wordle to start.")
            return
        sess_id, state = result
        players = state.get("players", {})
        progress = players.get(str(user_id), {})
        if progress.get("finished"):
            await update.message.reply_text("Your game is already finished.")
            return
        answer = state["answer"]
        guesses = progress.get("guesses", [])
        # Reveal first unguessed letter
        for i, letter in enumerate(answer):
            if not any(len(g) > i and g[i] == letter for g in guesses):
                await update.message.reply_text(f"Hint: position {i+1} is '{letter.upper()}'")
                return
        await update.message.reply_text("No more hints available.")

    async def handle_guess(self, update: Update, context: ContextTypes.DEFAULT_TYPE, guess: str) -> bool:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.first_name or str(user_id)

        if len(guess) != 5 or not guess.isalpha():
            return False

        if not self._wordlist.is_word(guess):
            await update.message.reply_text(f"'{guess}' is not in the word list.")
            return True

        result = await self._get_or_create_session(chat_id)
        if not result:
            return False
        sess_id, state = result

        date_str = state["date"]
        answer = state["answer"].lower()
        players: dict[str, Any] = state.get("players", {}) or {}
        pid = str(user_id)
        progress = players.get(pid, {"guesses": [], "rows": [], "finished": False, "solved": False, "best_green": 0})

        if progress["finished"]:
            await update.message.reply_text("Your game is already done. Wait for tomorrow's puzzle or use /wordle.")
            return True

        if guess in progress["guesses"]:
            await update.message.reply_text("You already tried that word.")
            return True

        row = _evaluate_guess(answer, guess)
        row_str = "".join(row) + f" {guess.upper()}"
        progress["guesses"].append(guess)
        progress["rows"].append(row_str)
        progress["best_green"] = max(progress.get("best_green", 0), _count_greens(row))

        solved = guess == answer
        out_of_guesses = len(progress["guesses"]) >= MAX_GUESSES

        if solved:
            progress["finished"] = True
            progress["solved"] = True
        elif out_of_guesses:
            progress["finished"] = True
            progress["solved"] = False

        players[pid] = progress
        state["players"] = players

        await self._games_repo.upsert_active_session(
            session_id=sess_id, platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key, state=state
        )

        board = "\n".join(progress["rows"])
        attempts = len(progress["guesses"])

        if solved:
            beans = self._rewards.amount(RewardKey.WORDLE_SOLVE)
            user = await self._users_repo.get_or_create_discord_user(
                discord_user_id=user_id, display_name=username
            )
            await self._economy.award_beans_discord(
                user_id=user_id, amount=beans, reason="Wordle TG",
                game_key=self.key, display_name=username, guild_id="en",
                metadata=json.dumps({"date": date_str, "solved": True, "attempts": attempts})
            )
            await update.message.reply_text(
                f"{board}\n\n"
                f"Solved in {attempts} guess{'es' if attempts != 1 else ''}! "
                f"You earned {beans} beans."
            )
        elif out_of_guesses:
            per_green = self._rewards.amount(RewardKey.WORDLE_FAIL_PER_GREEN)
            beans = per_green * progress["best_green"]
            await update.message.reply_text(
                f"{board}\n\n"
                f"Out of guesses. The word was {answer.upper()}.\n"
                f"Best: {progress['best_green']} green letters. You earned {beans} beans."
            )
        else:
            left = MAX_GUESSES - attempts
            await update.message.reply_text(f"{board}\n\n{left} guess{'es' if left != 1 else ''} left.")

        return True
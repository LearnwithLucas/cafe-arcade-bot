from __future__ import annotations

import json
import logging
import random
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.economy_service import EconomyService
from src.services.rewards_service import RewardsService, RewardKey
from src.services.wordlist import WordList

log = logging.getLogger("telegram.unscramble")

PLATFORM = "telegram"
MAX_GUESSES = 3
MIN_LEN = 5
MAX_LEN = 8


def _scramble(word: str) -> str:
    letters = list(word)
    for _ in range(20):
        random.shuffle(letters)
        if "".join(letters) != word:
            break
    return "".join(letters)


class TelegramUnscrambleGame:
    key = "unscramble_tg"

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
                    candidates = [
                        x for x in data
                        if isinstance(x, str) and MIN_LEN <= len(x) <= MAX_LEN and x.isalpha()
                    ]
                    if candidates:
                        return random.choice(candidates).lower()
                except TypeError:
                    pass
        return "garden"

    async def _get_active(self, chat_id: int, user_id: int) -> tuple[str, dict] | None:
        sess = await self._games_repo.get_active_session(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=str(user_id), game_key=self.key
        )
        if not sess:
            return None
        try:
            return sess.id, json.loads(sess.state_json)
        except Exception:
            return None

    async def is_finished(self, chat_id: int, user_id: int) -> bool:
        result = await self._get_active(chat_id, user_id)
        if not result:
            return True
        _, state = result
        return bool(state.get("finished", False))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        word = self._pick_word()
        scrambled = _scramble(word)
        state = {"answer": word, "scrambled": scrambled, "guesses": 0, "finished": False}

        await self._games_repo.end_active_in_location(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=str(user_id), game_key=self.key, status="ended"
        )
        session_id = f"{self.key}:{chat_id}:{user_id}:{int(__import__('time').time())}"
        await self._games_repo.upsert_active_session(
            session_id=session_id, platform=PLATFORM, location_id=str(chat_id),
            thread_id=str(user_id), game_key=self.key, state=state
        )
        await update.message.reply_text(
            f"Unscramble this word:\n\n*{scrambled.upper()}*\n\n"
            f"You have {MAX_GUESSES} guesses. Just type your answer.\n"
            "Use /hint for a clue or /skip to give up.",
            parse_mode="Markdown"
        )

    async def cmd_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        result = await self._get_active(chat_id, user_id)
        if not result:
            await update.message.reply_text("No active game. Use /unscramble to start.")
            return
        _, state = result
        answer = state["answer"]
        await update.message.reply_text(f"Hint: the word starts with '{answer[0].upper()}' and has {len(answer)} letters.")

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        result = await self._get_active(chat_id, user_id)
        if not result:
            await update.message.reply_text("No active game. Use /unscramble to start.")
            return
        _, state = result
        await self._games_repo.end_active_in_location(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=str(user_id), game_key=self.key, status="ended"
        )
        await update.message.reply_text(
            f"Skipped. The word was *{state['answer'].upper()}*.\nUse /unscramble to try another.",
            parse_mode="Markdown"
        )

    async def handle_guess(self, update: Update, context: ContextTypes.DEFAULT_TYPE, guess: str) -> bool:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.first_name or str(user_id)

        result = await self._get_active(chat_id, user_id)
        if not result:
            return False

        sess_id, state = result
        if state.get("finished"):
            return False

        answer = state["answer"]
        if sorted(guess) != sorted(answer):
            return False  # not plausibly an attempt at this word

        state["guesses"] = state.get("guesses", 0) + 1
        guesses_used = state["guesses"]

        if guess == answer:
            state["finished"] = True
            await self._games_repo.upsert_active_session(
                session_id=sess_id, platform=PLATFORM, location_id=str(chat_id),
                thread_id=str(user_id), game_key=self.key, state=state
            )
            beans = self._rewards.amount(RewardKey.UNSCRAMBLE_SOLVE) if guesses_used == 1 else max(5, 20 - (guesses_used - 1) * 5)
            await self._economy.award_beans_discord(
                user_id=user_id, amount=beans, reason="Unscramble TG",
                game_key=self.key, display_name=username, guild_id="en",
                metadata=json.dumps({"solved": True, "guesses": guesses_used})
            )
            await update.message.reply_text(
                f"Correct! The word was *{answer.upper()}*.\n"
                f"Solved in {guesses_used} guess{'es' if guesses_used != 1 else ''}. "
                f"You earned {beans} beans.\n\nUse /unscramble to play again.",
                parse_mode="Markdown"
            )
            return True

        if guesses_used >= MAX_GUESSES:
            state["finished"] = True
            await self._games_repo.upsert_active_session(
                session_id=sess_id, platform=PLATFORM, location_id=str(chat_id),
                thread_id=str(user_id), game_key=self.key, state=state
            )
            await update.message.reply_text(
                f"Out of guesses. The word was *{answer.upper()}*.\nUse /unscramble to try again.",
                parse_mode="Markdown"
            )
            return True

        left = MAX_GUESSES - guesses_used
        await self._games_repo.upsert_active_session(
            session_id=sess_id, platform=PLATFORM, location_id=str(chat_id),
            thread_id=str(user_id), game_key=self.key, state=state
        )
        await update.message.reply_text(
            f"Not quite. {left} guess{'es' if left != 1 else ''} left.\n"
            f"The scrambled word is: *{state['scrambled'].upper()}*",
            parse_mode="Markdown"
        )
        return True
from __future__ import annotations

import json
import logging
import random
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.wordlist import WordList

log = logging.getLogger("telegram.word_chain")

PLATFORM = "telegram"
class TelegramWordChainGame:
    key = "word_chain_tg"

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        wordlist: WordList,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._wordlist = wordlist

    def _starter_word(self) -> str:
        starters = ["apple", "brave", "crane", "drift", "earth", "flame", "globe", "house"]
        return random.choice(starters)

    async def _get_session(self, chat_id: int) -> tuple[str, dict] | None:
        sess = await self._games_repo.get_active_session(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key
        )
        if not sess:
            return None
        try:
            return sess.id, json.loads(sess.state_json)
        except Exception:
            return None

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        starter = self._starter_word()
        state = {
            "last_word": starter,
            "used_words": [starter],
            "chain_length": 1,
        }
        await self._games_repo.end_active_in_location(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key, status="ended"
        )
        session_id = f"{self.key}:{chat_id}:{int(__import__('time').time())}"
        await self._games_repo.upsert_active_session(
            session_id=session_id, platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key, state=state
        )
        await update.message.reply_text(
            f"Word Chain started!\n\n"
            f"Starting word: *{starter.upper()}*\n\n"
            f"Type a word that starts with *{starter[-1].upper()}*.\n"
            f"Each word must start with the last letter of the previous word.\n"
            f"Use /stopchain to end the game.",
            parse_mode="Markdown"
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        result = await self._get_session(chat_id)
        if not result:
            await update.message.reply_text("No active Word Chain. Use /wordchain to start.")
            return
        _, state = result
        await self._games_repo.end_active_in_location(
            platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key, status="ended"
        )
        length = state.get("chain_length", 1)
        await update.message.reply_text(
            f"Word Chain ended. You built a chain of *{length}* words.",
            parse_mode="Markdown"
        )

    async def handle_word(self, update: Update, context: ContextTypes.DEFAULT_TYPE, word: str) -> bool:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        result = await self._get_session(chat_id)
        if not result:
            return False

        sess_id, state = result
        last_word = state["last_word"]
        used_words = state.get("used_words", [])

        if word[0] != last_word[-1]:
            return False  # doesn't match — not a chain attempt

        if not self._wordlist.is_word(word):
            await update.message.reply_text(f"'{word}' is not in the word list.")
            return True

        if word in used_words:
            await update.message.reply_text(f"'{word}' was already used in this chain.")
            return True

        used_words.append(word)
        state["last_word"] = word
        state["used_words"] = used_words
        state["chain_length"] = state.get("chain_length", 1) + 1

        await self._games_repo.upsert_active_session(
            session_id=sess_id, platform=PLATFORM, location_id=str(chat_id),
            thread_id=None, game_key=self.key, state=state
        )

        next_letter = word[-1].upper()
        await update.message.reply_text(
            f"*{word.upper()}* ✓\n"
            f"Chain: {state['chain_length']} words\n"
            f"Next word must start with *{next_letter}*",
            parse_mode="Markdown"
        )
        return True
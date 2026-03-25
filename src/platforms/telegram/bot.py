from __future__ import annotations

import logging
import os
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.platforms.telegram.wordle import TelegramWordleGame
from src.platforms.telegram.unscramble import TelegramUnscrambleGame
from src.platforms.telegram.word_chain import TelegramWordChainGame

log = logging.getLogger("telegram.bot")


def _allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return result


def _discovery_mode() -> bool:
    return os.getenv("TG_DISCOVERY_MODE", "0").strip() == "1"


class TelegramBot:
    def __init__(
        self,
        *,
        token: str,
        wordle: TelegramWordleGame,
        unscramble: TelegramUnscrambleGame,
        word_chain: TelegramWordChainGame,
    ) -> None:
        self._token = token
        self._wordle = wordle
        self._unscramble = unscramble
        self._word_chain = word_chain
        self._app: Application | None = None

    def _is_allowed(self, chat_id: int) -> bool:
        allowed = _allowed_chat_ids()
        if not allowed:
            # No whitelist set — only allow if discovery mode is on
            return _discovery_mode()
        return chat_id in allowed

    async def _guard(self, update: Update) -> bool:
        chat_id = update.effective_chat.id
        if not self._is_allowed(chat_id):
            log.warning("TG: blocked message from chat_id=%s", chat_id)
            return False
        return True

    # ---- Commands ----

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id

        if _discovery_mode():
            await update.message.reply_text(
                f"Chat ID: {chat_id}\n\n"
                "Add this to your TELEGRAM_ALLOWED_CHAT_IDS environment variable, "
                "then set TG_DISCOVERY_MODE=0 and redeploy."
            )
            log.info("TG discovery: chat_id=%s", chat_id)
            return

        if not self._is_allowed(chat_id):
            return

        await update.message.reply_text(
            "Jerry The Duck reporting for duty.\n\n"
            "Games available:\n"
            "/wordle — guess the 5-letter word\n"
            "/unscramble — unscramble a word\n"
            "/wordchain — build a word chain\n\n"
            "/help for more info."
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "Jerry The Duck — word games\n\n"
            "Wordle:\n"
            "/wordle — start today's puzzle\n"
            "/hint — get a letter hint\n\n"
            "Unscramble:\n"
            "/unscramble — start a new game\n"
            "/hint — get a hint\n"
            "/skip — reveal the answer and skip\n\n"
            "Word Chain:\n"
            "/wordchain — start a chain\n"
            "/stopchain — end the chain\n\n"
            "Just type your guess as a regular message during any game."
        )

    async def cmd_wordle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._wordle.cmd_start(update, context)

    async def cmd_unscramble(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._unscramble.cmd_start(update, context)

    async def cmd_wordchain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._word_chain.cmd_start(update, context)

    async def cmd_stopchain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._word_chain.cmd_stop(update, context)

    async def cmd_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        # Try Wordle hint first, then Unscramble
        await self._wordle.cmd_hint(update, context)

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._unscramble.cmd_skip(update, context)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not await self._guard(update):
            return

        text = update.message.text.strip().lower()
        if not text or text.startswith("/"):
            return

        # Try word chain first (any valid word counts if a chain is active)
        if await self._word_chain.handle_word(update, context, text):
            return

        # Try Wordle (5-letter words)
        if len(text) == 5 and text.isalpha():
            if await self._wordle.handle_guess(update, context, text):
                return

        # Try Unscramble
        if text.isalpha():
            await self._unscramble.handle_guess(update, context, text)

    async def run(self) -> None:
        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )

        app = self._app
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("wordle", self.cmd_wordle))
        app.add_handler(CommandHandler("unscramble", self.cmd_unscramble))
        app.add_handler(CommandHandler("wordchain", self.cmd_wordchain))
        app.add_handler(CommandHandler("stopchain", self.cmd_stopchain))
        app.add_handler(CommandHandler("hint", self.cmd_hint))
        app.add_handler(CommandHandler("skip", self.cmd_skip))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        log.info("Telegram bot starting (discovery_mode=%s)", _discovery_mode())
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("Telegram bot running as @JerrytheDuckBot")
            # Keep running until cancelled
            import asyncio
            await asyncio.Event().wait()

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            log.info("Telegram bot stopped.")


def build_telegram_bot(*, services: dict[str, Any], token: str) -> TelegramBot:
    return TelegramBot(
        token=token,
        wordle=TelegramWordleGame(
            games_repo=services["games_repo"],
            users_repo=services["users_repo"],
            economy=services["economy"],
            rewards=services["rewards"],
            wordlist=services["wordlist"],
        ),
        unscramble=TelegramUnscrambleGame(
            games_repo=services["games_repo"],
            users_repo=services["users_repo"],
            economy=services["economy"],
            rewards=services["rewards"],
            wordlist=services["wordlist"],
        ),
        word_chain=TelegramWordChainGame(
            games_repo=services["games_repo"],
            users_repo=services["users_repo"],
            economy=services["economy"],
            rewards=services["rewards"],
            wordlist=services["wordlist"],
        ),
    )

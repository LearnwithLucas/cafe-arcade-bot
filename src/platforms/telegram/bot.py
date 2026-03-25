from __future__ import annotations

import asyncio
import logging
import os
import time
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

AFK_TIMEOUT_SECONDS = 3 * 60  # 3 minutes


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


def _admin_user_ids() -> set[int]:
    """Lucas's Telegram user ID — set TG_ADMIN_USER_IDS env var."""
    raw = os.getenv("TG_ADMIN_USER_IDS", "").strip()
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        try:
            result.add(int(part.strip()))
        except ValueError:
            pass
    return result


def _discovery_mode() -> bool:
    return os.getenv("TG_DISCOVERY_MODE", "0").strip() == "1"


# ---- Active game tracking ----
# Tracks which user is currently playing which game in which chat.
# Key: (chat_id, game_key) -> {user_id, last_active}
# Only one player per game per chat at a time.
_active_games: dict[tuple[int, str], dict] = {}


def _claim_game(chat_id: int, user_id: int, game_key: str) -> bool:
    """Returns True if user can play. False if another user is already playing."""
    key = (chat_id, game_key)
    existing = _active_games.get(key)
    now = time.time()

    if existing:
        # Same user — update last_active
        if existing["user_id"] == user_id:
            existing["last_active"] = now
            return True
        # Different user — check AFK
        if now - existing["last_active"] < AFK_TIMEOUT_SECONDS:
            return False  # someone else is active
        # AFK expired — allow takeover
    _active_games[key] = {"user_id": user_id, "last_active": now}
    return True


def _touch_game(chat_id: int, user_id: int, game_key: str) -> None:
    key = (chat_id, game_key)
    if key in _active_games and _active_games[key]["user_id"] == user_id:
        _active_games[key]["last_active"] = time.time()


def _release_game(chat_id: int, user_id: int, game_key: str) -> None:
    key = (chat_id, game_key)
    if key in _active_games and _active_games[key]["user_id"] == user_id:
        del _active_games[key]


def _who_is_playing(chat_id: int, game_key: str) -> int | None:
    key = (chat_id, game_key)
    existing = _active_games.get(key)
    if not existing:
        return None
    if time.time() - existing["last_active"] >= AFK_TIMEOUT_SECONDS:
        del _active_games[key]
        return None
    return existing["user_id"]


def _afk_seconds_left(chat_id: int, game_key: str) -> int:
    key = (chat_id, game_key)
    existing = _active_games.get(key)
    if not existing:
        return 0
    remaining = AFK_TIMEOUT_SECONDS - (time.time() - existing["last_active"])
    return max(0, int(remaining))


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
            return _discovery_mode()
        return chat_id in allowed

    async def _guard(self, update: Update) -> bool:
        if not self._is_allowed(update.effective_chat.id):
            log.warning("TG: blocked message from chat_id=%s", update.effective_chat.id)
            return False
        return True

    # ---- Commands ----

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        user = update.effective_user
        is_admin = user and user.id in _admin_user_ids()

        if _discovery_mode() or is_admin:
            await update.message.reply_text(
                f"Chat ID: `{chat_id}`\n\n"
                "Add this to TELEGRAM\\_ALLOWED\\_CHAT\\_IDS on Render, "
                "then set TG\\_DISCOVERY\\_MODE=0 and redeploy.",
                parse_mode="Markdown"
            )
            log.info("TG /start: chat_id=%s user=%s", chat_id, user.id if user else "?")

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
            "One player per game at a time. "
            "If a player goes quiet for 3 minutes the slot opens up.\n\n"
            "Type your guess as a plain message during any active game."
        )

    async def cmd_wordle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.first_name or str(user_id)

        current = _who_is_playing(chat_id, "wordle_tg")
        if current and current != user_id:
            left = _afk_seconds_left(chat_id, "wordle_tg")
            await update.message.reply_text(
                f"Someone else is playing Wordle right now. "
                f"Their slot opens in {left // 60}m {left % 60}s if they go quiet."
            )
            return

        if not _claim_game(chat_id, user_id, "wordle_tg"):
            await update.message.reply_text("Wordle is busy right now. Try again shortly.")
            return

        await self._wordle.cmd_start(update, context)

    async def cmd_unscramble(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        current = _who_is_playing(chat_id, "unscramble_tg")
        if current and current != user_id:
            left = _afk_seconds_left(chat_id, "unscramble_tg")
            await update.message.reply_text(
                f"Someone else is playing Unscramble right now. "
                f"Their slot opens in {left // 60}m {left % 60}s if they go quiet."
            )
            return

        if not _claim_game(chat_id, user_id, "unscramble_tg"):
            await update.message.reply_text("Unscramble is busy right now. Try again shortly.")
            return

        await self._unscramble.cmd_start(update, context)

    async def cmd_wordchain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        current = _who_is_playing(chat_id, "word_chain_tg")
        if current and current != user_id:
            left = _afk_seconds_left(chat_id, "word_chain_tg")
            await update.message.reply_text(
                f"Someone else is playing Word Chain right now. "
                f"Their slot opens in {left // 60}m {left % 60}s if they go quiet."
            )
            return

        if not _claim_game(chat_id, user_id, "word_chain_tg"):
            await update.message.reply_text("Word Chain is busy right now. Try again shortly.")
            return

        await self._word_chain.cmd_start(update, context)

    async def cmd_stopchain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        _release_game(chat_id, user_id, "word_chain_tg")
        await self._word_chain.cmd_stop(update, context)

    async def cmd_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # Hint for whichever game this user is playing
        if _who_is_playing(chat_id, "unscramble_tg") == user_id:
            await self._unscramble.cmd_hint(update, context)
        elif _who_is_playing(chat_id, "wordle_tg") == user_id:
            await self._wordle.cmd_hint(update, context)
        else:
            await update.message.reply_text("No active game to hint. Start one with /wordle or /unscramble.")

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        _release_game(chat_id, user_id, "unscramble_tg")
        await self._unscramble.cmd_skip(update, context)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not await self._guard(update):
            return

        text = update.message.text.strip().lower()
        if not text or text.startswith("/"):
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # Route to the game this user is currently playing
        wordle_player = _who_is_playing(chat_id, "wordle_tg")
        unscramble_player = _who_is_playing(chat_id, "unscramble_tg")
        chain_player = _who_is_playing(chat_id, "word_chain_tg")

        # Word chain — only if this user is the chain player
        if chain_player == user_id:
            _touch_game(chat_id, user_id, "word_chain_tg")
            handled = await self._word_chain.handle_word(update, context, text)
            if handled:
                return

        # Wordle — only if this user is the wordle player
        if wordle_player == user_id and len(text) == 5 and text.isalpha():
            _touch_game(chat_id, user_id, "wordle_tg")
            handled = await self._wordle.handle_guess(update, context, text)
            if handled:
                # Release if finished
                if await self._wordle.is_finished(chat_id, user_id):
                    _release_game(chat_id, user_id, "wordle_tg")
                return

        # Unscramble — only if this user is the unscramble player
        if unscramble_player == user_id and text.isalpha():
            _touch_game(chat_id, user_id, "unscramble_tg")
            handled = await self._unscramble.handle_guess(update, context, text)
            if handled:
                if await self._unscramble.is_finished(chat_id, user_id):
                    _release_game(chat_id, user_id, "unscramble_tg")
                return

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
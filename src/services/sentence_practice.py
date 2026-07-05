from __future__ import annotations

import json
import logging
import re
from typing import Any

import discord

from src.config.channels import UNSCRAMBLE_CHANNEL_ID, WORDLE_CHANNEL_ID, WORD_CHAIN_CHANNEL_ID


logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[^A-Za-z' -]+")
_TRACKED_GAME_KEYS = ("wordle", "unscramble", "word_chain")
_ALLOWED_CHANNEL_IDS = {WORDLE_CHANNEL_ID, UNSCRAMBLE_CHANNEL_ID, WORD_CHAIN_CHANNEL_ID}


def _clean_word(word: str | None) -> str:
    if not word:
        return ""
    cleaned = _WORD_RE.sub("", str(word)).strip()
    return " ".join(cleaned.split())[:40]


def _capitalize_sentence(text: str) -> str:
    text = " ".join(text.strip().split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def _example_sentence(word: str) -> str:
    return f"I learned the word '{word}' today, and I can use it in a simple sentence."


def _gentle_feedback(sentence: str, word: str) -> tuple[str, str]:
    cleaned = _capitalize_sentence(sentence)
    if not cleaned:
        return "Try one short sentence.", f"For example: {_example_sentence(word)}"

    lower = cleaned.lower()
    word_lower = word.lower()
    if word_lower not in lower:
        return (
            "Good start. Now make the target word clear.",
            f"Try: I used '{word}' because it helped me explain my idea.",
        )

    words = cleaned.split()
    if len(words) <= 4:
        return (
            "Nice and simple. You can make it stronger with one reason.",
            cleaned[:-1] + " because it matters to me.",
        )

    if " because " not in lower and not cleaned.endswith("?"):
        return (
            "Clear sentence. Add a reason if you want to practice more.",
            cleaned[:-1] + " because it connects to my life.",
        )

    return "Nice sentence. It is clear and easy to understand.", cleaned


class SentencePracticeModal(discord.ui.Modal, title="Make my sentence better"):
    sentence = discord.ui.TextInput(
        label="Your sentence",
        placeholder="Write one sentence with the word.",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=True,
    )

    def __init__(self, *, word: str) -> None:
        super().__init__()
        self._word = word

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note, improved = _gentle_feedback(str(self.sentence.value), self._word)
        embed = discord.Embed(
            title="Sentence practice",
            description=note,
        )
        embed.add_field(name="A polished version", value=improved, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SentencePracticeView(discord.ui.View):
    def __init__(self, *, word: str, owner_id: int | None = None) -> None:
        super().__init__(timeout=900)
        self._word = word
        self._owner_id = owner_id

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if self._owner_id is None or interaction.user.id == self._owner_id:
            return True
        await interaction.response.send_message(
            "This practice prompt belongs to the player who just finished the game.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Give me an example", style=discord.ButtonStyle.secondary)
    async def example(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_message(_example_sentence(self._word), ephemeral=True)

    @discord.ui.button(label="Make mine better", style=discord.ButtonStyle.primary)
    async def improve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(SentencePracticeModal(word=self._word))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_message("No problem. The game still counts.", ephemeral=True)


async def send_sentence_practice_prompt(
    channel: discord.abc.Messageable,
    *,
    user_id: int,
    word: str | None,
    game_label: str,
) -> None:
    cleaned = _clean_word(word)
    if not cleaned:
        return

    embed = discord.Embed(
        title="Use it in a sentence",
        description=(
            f"Nice work, <@{user_id}>. Want to turn this {game_label} round into real English?\n\n"
            f"Write one sentence with **{cleaned}**."
        ),
    )
    embed.add_field(name="Starter", value=f"I learned the word '{cleaned}' because ...", inline=False)
    embed.set_footer(text="Optional practice. One sentence is enough.")
    await channel.send(embed=embed, view=SentencePracticeView(word=cleaned, owner_id=user_id))


def _load_state(session: Any | None) -> dict[str, Any] | None:
    if not session:
        return None
    try:
        return json.loads(session.state_json)
    except Exception:
        logger.exception("SentencePractice: could not load game state")
        return None


def _player_finished(state: dict[str, Any] | None, user_id: int) -> bool:
    if not state:
        return False
    players = state.get("players", {}) or {}
    player = players.get(str(user_id), {}) or {}
    return bool(player.get("finished"))


def _answer_for(game_key: str, state: dict[str, Any] | None, user_id: int) -> str | None:
    if not state:
        return None
    if game_key == "wordle":
        return str(state.get("answer") or "")
    if game_key == "unscramble":
        players = state.get("players", {}) or {}
        player = players.get(str(user_id), {}) or {}
        return str(player.get("answer") or "")
    if game_key == "word_chain":
        return str(state.get("last_word") or "")
    return None


async def capture_sentence_practice_state(services: dict[str, Any], message: discord.Message) -> dict[str, dict[str, Any]]:
    repo = services.get("games_repo")
    channel_id = getattr(message.channel, "id", None)
    if not repo or channel_id not in _ALLOWED_CHANNEL_IDS:
        return {}

    snapshot: dict[str, dict[str, Any]] = {}
    for game_key in _TRACKED_GAME_KEYS:
        try:
            session = await repo.get_active_session(
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=game_key,
            )
        except Exception:
            logger.exception("SentencePractice: could not fetch %s session", game_key)
            continue

        state = _load_state(session)
        snapshot[game_key] = {
            "active": bool(session),
            "finished": _player_finished(state, message.author.id),
            "word": _answer_for(game_key, state, message.author.id),
        }
    return snapshot


async def maybe_send_sentence_practice_after_game(
    services: dict[str, Any],
    message: discord.Message,
    before: dict[str, dict[str, Any]],
) -> None:
    after = await capture_sentence_practice_state(services, message)
    labels = {
        "wordle": "Wordle",
        "unscramble": "Unscramble",
        "word_chain": "Word Chain",
    }

    for game_key in _TRACKED_GAME_KEYS:
        before_row = before.get(game_key, {})
        after_row = after.get(game_key, {})

        just_finished = bool(after_row.get("finished")) and not bool(before_row.get("finished"))
        word_chain_ended = (
            game_key == "word_chain"
            and bool(before_row.get("active"))
            and not bool(after_row.get("active"))
        )
        if not just_finished and not word_chain_ended:
            continue

        word = after_row.get("word") or before_row.get("word")
        if not word:
            continue

        try:
            await send_sentence_practice_prompt(
                message.channel,
                user_id=message.author.id,
                word=str(word),
                game_label=labels[game_key],
            )
        except Exception:
            logger.exception("SentencePractice: failed to send prompt for %s", game_key)
        return
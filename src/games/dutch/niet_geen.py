from __future__ import annotations

import asyncio
import json
import random
import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.economy_service import EconomyService
from src.services.rewards_service import RewardsService
from src.db.repo.economy_repo import GUILD_NL

log = logging.getLogger("games.niet_geen")

CHANNEL_ID = 1487175077702275273
ROUNDS_PER_GAME = 7
BEANS_CORRECT = 3
BEANS_PERFECT = 10   # bonus for perfect score on top of per-round beans

# ---- Question bank ----
# Each entry: (sentence_with_blank, answer, explanation)
# _____ is always the blank. Answer is 'niet' or 'geen'.

QUESTIONS: list[tuple[str, str, str]] = [
    (
        "Ik heb _____ auto.",
        "geen",
        "Test: kun je 'een auto' zeggen? Ja. Gebruik dan **geen**.\n"
        "Ik heb **geen** auto."
    ),
    (
        "Het is _____ leuk.",
        "niet",
        "Test: kun je 'een leuk' zeggen? Nee, 'leuk' is een bijvoeglijk naamwoord. Gebruik dan **niet**.\n"
        "Het is **niet** leuk."
    ),
    (
        "We hebben _____ geld.",
        "geen",
        "Test: kun je 'een geld' zeggen? Ja, geld is een onbepaald zelfstandig naamwoord. Gebruik dan **geen**.\n"
        "We hebben **geen** geld."
    ),
    (
        "Ik ben _____ moe.",
        "niet",
        "Test: kun je 'een moe' zeggen? Nee, 'moe' is een bijvoeglijk naamwoord. Gebruik dan **niet**.\n"
        "Ik ben **niet** moe."
    ),
    (
        "Er is _____ probleem.",
        "geen",
        "Test: kun je 'een probleem' zeggen? Ja. Gebruik dan **geen**.\n"
        "Er is **geen** probleem."
    ),
    (
        "Ik kom _____ vandaag.",
        "niet",
        "Test: 'vandaag' is geen zelfstandig naamwoord, het is een bijwoord. Gebruik dan **niet**.\n"
        "Ik kom **niet** vandaag."
    ),
    (
        "Zij heeft _____ hond.",
        "geen",
        "Test: kun je 'een hond' zeggen? Ja. Gebruik dan **geen**.\n"
        "Zij heeft **geen** hond."
    ),
    (
        "Ik zie de auto _____.",
        "niet",
        "Test: 'de auto' is bepaald (met 'de'). Bij bepaalde lidwoorden gebruik je **niet**, en het staat aan het einde van de zin.\n"
        "Ik zie de auto **niet**."
    ),
    (
        "Hij heeft _____ tijd.",
        "geen",
        "Test: kun je 'een tijd' zeggen? Ja. Gebruik dan **geen**.\n"
        "Hij heeft **geen** tijd."
    ),
    (
        "Dit is _____ goed idee.",
        "geen",
        "Test: kun je 'een goed idee' zeggen? Ja. Gebruik dan **geen**.\n"
        "Dit is **geen** goed idee."
    ),
    (
        "Ik werk _____ morgen.",
        "niet",
        "Test: 'morgen' is een bijwoord, geen zelfstandig naamwoord. Gebruik dan **niet**.\n"
        "Ik werk **niet** morgen."
    ),
    (
        "We hebben _____ brood meer.",
        "geen",
        "Test: kun je 'een brood' zeggen? Ja. Gebruik dan **geen**.\n"
        "We hebben **geen** brood meer."
    ),
    (
        "Ik vind het _____ moeilijk.",
        "niet",
        "Test: kun je 'een moeilijk' zeggen? Nee, 'moeilijk' is een bijvoeglijk naamwoord. Gebruik dan **niet**.\n"
        "Ik vind het **niet** moeilijk."
    ),
    (
        "Er zijn _____ stoelen.",
        "geen",
        "Test: kun je 'een stoel' zeggen? Ja (enkelvoud). Bij meervoud zonder lidwoord gebruik je ook **geen**.\n"
        "Er zijn **geen** stoelen."
    ),
    (
        "Ze spreekt _____ Nederlands.",
        "geen",
        "Test: kun je 'een Nederlands' zeggen? Taal als onbepaald zelfstandig naamwoord krijgt **geen**.\n"
        "Ze spreekt **geen** Nederlands."
    ),
    (
        "Ik snap het _____.",
        "niet",
        "Test: 'het' is bepaald. Bij bepaalde lidwoorden gebruik je **niet**.\n"
        "Ik snap het **niet**."
    ),
    (
        "Dit is _____ probleem.",
        "geen",
        "Test: kun je 'een probleem' zeggen? Ja. Gebruik dan **geen**.\n"
        "Dit is **geen** probleem."
    ),
    (
        "Hij is _____ blij.",
        "niet",
        "Test: kun je 'een blij' zeggen? Nee, 'blij' is een bijvoeglijk naamwoord. Gebruik dan **niet**.\n"
        "Hij is **niet** blij."
    ),
    (
        "Ik heb _____ zin.",
        "geen",
        "Test: kun je 'een zin' zeggen? Ja. Gebruik dan **geen**.\n"
        "Ik heb **geen** zin."
    ),
    (
        "Ze komt _____ naar de les.",
        "niet",
        "Test: er is geen zelfstandig naamwoord met 'een' — 'naar de les' is een vaste uitdrukking. Gebruik dan **niet**.\n"
        "Ze komt **niet** naar de les."
    ),
]


@dataclass
class _GameState:
    question_indices: list[int]
    current_round: int = 0        # 0-based index into question_indices
    scores: dict[str, int] = field(default_factory=dict)   # user_id -> correct count
    names: dict[str, str] = field(default_factory=dict)    # user_id -> display_name
    answered_round: set[str] = field(default_factory=set)  # user_ids who answered this round
    round_answers: dict[str, str] = field(default_factory=dict)  # uid -> 'niet'/'geen' for current round
    finished: bool = False

    def current_q(self) -> tuple[str, str, str]:
        return QUESTIONS[self.question_indices[self.current_round]]

    def total_rounds(self) -> int:
        return len(self.question_indices)


def _make_question_embed(state: _GameState) -> discord.Embed:
    sentence, _, _ = state.current_q()
    round_num = state.current_round + 1
    total = state.total_rounds()
    embed = discord.Embed(
        title=f"Niet of Geen? — Ronde {round_num}/{total}",
        description=(
            f"**{sentence}**\n\n"
            "Typ **niet** of **geen** in de chat."
        ),
    )
    embed.set_footer(text="Iedereen kan meedoen. Typ je antwoord als gewoon bericht.")
    return embed


def _make_result_embed(
    state: _GameState,
    answer: str,
    explanation: str,
    correct_users: list[str],
    wrong_users: list[str],
) -> discord.Embed:
    sentence, correct, _ = state.current_q()
    filled = sentence.replace("_____", f"**{correct}**")

    correct_line = ", ".join(correct_users) if correct_users else "Niemand"
    wrong_line = ", ".join(wrong_users) if wrong_users else "Niemand"

    embed = discord.Embed(
        title=f"Antwoord: **{correct.upper()}**",
        description=(
            f"{filled}\n\n"
            f"{explanation}\n\n"
            f"Goed: {correct_line}\n"
            f"Fout: {wrong_line}"
        ),
        color=discord.Color.green(),
    )
    return embed


def _make_final_embed(state: _GameState) -> discord.Embed:
    total = state.total_rounds()
    if not state.scores:
        return discord.Embed(
            title="Spel voorbij",
            description="Niemand heeft meegedaan. Typ /nietgeen om opnieuw te beginnen.",
        )

    lines = []
    for uid, score in sorted(state.scores.items(), key=lambda x: -x[1]):
        name = state.names.get(uid, uid)
        pct = int(score / total * 100)
        medal = "🥇" if score == total else "🥈" if score >= total * 0.7 else "🥉" if score >= total * 0.5 else ""
        lines.append(f"{medal} **{name}** — {score}/{total} ({pct}%)")

    embed = discord.Embed(
        title="Eindstand — Niet vs Geen",
        description="\n".join(lines) + "\n\nTyp **/nietgeen** om opnieuw te spelen.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="De sneltest: kan ik 'een' zeggen? Ja = geen. Nee = niet.")
    return embed


class NietGeenGame:
    """
    Niet vs Geen — Dutch grammar game for the game bot.
    Hooks into the existing game_registry via handle_discord_message.
    Slash command /nietgeen starts a new game.
    """

    key = "niet_geen"
    allowed_channel_ids: set[int] = {CHANNEL_ID}

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        allowed_channel_ids: set[int] | None = None,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._economy = economy
        self._rewards = rewards
        if allowed_channel_ids is not None:
            self.allowed_channel_ids = set(allowed_channel_ids)

        # In-memory state: channel_id -> _GameState
        self._active: dict[int, _GameState] = {}
        # Pending answer collection task per channel
        self._collect_tasks: dict[int, asyncio.Task] = {}

    # ---- Public: start a game ----

    async def start_game(self, channel: discord.TextChannel) -> None:
        if channel.id in self._active and not self._active[channel.id].finished:
            await channel.send("Er loopt al een spel. Wacht tot het klaar is of typ **/stopnietgeen**.")
            return

        indices = random.sample(range(len(QUESTIONS)), k=ROUNDS_PER_GAME)
        state = _GameState(question_indices=indices)
        self._active[channel.id] = state

        await channel.send(
            "**Niet vs Geen — het spel**\n\n"
            f"{ROUNDS_PER_GAME} rondes. Typ **niet** of **geen** als je het antwoord weet.\n"
            "Iedereen kan meedoen. De sneltest: *kan ik 'een' zeggen? Ja = geen. Nee = niet.*\n\n"
            "Eerste vraag:"
        )
        await self._post_question(channel, state)

    async def stop_game(self, channel: discord.TextChannel) -> None:
        state = self._active.pop(channel.id, None)
        task = self._collect_tasks.pop(channel.id, None)
        if task:
            task.cancel()
        if state:
            await channel.send("Spel gestopt.")
        else:
            await channel.send("Er is geen actief spel.")

    # ---- Internal: game flow ----

    async def _post_question(self, channel: discord.TextChannel, state: _GameState) -> None:
        state.answered_round.clear()
        state.round_answers.clear()
        embed = _make_question_embed(state)
        await channel.send(embed=embed)
        # Collect answers for 20 seconds
        task = asyncio.create_task(self._collect_answers(channel, state))
        self._collect_tasks[channel.id] = task

    async def _collect_answers(self, channel: discord.TextChannel, state: _GameState) -> None:
        await asyncio.sleep(20)
        await self._reveal_and_advance(channel, state)

    async def _reveal_and_advance(self, channel: discord.TextChannel, state: _GameState) -> None:
        self._collect_tasks.pop(channel.id, None)

        _, correct, explanation = state.current_q()
        correct_users = []
        wrong_users = []

        for uid, score in state.scores.items():
            pass

        round_answers = state.round_answers
        for uid, given in round_answers.items():
            name = state.names.get(uid, uid)
            if given == correct:
                correct_users.append(name)
            else:
                wrong_users.append(name)

        embed = _make_result_embed(state, correct, explanation, correct_users, wrong_users)
        await channel.send(embed=embed)

        # Award beans
        for uid, given in state.round_answers.items():
            if given == correct:
                try:
                    uid_int = int(uid)
                    await self._economy.award_beans_discord(
                        user_id=uid_int,
                        amount=BEANS_CORRECT,
                        reason="Niet vs Geen correct",
                        game_key=self.key,
                        display_name=state.names.get(uid),
                        guild_id=GUILD_NL,
                    )
                except Exception:
                    log.exception("NietGeen: failed to award beans uid=%s", uid)

        # Advance
        state.current_round += 1
        if state.current_round >= state.total_rounds():
            await self._finish_game(channel, state)
        else:
            await asyncio.sleep(3)
            await self._post_question(channel, state)

    async def _finish_game(self, channel: discord.TextChannel, state: _GameState) -> None:
        state.finished = True
        # Bonus beans for perfect score
        total = state.total_rounds()
        for uid, score in state.scores.items():
            if score == total:
                try:
                    uid_int = int(uid)
                    await self._economy.award_beans_discord(
                        user_id=uid_int,
                        amount=BEANS_PERFECT,
                        reason="Niet vs Geen perfecte score bonus",
                        game_key=self.key,
                        display_name=state.names.get(uid),
                        guild_id=GUILD_NL,
                    )
                except Exception:
                    log.exception("NietGeen: failed to award perfect bonus uid=%s", uid)

        embed = _make_final_embed(state)
        await channel.send(embed=embed)
        self._active.pop(channel.id, None)

    # ---- Message handler ----

    async def handle_discord_message(self, message: discord.Message) -> bool:
        if message.channel.id not in self.allowed_channel_ids:
            return False

        state = self._active.get(message.channel.id)
        if not state or state.finished:
            return False

        text = message.content.strip().lower()
        if text not in ("niet", "geen"):
            return False

        uid = str(message.author.id)

        # Only first answer per round counts
        if uid in state.answered_round:
            return True  # consume but ignore

        state.answered_round.add(uid)
        state.names[uid] = message.author.display_name

        _, correct, _ = state.current_q()
        is_correct = text == correct

        state.round_answers[uid] = text

        # Track score
        if uid not in state.scores:
            state.scores[uid] = 0
        if is_correct:
            state.scores[uid] += 1

        # React to acknowledge
        try:
            await message.add_reaction("✅" if is_correct else "❌")
        except Exception:
            pass

        return True

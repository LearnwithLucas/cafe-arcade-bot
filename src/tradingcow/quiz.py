"""OSRS quiz for TradingCow: /osrsquiz starts a round of multiple-choice questions.

Each question has four answer buttons and a 20 second countdown. Everyone in the channel can play.
One answer per person per question. A right answer scores 10 points plus 1 per second left, so faster is better.
The answer, who got it right and the scores show after each question; the final standings after the last one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

from .quiz_questions import QUESTIONS

log = logging.getLogger("tradingcow.quiz")

QUIZ_CHANNEL_ID = int(os.getenv("QUIZ_CHANNEL_ID", "1465420490758688769"))
SECONDS = 20
PAUSE = 6  # seconds between the answer reveal and the next question
COLOR = 0xC8A24A
LETTERS = "ABCD"
MEDALS = ["\U0001F947", "\U0001F948", "\U0001F949"]


@dataclass
class Game:
    channel_id: int
    host_id: int
    questions: list[tuple[str, str, list[str]]]
    scores: dict[int, int] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    right: dict[int, int] = field(default_factory=dict)
    answers: dict[int, tuple[int, float]] = field(default_factory=dict)  # user -> (option index, time answered)
    options: list[str] = field(default_factory=list)
    correct: int = 0
    asked_at: float = 0.0
    open: bool = False
    stopped: bool = False


GAMES: dict[int, Game] = {}
TASKS: set[asyncio.Task] = set()  # keep a reference so running games are never garbage collected


class Poster:
    """Posts in the channel; if the bot isn't allowed to (missing Send Messages / Embed Links),
    it falls back to follow-ups on the /osrsquiz interaction, which need no channel permissions."""

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.channel = interaction.channel
        self.use_followup = self.channel is None

    async def send(self, delete_after: float | None = None, **kwargs):
        if not self.use_followup:
            try:
                return await self.channel.send(delete_after=delete_after, **kwargs)
            except discord.Forbidden:
                log.warning("Quiz: no permission to post in channel %s, using interaction follow-ups",
                            getattr(self.channel, "id", "?"))
                self.use_followup = True
        msg = await self.interaction.followup.send(wait=True, **kwargs)
        if delete_after:
            await msg.delete(delay=delete_after)
        return msg


def scoreboard(game: Game, limit: int = 10) -> str:
    ranked = sorted(game.scores.items(), key=lambda kv: (-kv[1], game.names.get(kv[0], "").lower()))
    lines = []
    for i, (uid, pts) in enumerate(ranked[:limit]):
        mark = MEDALS[i] if i < 3 else f"`{i + 1:>2}.`"
        lines.append(f"{mark} <@{uid}>: **{pts}** points ({game.right.get(uid, 0)} right)")
    return "\n".join(lines) or "Nobody scored yet."


class AnswerView(discord.ui.View):
    def __init__(self, game: Game, number: int) -> None:
        super().__init__(timeout=SECONDS + 5)
        self.game = game
        for i, text in enumerate(game.options):
            btn = discord.ui.Button(label=f"{LETTERS[i]}. {text}"[:80], style=discord.ButtonStyle.secondary,
                                    custom_id=f"quiz:{game.channel_id}:{number}:{i}", row=i // 2)
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            game = self.game
            if not game.open or game.stopped:
                await interaction.response.send_message("Too late, time is up for this question.", ephemeral=True)
                return
            if interaction.user.id in game.answers:
                prev = game.answers[interaction.user.id][0]
                await interaction.response.send_message(f"You already answered {LETTERS[prev]}.", ephemeral=True)
                return
            game.answers[interaction.user.id] = (index, time.monotonic())
            game.names[interaction.user.id] = interaction.user.display_name
            await interaction.response.send_message(f"Locked in: **{LETTERS[index]}. {game.options[index]}**", ephemeral=True)
        return callback

    def reveal(self) -> None:
        for i, item in enumerate(self.children):
            item.disabled = True
            if i == self.game.correct:
                item.style = discord.ButtonStyle.success


async def run_game(channel: "Poster", game: Game, delay: int = 0) -> None:
    total = len(game.questions)
    try:
        await asyncio.sleep(delay)
        for n, (question, answer, wrong) in enumerate(game.questions, start=1):
            if game.stopped:
                break
            options = [answer, *wrong]
            random.shuffle(options)
            game.options, game.correct, game.answers = options, options.index(answer), {}
            ends = int(time.time()) + SECONDS
            embed = discord.Embed(title=f"Question {n} of {total}", description=f"**{question}**", color=COLOR)
            embed.add_field(name="Time left", value=f"<t:{ends}:R>")
            embed.set_footer(text="Faster right answers score more points. One answer each.")
            view = AnswerView(game, n)
            game.asked_at, game.open = time.monotonic(), True  # open before sending, so a lightning-fast click counts
            msg = await channel.send(embed=embed, view=view)
            await asyncio.sleep(SECONDS)
            game.open = False

            winners = []
            for uid, (idx, at) in sorted(game.answers.items(), key=lambda kv: kv[1][1]):
                game.scores.setdefault(uid, 0)
                if idx == game.correct:
                    left = max(0, SECONDS - int(at - game.asked_at))
                    game.scores[uid] += 10 + left
                    game.right[uid] = game.right.get(uid, 0) + 1
                    winners.append(f"<@{uid}> (+{10 + left})")
            view.reveal()
            embed.clear_fields()
            embed.add_field(name="Answer", value=f"**{LETTERS[game.correct]}. {answer}**", inline=False)
            embed.add_field(name=f"Right ({len(winners)} of {len(game.answers)})",
                            value=", ".join(winners)[:1024] or "Nobody got it.", inline=False)
            await msg.edit(embed=embed, view=view)
            view.stop()
            if n < total and not game.stopped:
                await channel.send(embed=discord.Embed(
                    title="Scores", description=scoreboard(game, 5) + f"\n\nNext question in {PAUSE} seconds...", color=COLOR),
                    delete_after=PAUSE + 2)
                await asyncio.sleep(PAUSE)
        title = "Quiz stopped" if game.stopped else "Quiz finished!"
        final = discord.Embed(title=title, description=scoreboard(game, 15), color=COLOR)
        ranked = sorted(game.scores.items(), key=lambda kv: -kv[1])
        if ranked and ranked[0][1] > 0 and not game.stopped:
            final.add_field(name="Winner", value=f"\U0001F451 <@{ranked[0][0]}> with {ranked[0][1]} points")
        final.set_footer(text="Play again with /osrsquiz")
        await channel.send(embed=final, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("OSRS quiz crashed")
    finally:
        GAMES.pop(game.channel_id, None)


@app_commands.command(name="osrsquiz", description="Start an OSRS quiz: multiple choice, 20 seconds per question")
@app_commands.describe(questions="How many questions (default 10)", stop="Stop the quiz that is running")
async def osrsquiz_cmd(interaction: discord.Interaction, questions: app_commands.Range[int, 1, 30] = 10,
                       stop: bool = False) -> None:
    if interaction.channel_id != QUIZ_CHANNEL_ID:
        await interaction.response.send_message(f"The quiz runs in <#{QUIZ_CHANNEL_ID}>.", ephemeral=True)
        return
    running = GAMES.get(interaction.channel_id)
    if stop:
        if not running:
            await interaction.response.send_message("No quiz is running.", ephemeral=True)
            return
        from .bot import can_configure  # late import, bot.py imports this module
        if interaction.user.id != running.host_id and not can_configure(interaction):
            await interaction.response.send_message("Only the person who started the quiz can stop it.", ephemeral=True)
            return
        running.stopped = True
        await interaction.response.send_message("Stopping the quiz after this question.")
        return
    if running:
        await interaction.response.send_message("A quiz is already running here. Jump in!", ephemeral=True)
        return
    game = Game(interaction.channel_id, interaction.user.id, random.sample(QUESTIONS, min(questions, len(QUESTIONS))))
    GAMES[interaction.channel_id] = game
    await interaction.response.send_message(embed=discord.Embed(
        title="⚔️ OSRS Quiz",
        description=(f"{interaction.user.mention} started a quiz with **{len(game.questions)} questions**.\n"
                     f"You get {SECONDS} seconds per question. Press a button to answer, one answer each.\n"
                     f"Right answer: 10 points plus 1 point per second left.\n\nFirst question in 5 seconds..."),
        color=COLOR))
    task = asyncio.create_task(run_game(Poster(interaction), game, delay=5))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def setup_quiz(bot) -> None:
    bot.tree.add_command(osrsquiz_cmd)

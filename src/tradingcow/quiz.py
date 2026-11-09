"""OSRS quiz for TradingCow: /osrsquiz starts a round of multiple-choice questions, text only.

Each question shows four options (A to D) and a 20 second countdown. Players shout their answer in the chat.
When time is up the same message shows the right answer, then the next question follows 5 seconds later.

No buttons and no reading the chat, so it works even when the bot can't see or post in the channel:
it then posts everything as follow-ups to the /osrsquiz command (Discord allows those for 15 minutes).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass

import discord
from discord import app_commands

from .quiz_questions import QUESTIONS

log = logging.getLogger("tradingcow.quiz")

QUIZ_CHANNEL_ID = int(os.getenv("QUIZ_CHANNEL_ID", "1465420490758688769"))
SECONDS = 20
PAUSE = 5  # seconds between the answer reveal and the next question
COLOR = 0xC8A24A
LETTERS = "ABCD"


@dataclass
class Game:
    channel_id: int
    host_id: int
    questions: list[tuple[str, str, list[str]]]
    stopped: bool = False


GAMES: dict[int, Game] = {}
TASKS: set[asyncio.Task] = set()  # keep a reference so running games are never garbage collected


class Poster:
    """Posts in the channel; if the bot isn't allowed to (Missing Access, no Send Messages or Embed Links),
    it falls back to follow-ups on the /osrsquiz interaction, which need no channel permissions."""

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.channel = interaction.channel
        self.use_followup = self.channel is None

    async def send(self, **kwargs):
        if not self.use_followup:
            try:
                return await self.channel.send(**kwargs)
            except discord.Forbidden:
                log.warning("Quiz: no permission to post in channel %s, using interaction follow-ups",
                            getattr(self.channel, "id", "?"))
                self.use_followup = True
        return await self.interaction.followup.send(wait=True, **kwargs)


def question_embed(n: int, total: int, question: str, options: list[str]) -> discord.Embed:
    e = discord.Embed(title=f"Question {n} of {total}", description=f"**{question}**", color=COLOR)
    e.add_field(name="Options", value="\n".join(f"**{LETTERS[i]}.** {o}" for i, o in enumerate(options)), inline=False)
    return e


async def run_game(poster: Poster, game: Game, delay: int = 0) -> None:
    total = len(game.questions)
    try:
        await asyncio.sleep(delay)
        for n, (question, answer, wrong) in enumerate(game.questions, start=1):
            if game.stopped:
                break
            options = [answer, *wrong]
            random.shuffle(options)
            correct = options.index(answer)
            embed = question_embed(n, total, question, options)
            embed.add_field(name="Time left", value=f"<t:{int(time.time()) + SECONDS}:R>", inline=False)
            embed.set_footer(text="Type A, B, C or D in the chat!")
            msg = await poster.send(embed=embed)
            await asyncio.sleep(SECONDS)

            done = question_embed(n, total, question, options)
            done.add_field(name="✅ Answer", value=f"**{LETTERS[correct]}. {answer}**", inline=False)
            done.set_footer(text="Next question in 5 seconds..." if n < total and not game.stopped else "That was the last one!")
            await msg.edit(embed=done)
            if n < total and not game.stopped:
                await asyncio.sleep(PAUSE)
        end = discord.Embed(title="Quiz stopped" if game.stopped else "Quiz finished!", color=COLOR,
                            description="Thanks for playing! Start a new round with /osrsquiz.")
        await poster.send(embed=end)
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
                     f"Each question has 4 options and {SECONDS} seconds on the clock. Type your answer (A, B, C or D) "
                     f"in the chat. Then the right answer shows, and the next question follows {PAUSE} seconds later.\n\n"
                     "First question in 5 seconds..."),
        color=COLOR))
    task = asyncio.create_task(run_game(Poster(interaction), game, delay=5))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def setup_quiz(bot) -> None:
    bot.tree.add_command(osrsquiz_cmd)

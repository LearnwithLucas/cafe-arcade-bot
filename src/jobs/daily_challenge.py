from __future__ import annotations

# src/jobs/daily_challenge.py
import asyncio
import datetime as dt
import logging
import random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import tasks

log = logging.getLogger("jobs.daily_challenge")

TZ_NAME = "Europe/Amsterdam"
POST_HOUR = 10
POST_MINUTE = 0

EN_CHALLENGE_CHANNEL_ID = 1488073429956427866  # 🏆┃daily-challenge
NL_CHALLENGE_CHANNEL_ID = 1488073315238281368  # 🏆┃dagelijkse-challenge

KV_EN_CHALLENGE_DATE = "daily_challenge_en_last_date"
KV_NL_CHALLENGE_DATE = "daily_challenge_nl_last_date"

BEANS_CHALLENGE = 15   # beans awarded for completing the daily challenge


# =======================================================
# CHALLENGE POOLS
# Each entry: (title, description, prompt)
# =======================================================

EN_CHALLENGES: list[tuple[str, str, str]] = [
    (
        "Word of the Moment",
        "Use today's word of the day in a sentence — but make it personal. "
        "Something that actually happened to you or something you genuinely think.",
        "Reply with your sentence below. Keep it real.",
    ),
    (
        "One Sentence Story",
        "Tell a complete story in exactly one sentence. "
        "It must have a beginning, a middle and an end.",
        "Reply with your one-sentence story.",
    ),
    (
        "Describe Your Day Without Using 'Good' or 'Fine'",
        "How was your day? You cannot use the words 'good' or 'fine'. "
        "Find other words to describe it.",
        "Reply with your description.",
    ),
    (
        "Explain It Simply",
        "Pick something complicated — a job, a concept, a hobby — and explain it "
        "as if the other person has never heard of it. Use simple English only.",
        "Reply with your explanation.",
    ),
    (
        "Three Things",
        "Name three things you can see right now and describe each one in one sentence. "
        "No single-word answers.",
        "Reply with your three descriptions.",
    ),
    (
        "Disagree Politely",
        "Think of a common piece of advice you disagree with. "
        "Explain why — politely and clearly.",
        "Reply with the advice and your disagreement.",
    ),
    (
        "The Awkward Question",
        "Ask a question you have always been curious about but never asked. "
        "Something about daily life, language, culture — anything.",
        "Post your question and see who answers.",
    ),
    (
        "Replace a Cliche",
        "Take a common English phrase or cliche and rewrite it in your own words. "
        "Same meaning, no cliche.",
        "Reply with the original and your version.",
    ),
    (
        "First and Last",
        "Describe your morning in one sentence and your evening in one sentence. "
        "Use different vocabulary for each.",
        "Reply with both sentences.",
    ),
    (
        "Why This Word",
        "Pick an English word you learned recently and explain why it stuck with you. "
        "What made it memorable?",
        "Reply with the word and your reason.",
    ),
    (
        "The Honest Opinion",
        "Give your honest opinion about something small — a food, a habit, a trend. "
        "One clear sentence. No 'I think it depends'.",
        "Reply with your opinion.",
    ),
    (
        "Teach Something",
        "Share one thing you know well that most people around you do not. "
        "Explain it clearly in 2-3 sentences.",
        "Reply with your explanation.",
    ),
    (
        "Rewrite the Mistake",
        "Write a sentence with a grammar or vocabulary mistake on purpose. "
        "Then write the corrected version below it.",
        "Reply with both versions — wrong first, then correct.",
    ),
    (
        "The Comparison",
        "Compare two things that seem unrelated. "
        "Find one real similarity between them.",
        "Reply with your comparison.",
    ),
    (
        "30 Words Maximum",
        "Describe what you want to achieve in the next month. "
        "30 words maximum. Make every word count.",
        "Reply with your description. Word count counts.",
    ),
]

NL_CHALLENGES: list[tuple[str, str, str]] = [
    (
        "Woord van het Moment",
        "Gebruik het woord van de dag van vandaag in een zin — maar maak het persoonlijk. "
        "Iets wat echt is gebeurd of iets wat je echt denkt.",
        "Reageer met je zin hieronder. Maak het echt.",
    ),
    (
        "Een Zinsverhaal",
        "Vertel een compleet verhaal in precies één zin. "
        "Het moet een begin, een midden en een einde hebben.",
        "Reageer met je zin.",
    ),
    (
        "Beschrijf Je Dag Zonder 'Goed' of 'Prima'",
        "Hoe was je dag? Je mag de woorden 'goed' of 'prima' niet gebruiken. "
        "Zoek andere woorden om het te beschrijven.",
        "Reageer met je beschrijving.",
    ),
    (
        "Leg Het Simpel Uit",
        "Kies iets ingewikkelds — een beroep, een concept, een hobby — en leg het uit "
        "alsof de ander er nog nooit van heeft gehoord. Gebruik alleen eenvoudig Nederlands.",
        "Reageer met je uitleg.",
    ),
    (
        "Drie Dingen",
        "Noem drie dingen die je nu kunt zien en beschrijf elk in één zin. "
        "Geen antwoorden van één woord.",
        "Reageer met je drie beschrijvingen.",
    ),
    (
        "Beleefd Oneens",
        "Denk aan een veelgehoord advies waarmee je het niet eens bent. "
        "Leg uit waarom — beleefd en duidelijk.",
        "Reageer met het advies en je argument.",
    ),
    (
        "De Ongemakkelijke Vraag",
        "Stel een vraag die je altijd nieuwsgierig heeft gemaakt maar nooit hebt gesteld. "
        "Over het dagelijks leven, taal, cultuur — wat dan ook.",
        "Post je vraag en kijk wie antwoord geeft.",
    ),
    (
        "Vervang het Cliché",
        "Neem een veelgebruikte Nederlandse uitdrukking of cliché en herschrijf het in je eigen woorden. "
        "Zelfde betekenis, geen cliché.",
        "Reageer met het origineel en jouw versie.",
    ),
    (
        "Ochtend en Avond",
        "Beschrijf je ochtend in één zin en je avond in één zin. "
        "Gebruik voor elk andere woorden.",
        "Reageer met beide zinnen.",
    ),
    (
        "Waarom Dit Woord",
        "Kies een Nederlands woord dat je onlangs hebt geleerd en leg uit waarom het je bijgebleven is. "
        "Wat maakte het gedenkwaardig?",
        "Reageer met het woord en je reden.",
    ),
    (
        "De Eerlijke Mening",
        "Geef je eerlijke mening over iets kleins — een gerecht, een gewoonte, een trend. "
        "Één duidelijke zin. Geen 'ik denk dat het ervan afhangt'.",
        "Reageer met je mening.",
    ),
    (
        "Leer Iets",
        "Deel iets wat jij goed weet maar de meeste mensen om je heen niet. "
        "Leg het duidelijk uit in 2-3 zinnen.",
        "Reageer met je uitleg.",
    ),
    (
        "Herschrijf de Fout",
        "Schrijf een zin met een grammatica- of woordenschatfout — met opzet. "
        "Schrijf daarna de gecorrigeerde versie eronder.",
        "Reageer met beide versies — eerst fout, dan goed.",
    ),
    (
        "De Vergelijking",
        "Vergelijk twee dingen die niets met elkaar te maken lijken te hebben. "
        "Vind één echte overeenkomst.",
        "Reageer met je vergelijking.",
    ),
    (
        "Maximaal 30 Woorden",
        "Beschrijf wat je de komende maand wilt bereiken. "
        "Maximaal 30 woorden. Zorg dat elk woord telt.",
        "Reageer met je beschrijving. Het aantal woorden telt.",
    ),
]


def _get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _pick(pool: list, date_str: str) -> tuple[str, str, str]:
    idx = hash(date_str) % len(pool)
    return pool[idx]


def _build_en_embed(title: str, description: str, prompt: str, date_str: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Daily Challenge — {title}",
        description=(
            f"{description}\n\n"
            f"**{prompt}**\n\n"
            f"Complete the challenge and earn **{BEANS_CHALLENGE} beans**.\n"
            "Use `/cafe daily` to claim your daily beans too."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"{date_str} | One challenge per day")
    return embed


def _build_nl_embed(title: str, description: str, prompt: str, date_str: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Dagelijkse Uitdaging — {title}",
        description=(
            f"{description}\n\n"
            f"**{prompt}**\n\n"
            f"Voltooi de uitdaging en verdien **{BEANS_CHALLENGE} bonen**.\n"
            "Gebruik ook `/cafe dagelijks` voor je dagelijkse bonen."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"{date_str} | Eén uitdaging per dag")
    return embed


class DailyChallengeJob:
    def __init__(
        self,
        *,
        bot: discord.Client,
        repo,
        en_guild_id: int,
        nl_guild_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._repo = repo
        self._en_guild_id = en_guild_id
        self._nl_guild_id = nl_guild_id
        self._tz = _get_tz()
        self._tick.start()

    def _now(self) -> dt.datetime:
        return dt.datetime.now(tz=self._tz)

    async def _get_channel(self, channel_id: int) -> discord.TextChannel | None:
        ch = self._bot.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await self._bot.fetch_channel(channel_id)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except Exception:
            pass
        log.warning("DailyChallenge: could not fetch channel %s", channel_id)
        return None

    @tasks.loop(minutes=1)
    async def _tick(self) -> None:
        now = self._now()
        if not (now.hour == POST_HOUR and now.minute == POST_MINUTE):
            return

        date_str = now.date().isoformat()

        # English
        last_en = await self._repo.kv_get(self._en_guild_id, KV_EN_CHALLENGE_DATE)
        if last_en != date_str:
            await self._post_english(date_str)
            await self._repo.kv_set(self._en_guild_id, KV_EN_CHALLENGE_DATE, date_str)

        # Dutch
        if self._nl_guild_id:
            last_nl = await self._repo.kv_get(self._nl_guild_id, KV_NL_CHALLENGE_DATE)
            if last_nl != date_str:
                await self._post_dutch(date_str)
                await self._repo.kv_set(self._nl_guild_id, KV_NL_CHALLENGE_DATE, date_str)

    @_tick.before_loop
    async def _before(self) -> None:
        await self._bot.wait_until_ready()

    async def _post_english(self, date_str: str) -> None:
        ch = await self._get_channel(EN_CHALLENGE_CHANNEL_ID)
        if not ch:
            return
        title, description, prompt = _pick(EN_CHALLENGES, date_str)
        embed = _build_en_embed(title, description, prompt, date_str)
        try:
            await ch.send(embed=embed)
            log.info("DailyChallenge: posted EN challenge for %s", date_str)
        except Exception:
            log.exception("DailyChallenge: failed to post EN challenge")

    async def _post_dutch(self, date_str: str) -> None:
        ch = await self._get_channel(NL_CHALLENGE_CHANNEL_ID)
        if not ch:
            return
        title, description, prompt = _pick(NL_CHALLENGES, date_str)
        embed = _build_nl_embed(title, description, prompt, date_str)
        try:
            await ch.send(embed=embed)
            log.info("DailyChallenge: posted NL challenge for %s", date_str)
        except Exception:
            log.exception("DailyChallenge: failed to post NL challenge")

    async def post_en_now(self) -> None:
        """Manual trigger — admin command."""
        date_str = self._now().date().isoformat()
        await self._post_english(date_str)

    async def post_nl_now(self) -> None:
        """Manual trigger — admin command."""
        date_str = self._now().date().isoformat()
        await self._post_dutch(date_str)

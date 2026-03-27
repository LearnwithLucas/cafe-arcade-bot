from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import discord

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.economy_service import EconomyService
from src.services.rewards_service import RewardsService
from src.db.repo.economy_repo import GUILD_NL

log = logging.getLogger("games.niet_geen")

CHANNEL_ID = 1487175077702275273
BEANS_CORRECT = 3
BEANS_STREAK_BONUS = 2    # extra per 3-in-a-row streak


QUESTIONS: list[tuple[str, str, str]] = [
    # --- GEEN: indefinite singular nouns ---
    ("Ik heb _____ auto.", "geen", "**geen** - 'een auto' kan. Geen vervangt het onbepaald lidwoord 'een'.\nIk heb **geen** auto."),
    ("Dit is _____ probleem.", "geen", "**geen** - 'een probleem' kan. Geen vervangt 'een'.\nDit is **geen** probleem."),
    ("Zij heeft _____ hond.", "geen", "**geen** - 'een hond' kan. Geen vervangt 'een'.\nZij heeft **geen** hond."),
    ("Hij heeft _____ baan.", "geen", "**geen** - 'een baan' kan. Geen vervangt 'een'.\nHij heeft **geen** baan."),
    ("We hebben _____ tafel.", "geen", "**geen** - 'een tafel' kan. Geen vervangt 'een'.\nWe hebben **geen** tafel."),
    ("Er is _____ bus vandaag.", "geen", "**geen** - 'een bus' kan. Geen vervangt 'een'.\nEr is **geen** bus vandaag."),
    ("Ik heb _____ pen bij me.", "geen", "**geen** - 'een pen' kan. Geen vervangt 'een'.\nIk heb **geen** pen bij me."),
    ("Dit is _____ goed idee.", "geen", "**geen** - 'een goed idee' kan. Het bijvoeglijk naamwoord hoort bij het zelfstandig naamwoord.\nDit is **geen** goed idee."),
    ("Hij is _____ leraar.", "geen", "**geen** - 'een leraar' kan. Beroepen zijn onbepaalde zelfstandige naamwoorden.\nHij is **geen** leraar."),
    ("Ze heeft _____ zus.", "geen", "**geen** - 'een zus' kan. Geen vervangt 'een'.\nZe heeft **geen** zus."),
    # --- GEEN: mass / uncountable nouns ---
    ("We hebben _____ geld.", "geen", "**geen** - geld is een stofnaam. Stofnamen zonder lidwoord krijgen geen.\nWe hebben **geen** geld."),
    ("Hij heeft _____ tijd.", "geen", "**geen** - 'een tijd' kan. Geen vervangt 'een'.\nHij heeft **geen** tijd."),
    ("Er is _____ water meer.", "geen", "**geen** - stofnamen zonder lidwoord krijgen geen.\nEr is **geen** water meer."),
    ("We hebben _____ brood.", "geen", "**geen** - stofnamen zonder lidwoord krijgen geen.\nWe hebben **geen** brood."),
    ("Ze heeft _____ geduld.", "geen", "**geen** - 'geduld' is een onbepaald zelfstandig naamwoord.\nZe heeft **geen** geduld."),
    # --- GEEN: plurals ---
    ("Er zijn _____ stoelen.", "geen", "**geen** - meervoud zonder lidwoord krijgt geen. Test: 'een stoel' kan in enkelvoud.\nEr zijn **geen** stoelen."),
    ("We hebben _____ boodschappen gedaan.", "geen", "**geen** - meervoud zonder lidwoord krijgt geen.\nWe hebben **geen** boodschappen gedaan."),
    ("Er zijn _____ kaartjes meer.", "geen", "**geen** - meervoud zonder lidwoord krijgt geen.\nEr zijn **geen** kaartjes meer."),
    ("Ze heeft _____ vrienden hier.", "geen", "**geen** - meervoud zonder lidwoord krijgt geen.\nZe heeft **geen** vrienden hier."),
    ("Er zijn _____ goede restaurants hier.", "geen", "**geen** - meervoud zonder lidwoord krijgt geen.\nEr zijn **geen** goede restaurants hier."),
    # --- GEEN: languages, abstract, experience ---
    ("Ze spreekt _____ Nederlands.", "geen", "**geen** - taalnames als onbepaald zelfstandig naamwoord krijgen geen.\nZe spreekt **geen** Nederlands."),
    ("Hij heeft _____ ervaring.", "geen", "**geen** - 'een ervaring' kan. Geen vervangt 'een'.\nHij heeft **geen** ervaring."),
    ("We hebben _____ informatie gekregen.", "geen", "**geen** - 'informatie' is een onbepaald zelfstandig naamwoord.\nWe hebben **geen** informatie gekregen."),
    ("Er is _____ bewijs.", "geen", "**geen** - 'een bewijs' kan. Geen vervangt 'een'.\nEr is **geen** bewijs."),
    ("Ze heeft _____ zin.", "geen", "**geen** - 'een zin' kan. Geen vervangt 'een'.\nZe heeft **geen** zin."),
    # --- NIET: adjectives ---
    ("Het is _____ leuk.", "niet", "**niet** - 'leuk' is een bijvoeglijk naamwoord. Test: 'een leuk' kan niet.\nHet is **niet** leuk."),
    ("Ik ben _____ moe.", "niet", "**niet** - 'moe' is een bijvoeglijk naamwoord. Test: 'een moe' kan niet.\nIk ben **niet** moe."),
    ("Hij is _____ blij.", "niet", "**niet** - 'blij' is een bijvoeglijk naamwoord. Test: 'een blij' kan niet.\nHij is **niet** blij."),
    ("Ze is _____ ziek.", "niet", "**niet** - 'ziek' is een bijvoeglijk naamwoord. Test: 'een ziek' kan niet.\nZe is **niet** ziek."),
    ("Het eten is _____ lekker.", "niet", "**niet** - 'lekker' is een bijvoeglijk naamwoord. Test: 'een lekker' kan niet.\nHet eten is **niet** lekker."),
    ("De film was _____ interessant.", "niet", "**niet** - 'interessant' is een bijvoeglijk naamwoord. Test: 'een interessant' kan niet.\nDe film was **niet** interessant."),
    ("Het is _____ moeilijk.", "niet", "**niet** - 'moeilijk' is een bijvoeglijk naamwoord. Test: 'een moeilijk' kan niet.\nHet is **niet** moeilijk."),
    ("De kamer is _____ groot.", "niet", "**niet** - 'groot' is een bijvoeglijk naamwoord. Test: 'een groot' kan niet.\nDe kamer is **niet** groot."),
    # --- NIET: verbs and adverbs ---
    ("Ik kom _____ vandaag.", "niet", "**niet** - 'vandaag' is een bijwoord. Werkwoorden en bijwoorden krijgen niet.\nIk kom **niet** vandaag."),
    ("Ze werkt _____ morgen.", "niet", "**niet** - 'morgen' is een bijwoord. Werkwoorden en bijwoorden krijgen niet.\nZe werkt **niet** morgen."),
    ("Hij slaapt _____ goed.", "niet", "**niet** - 'goed' is een bijwoord hier. Bijwoorden krijgen niet.\nHij slaapt **niet** goed."),
    ("Ze antwoordt _____.", "niet", "**niet** - werkwoord krijgt niet.\nZe antwoordt **niet**."),
    ("Hij luistert _____.", "niet", "**niet** - werkwoord krijgt niet.\nHij luistert **niet**."),
    ("Ik vind het _____ leuk.", "niet", "**niet** - 'leuk' is een bijvoeglijk naamwoord. Test: 'een leuk' kan niet.\nIk vind het **niet** leuk."),
    ("Ze lacht _____.", "niet", "**niet** - werkwoord krijgt niet.\nZe lacht **niet**."),
    # --- NIET: definite nouns (de/het) ---
    ("Ik zie de auto _____.", "niet", "**niet** - 'de auto' heeft een bepaald lidwoord. Bepaalde zelfstandige naamwoorden krijgen niet, en niet staat aan het einde.\nIk zie de auto **niet**."),
    ("Hij kent de regels _____.", "niet", "**niet** - 'de regels' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nHij kent de regels **niet**."),
    ("Ze begrijpt het probleem _____.", "niet", "**niet** - 'het probleem' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nZe begrijpt het probleem **niet**."),
    ("Ik hoor de muziek _____.", "niet", "**niet** - 'de muziek' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nIk hoor de muziek **niet**."),
    ("Hij vindt de film _____ leuk.", "niet", "**niet** - 'leuk' is een bijvoeglijk naamwoord hier. Test: 'een leuk' kan niet.\nHij vindt de film **niet** leuk."),
    # --- Mixed / tricky ---
    ("Dat is _____ waar.", "niet", "**niet** - 'waar' is een bijvoeglijk naamwoord. Test: 'een waar' kan niet.\nDat is **niet** waar."),
    ("Ze heeft _____ genoeg geld.", "niet", "**niet** - 'genoeg' is een bijwoord dat het geheel ontkent, geen zelfstandig naamwoord.\nZe heeft **niet** genoeg geld."),
    ("Ik heb _____ zin in koffie.", "geen", "**geen** - 'zin' is een onbepaald zelfstandig naamwoord. Test: 'een zin' kan.\nIk heb **geen** zin in koffie."),
    ("Hij is _____ thuis.", "niet", "**niet** - 'thuis' is een bijwoord, geen zelfstandig naamwoord.\nHij is **niet** thuis."),
    ("We hebben _____ les vandaag.", "geen", "**geen** - 'een les' kan. Geen vervangt 'een'.\nWe hebben **geen** les vandaag."),
    ("Dat hoeft _____ zo.", "niet", "**niet** - er is geen zelfstandig naamwoord. 'Zo' is een bijwoord.\nDat hoeft **niet** zo."),
    ("Hij heeft _____ honger.", "geen", "**geen** - 'honger' is een onbepaald zelfstandig naamwoord. Test: 'een honger' kan.\nHij heeft **geen** honger."),
    ("Ze is _____ klaar.", "niet", "**niet** - 'klaar' is een bijvoeglijk naamwoord. Test: 'een klaar' kan niet.\nZe is **niet** klaar."),
    ("Ik heb _____ idee.", "geen", "**geen** - 'een idee' kan. Geen vervangt 'een'.\nIk heb **geen** idee."),
    ("Het maakt _____ uit.", "niet", "**niet** - vaste uitdrukking met werkwoord. Werkwoorden krijgen niet.\nHet maakt **niet** uit."),
    ("Ik begrijp het _____.", "niet", "**niet** - 'het' is een bepaald voornaamwoord. Bepaalde objecten krijgen niet, en niet staat aan het einde.\nIk begrijp het **niet**."),
]


# =======================================================
# PER-PLAYER SESSION STATE
# =======================================================

@dataclass
class _Session:
    uid: str
    display_name: str
    used_indices: set[int] = field(default_factory=set)
    correct: int = 0
    wrong: int = 0
    streak: int = 0
    started_at: float = field(default_factory=time.time)
    current_q_index: int = -1
    msg: discord.Message | None = None   # the active question message


# =======================================================
# EMBEDS
# =======================================================

def _question_embed(session: _Session, sentence: str, q_num: int) -> discord.Embed:
    total = session.correct + session.wrong
    streak_str = f"  |  {session.streak} op rij" if session.streak >= 2 else ""
    embed = discord.Embed(
        title=f"Niet of Geen? — Vraag {q_num}",
        description=f"**{sentence}**",
    )
    embed.set_footer(
        text=f"{session.display_name}  |  Goed: {session.correct}  Fout: {session.wrong}{streak_str}"
    )
    return embed


def _feedback_embed(
    session: _Session,
    sentence: str,
    correct: str,
    explanation: str,
    was_correct: bool,
    q_num: int,
) -> discord.Embed:
    filled = sentence.replace("_____", f"**{correct}**")
    total = session.correct + session.wrong
    streak_str = f"  |  {session.streak} op rij" if session.streak >= 2 else ""
    color = discord.Color.green() if was_correct else discord.Color.red()
    result_line = "Goed!" if was_correct else f"Helaas. Het antwoord is **{correct}**."
    embed = discord.Embed(
        title=result_line,
        description=f"{filled}\n\n{explanation}",
        color=color,
    )
    embed.set_footer(
        text=f"{session.display_name}  |  Goed: {session.correct}  Fout: {session.wrong}{streak_str}"
    )
    return embed


def _final_embed(session: _Session) -> discord.Embed:
    elapsed = int(time.time() - session.started_at)
    mins, secs = divmod(elapsed, 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
    total = session.correct + session.wrong
    pct = int(session.correct / total * 100) if total else 0

    if pct == 100:
        verdict = "Perfecte score!"
    elif pct >= 80:
        verdict = "Heel goed gedaan."
    elif pct >= 60:
        verdict = "Goed bezig."
    else:
        verdict = "Blijven oefenen, je komt er."

    embed = discord.Embed(
        title="Sessie beeindigd",
        description=(
            f"**{session.display_name}**\n\n"
            f"Vragen beantwoord: **{total}**\n"
            f"Goed: **{session.correct}** ({pct}%)\n"
            f"Fout: **{session.wrong}**\n"
            f"Tijd: **{time_str}**\n\n"
            f"{verdict}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Gebruik /nietgeen om opnieuw te beginnen.")
    return embed


# =======================================================
# VIEWS
# =======================================================

class QuestionView(discord.ui.View):
    """Niet / Geen buttons + Stop button on the question embed."""

    def __init__(self, *, game: "NietGeenGame", session: _Session, sentence: str, answer: str, explanation: str, q_num: int) -> None:
        super().__init__(timeout=None)
        self._game = game
        self._session = session
        self._sentence = sentence
        self._answer = answer
        self._explanation = explanation
        self._q_num = q_num
        self._answered = False

    async def _handle_answer(self, interaction: discord.Interaction, choice: str) -> None:
        # Only the session owner can answer
        if str(interaction.user.id) != self._session.uid:
            await interaction.response.send_message(
                "Dit is niet jouw sessie. Gebruik /nietgeen om je eigen spel te starten.", ephemeral=True
            )
            return

        if self._answered:
            await interaction.response.send_message("Je hebt al geantwoord.", ephemeral=True)
            return

        self._answered = True
        was_correct = choice == self._answer

        if was_correct:
            self._session.correct += 1
            self._session.streak += 1
        else:
            self._session.wrong += 1
            self._session.streak = 0

        # Award beans for correct answers
        if was_correct:
            beans = BEANS_CORRECT + (BEANS_STREAK_BONUS if self._session.streak % 3 == 0 else 0)
            try:
                await self._game._economy.award_beans_discord(
                    user_id=int(self._session.uid),
                    amount=beans,
                    reason="Niet vs Geen correct",
                    game_key="niet_geen",
                    display_name=self._session.display_name,
                    guild_id=GUILD_NL,
                )
            except Exception:
                log.exception("NietGeen: bean award failed uid=%s", self._session.uid)

        # Show feedback on this message, then post next question
        feedback = _feedback_embed(
            self._session, self._sentence, self._answer,
            self._explanation, was_correct, self._q_num,
        )
        # Disable all buttons on the answered embed
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

        await interaction.response.edit_message(embed=feedback, view=self)

        # Post next question after a short pause
        await asyncio.sleep(1.5)
        await self._game._post_next(interaction.channel, self._session)

    @discord.ui.button(label="Niet", style=discord.ButtonStyle.primary, custom_id="ng2:niet")
    async def btn_niet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_answer(interaction, "niet")

    @discord.ui.button(label="Geen", style=discord.ButtonStyle.success, custom_id="ng2:geen")
    async def btn_geen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_answer(interaction, "geen")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="ng2:stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self._session.uid:
            await interaction.response.send_message(
                "Dit is niet jouw sessie.", ephemeral=True
            )
            return

        # Disable all buttons
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)

        await self._game._end_session(interaction.channel, self._session)


# =======================================================
# GAME CLASS
# =======================================================

class NietGeenGame:
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

        # uid -> _Session (one per player, channel-scoped via channel.id check)
        self._sessions: dict[str, _Session] = {}

    async def start_game(self, channel: discord.TextChannel, user: discord.User | discord.Member) -> None:
        uid = str(user.id)

        if uid in self._sessions:
            await channel.send(
                f"{user.display_name}, je hebt al een actief spel. Druk op **Stop** om het te beeindigen.",
                delete_after=8,
            )
            return

        session = _Session(uid=uid, display_name=user.display_name)
        self._sessions[uid] = session

        await channel.send(
            f"**{user.display_name}** — Niet vs Geen\n\n"
            "Druk op **Niet** of **Geen**. Druk op **Stop** als je klaar bent.\n"
            "De sneltest: *kan ik 'een' zeggen? Ja = geen. Nee = niet.*",
            delete_after=10,
        )
        await self._post_next(channel, session)

    async def stop_game(self, channel: discord.TextChannel, user: discord.User | discord.Member) -> None:
        uid = str(user.id)
        session = self._sessions.pop(uid, None)
        if not session:
            await channel.send("Je hebt geen actief spel.", delete_after=6)
            return
        await channel.send(embed=_final_embed(session))

    async def _post_next(self, channel: discord.abc.Messageable, session: _Session) -> None:
        uid = session.uid
        if uid not in self._sessions:
            return  # session was ended

        available = [i for i in range(len(QUESTIONS)) if i not in session.used_indices]
        if not available:
            session.used_indices.clear()
            available = list(range(len(QUESTIONS)))

        idx = random.choice(available)
        session.used_indices.add(idx)
        session.current_q_index = idx
        sentence, answer, explanation = QUESTIONS[idx]
        q_num = len(session.used_indices)

        view = QuestionView(
            game=self,
            session=session,
            sentence=sentence,
            answer=answer,
            explanation=explanation,
            q_num=q_num,
        )
        embed = _question_embed(session, sentence, q_num)
        msg = await channel.send(embed=embed, view=view)
        session.msg = msg

    async def _end_session(self, channel: discord.abc.Messageable, session: _Session) -> None:
        self._sessions.pop(session.uid, None)
        await channel.send(embed=_final_embed(session))

    async def handle_discord_message(self, message: discord.Message) -> bool:
        return False
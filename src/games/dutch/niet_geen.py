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
ROUND_SECONDS = 15


QUESTIONS: list[tuple[str, str, str]] = [
    # --- GEEN: indefinite singular nouns ---
    (
        "Ik heb _____ auto.",
        "geen",
        "**geen** - 'een auto' kan. Geen vervangt het onbepaald lidwoord 'een'.\nIk heb **geen** auto.",
    ),
    (
        "Dit is _____ probleem.",
        "geen",
        "**geen** - 'een probleem' kan. Geen vervangt 'een'.\nDit is **geen** probleem.",
    ),
    (
        "Zij heeft _____ hond.",
        "geen",
        "**geen** - 'een hond' kan. Geen vervangt 'een'.\nZij heeft **geen** hond.",
    ),
    (
        "Hij heeft _____ baan.",
        "geen",
        "**geen** - 'een baan' kan. Geen vervangt 'een'.\nHij heeft **geen** baan.",
    ),
    (
        "We hebben _____ tafel.",
        "geen",
        "**geen** - 'een tafel' kan. Geen vervangt 'een'.\nWe hebben **geen** tafel.",
    ),
    (
        "Er is _____ bus vandaag.",
        "geen",
        "**geen** - 'een bus' kan. Geen vervangt 'een'.\nEr is **geen** bus vandaag.",
    ),
    (
        "Ik heb _____ pen bij me.",
        "geen",
        "**geen** - 'een pen' kan. Geen vervangt 'een'.\nIk heb **geen** pen bij me.",
    ),
    (
        "Dit is _____ goed idee.",
        "geen",
        "**geen** - 'een goed idee' kan. Het bijvoeglijk naamwoord hoort bij het zelfstandig naamwoord.\nDit is **geen** goed idee.",
    ),
    (
        "Hij is _____ leraar.",
        "geen",
        "**geen** - 'een leraar' kan. Beroepen zijn onbepaalde zelfstandige naamwoorden.\nHij is **geen** leraar.",
    ),
    (
        "Ze heeft _____ zus.",
        "geen",
        "**geen** - 'een zus' kan. Geen vervangt 'een'.\nZe heeft **geen** zus.",
    ),
    # --- GEEN: mass / uncountable nouns ---
    (
        "We hebben _____ geld.",
        "geen",
        "**geen** - geld is een stofnaam. Stofnamen zonder lidwoord krijgen geen.\nWe hebben **geen** geld.",
    ),
    (
        "Hij heeft _____ tijd.",
        "geen",
        "**geen** - 'een tijd' kan. Geen vervangt 'een'.\nHij heeft **geen** tijd.",
    ),
    (
        "Er is _____ water meer.",
        "geen",
        "**geen** - stofnamen zonder lidwoord krijgen geen.\nEr is **geen** water meer.",
    ),
    (
        "We hebben _____ brood.",
        "geen",
        "**geen** - stofnamen zonder lidwoord krijgen geen.\nWe hebben **geen** brood.",
    ),
    (
        "Ze heeft _____ geduld.",
        "geen",
        "**geen** - 'geduld' is een onbepaald zelfstandig naamwoord.\nZe heeft **geen** geduld.",
    ),
    # --- GEEN: plurals ---
    (
        "Er zijn _____ stoelen.",
        "geen",
        "**geen** - meervoud zonder lidwoord krijgt geen. Test: 'een stoel' kan in enkelvoud.\nEr zijn **geen** stoelen.",
    ),
    (
        "We hebben _____ boodschappen gedaan.",
        "geen",
        "**geen** - meervoud zonder lidwoord krijgt geen.\nWe hebben **geen** boodschappen gedaan.",
    ),
    (
        "Er zijn _____ kaartjes meer.",
        "geen",
        "**geen** - meervoud zonder lidwoord krijgt geen.\nEr zijn **geen** kaartjes meer.",
    ),
    (
        "Ze heeft _____ vrienden hier.",
        "geen",
        "**geen** - meervoud zonder lidwoord krijgt geen.\nZe heeft **geen** vrienden hier.",
    ),
    (
        "Er zijn _____ goede restaurants hier.",
        "geen",
        "**geen** - meervoud zonder lidwoord krijgt geen.\nEr zijn **geen** goede restaurants hier.",
    ),
    # --- GEEN: languages, abstract, experience ---
    (
        "Ze spreekt _____ Nederlands.",
        "geen",
        "**geen** - taalnames als onbepaald zelfstandig naamwoord krijgen geen.\nZe spreekt **geen** Nederlands.",
    ),
    (
        "Hij heeft _____ ervaring.",
        "geen",
        "**geen** - 'een ervaring' kan. Geen vervangt 'een'.\nHij heeft **geen** ervaring.",
    ),
    (
        "We hebben _____ informatie gekregen.",
        "geen",
        "**geen** - 'informatie' is een onbepaald zelfstandig naamwoord.\nWe hebben **geen** informatie gekregen.",
    ),
    (
        "Er is _____ bewijs.",
        "geen",
        "**geen** - 'een bewijs' kan. Geen vervangt 'een'.\nEr is **geen** bewijs.",
    ),
    (
        "Ze heeft _____ zin.",
        "geen",
        "**geen** - 'een zin' kan. Geen vervangt 'een'.\nZe heeft **geen** zin.",
    ),
    # --- NIET: adjectives ---
    (
        "Het is _____ leuk.",
        "niet",
        "**niet** - 'leuk' is een bijvoeglijk naamwoord. Test: 'een leuk' kan niet.\nHet is **niet** leuk.",
    ),
    (
        "Ik ben _____ moe.",
        "niet",
        "**niet** - 'moe' is een bijvoeglijk naamwoord. Test: 'een moe' kan niet.\nIk ben **niet** moe.",
    ),
    (
        "Hij is _____ blij.",
        "niet",
        "**niet** - 'blij' is een bijvoeglijk naamwoord. Test: 'een blij' kan niet.\nHij is **niet** blij.",
    ),
    (
        "Ze is _____ ziek.",
        "niet",
        "**niet** - 'ziek' is een bijvoeglijk naamwoord. Test: 'een ziek' kan niet.\nZe is **niet** ziek.",
    ),
    (
        "Het eten is _____ lekker.",
        "niet",
        "**niet** - 'lekker' is een bijvoeglijk naamwoord. Test: 'een lekker' kan niet.\nHet eten is **niet** lekker.",
    ),
    (
        "De film was _____ interessant.",
        "niet",
        "**niet** - 'interessant' is een bijvoeglijk naamwoord. Test: 'een interessant' kan niet.\nDe film was **niet** interessant.",
    ),
    (
        "Het is _____ moeilijk.",
        "niet",
        "**niet** - 'moeilijk' is een bijvoeglijk naamwoord. Test: 'een moeilijk' kan niet.\nHet is **niet** moeilijk.",
    ),
    (
        "De kamer is _____ groot.",
        "niet",
        "**niet** - 'groot' is een bijvoeglijk naamwoord. Test: 'een groot' kan niet.\nDe kamer is **niet** groot.",
    ),
    # --- NIET: verbs and adverbs ---
    (
        "Ik kom _____ vandaag.",
        "niet",
        "**niet** - 'vandaag' is een bijwoord. Werkwoorden en bijwoorden krijgen niet.\nIk kom **niet** vandaag.",
    ),
    (
        "Ze werkt _____ morgen.",
        "niet",
        "**niet** - 'morgen' is een bijwoord. Werkwoorden en bijwoorden krijgen niet.\nZe werkt **niet** morgen.",
    ),
    (
        "Hij slaapt _____ goed.",
        "niet",
        "**niet** - 'goed' is een bijwoord hier. Bijwoorden krijgen niet.\nHij slaapt **niet** goed.",
    ),
    (
        "Ze antwoordt _____.",
        "niet",
        "**niet** - werkwoord krijgt niet.\nZe antwoordt **niet**.",
    ),
    (
        "Hij luistert _____.",
        "niet",
        "**niet** - werkwoord krijgt niet.\nHij luistert **niet**.",
    ),
    (
        "Ik vind het _____ leuk.",
        "niet",
        "**niet** - 'leuk' is een bijvoeglijk naamwoord. Test: 'een leuk' kan niet.\nIk vind het **niet** leuk.",
    ),
    (
        "Ze lacht _____.",
        "niet",
        "**niet** - werkwoord krijgt niet.\nZe lacht **niet**.",
    ),
    # --- NIET: definite nouns (de/het + noun) ---
    (
        "Ik zie de auto _____.",
        "niet",
        "**niet** - 'de auto' heeft een bepaald lidwoord. Bepaalde zelfstandige naamwoorden krijgen niet, en niet staat aan het einde.\nIk zie de auto **niet**.",
    ),
    (
        "Hij kent de regels _____.",
        "niet",
        "**niet** - 'de regels' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nHij kent de regels **niet**.",
    ),
    (
        "Ze begrijpt het probleem _____.",
        "niet",
        "**niet** - 'het probleem' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nZe begrijpt het probleem **niet**.",
    ),
    (
        "Ik hoor de muziek _____.",
        "niet",
        "**niet** - 'de muziek' is bepaald. Bepaalde zelfstandige naamwoorden krijgen niet.\nIk hoor de muziek **niet**.",
    ),
    (
        "Ik begrijp het _____.",
        "niet",
        "**niet** - 'het' is een bepaald voornaamwoord. Bepaalde objecten krijgen niet, en niet staat aan het einde.\nIk begrijp het **niet**.",
    ),
    # --- Mixed / tricky ---
    (
        "Dat is _____ waar.",
        "niet",
        "**niet** - 'waar' is een bijvoeglijk naamwoord. Test: 'een waar' kan niet.\nDat is **niet** waar.",
    ),
    (
        "Ze heeft _____ genoeg geld.",
        "niet",
        "**niet** - 'genoeg' is een bijwoord dat het geheel ontkent, geen zelfstandig naamwoord.\nZe heeft **niet** genoeg geld.",
    ),
    (
        "Ik heb _____ zin in koffie.",
        "geen",
        "**geen** - 'zin' is een onbepaald zelfstandig naamwoord. Test: 'een zin' kan.\nIk heb **geen** zin in koffie.",
    ),
    (
        "Hij is _____ thuis.",
        "niet",
        "**niet** - 'thuis' is een bijwoord, geen zelfstandig naamwoord.\nHij is **niet** thuis.",
    ),
    (
        "We hebben _____ les vandaag.",
        "geen",
        "**geen** - 'een les' kan. Geen vervangt 'een'.\nWe hebben **geen** les vandaag.",
    ),
    (
        "Dat hoeft _____ zo.",
        "niet",
        "**niet** - er is geen zelfstandig naamwoord. 'Zo' is een bijwoord.\nDat hoeft **niet** zo.",
    ),
    (
        "Hij heeft _____ honger.",
        "geen",
        "**geen** - 'honger' is een onbepaald zelfstandig naamwoord. Test: 'een honger' kan.\nHij heeft **geen** honger.",
    ),
    (
        "Ze is _____ klaar.",
        "niet",
        "**niet** - 'klaar' is een bijvoeglijk naamwoord. Test: 'een klaar' kan niet.\nZe is **niet** klaar.",
    ),
    (
        "Ik heb _____ idee.",
        "geen",
        "**geen** - 'een idee' kan. Geen vervangt 'een'.\nIk heb **geen** idee.",
    ),
    (
        "Het maakt _____ uit.",
        "niet",
        "**niet** - vaste uitdrukking met werkwoord. Werkwoorden krijgen niet.\nHet maakt **niet** uit.",
    ),
]




# =======================================================
# STATE
# =======================================================

@dataclass
class _RoundState:
    sentence: str
    answer: str
    explanation: str
    question_number: int
    answers: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class _GameState:
    round_number: int = 1
    scores: dict[str, int] = field(default_factory=dict)
    streaks: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    used_indices: set[int] = field(default_factory=set)


# =======================================================
# EMBEDS
# =======================================================

def _question_embed(
    round_state: _RoundState, game_state: _GameState, seconds_left: int
) -> discord.Embed:
    filled = int(seconds_left / ROUND_SECONDS * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    top = sorted(game_state.scores.items(), key=lambda x: -x[1])[:3]
    score_str = "  ·  ".join(
        f"{game_state.names.get(u, u)}: {s}" for u, s in top
    ) if top else "Nog geen punten"

    embed = discord.Embed(
        title=f"Niet of Geen? — Vraag {round_state.question_number}",
        description=(
            f"**{round_state.sentence}**\n\n"
            f"{bar} {seconds_left}s\n\n"
            f"{score_str}"
        ),
    )
    embed.set_footer(
        text=f"{len(round_state.answers)} antwoord(en)"
    )
    return embed


def _result_embed(round_state: _RoundState, game_state: _GameState) -> discord.Embed:
    correct = round_state.answer
    right = [round_state.names.get(u, u) for u, a in round_state.answers.items() if a == correct]
    wrong = [round_state.names.get(u, u) for u, a in round_state.answers.items() if a != correct]

    filled = round_state.sentence.replace("_____", f"**{correct}**")

    top = sorted(game_state.scores.items(), key=lambda x: -x[1])[:5]
    score_lines = "\n".join(
        f"{i+1}. {game_state.names.get(u, u)} — {s} pt"
        for i, (u, s) in enumerate(top)
    ) if top else "Nog geen punten"

    embed = discord.Embed(
        title=f"Antwoord: **{correct.upper()}**",
        description=(
            f"{filled}\n\n"
            f"{round_state.explanation}\n\n"
            f"**Goed:** {', '.join(right) if right else 'Niemand'}\n"
            f"**Fout:** {', '.join(wrong) if wrong else 'Niemand'}\n\n"
            f"**Stand:**\n{score_lines}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Volgende vraag over 4 seconden...")
    return embed


# =======================================================
# BUTTON VIEW
# =======================================================

class AnswerView(discord.ui.View):
    def __init__(self, *, round_state: _RoundState, game_state: _GameState) -> None:
        super().__init__(timeout=float(ROUND_SECONDS + 5))
        self._round = round_state
        self._game = game_state

    async def _handle(self, interaction: discord.Interaction, choice: str) -> None:
        uid = str(interaction.user.id)
        if uid in self._round.answers:
            await interaction.response.send_message(
                "Je hebt al geantwoord voor deze ronde.", ephemeral=True
            )
            return

        self._round.answers[uid] = choice
        self._round.names[uid] = interaction.user.display_name
        self._game.names[uid] = interaction.user.display_name

        is_correct = choice == self._round.answer
        if is_correct:
            await interaction.response.send_message("Goed!", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Helaas. Het antwoord is **{self._round.answer}**.", ephemeral=True
            )

    @discord.ui.button(label="Niet", style=discord.ButtonStyle.primary, custom_id="ng:niet")
    async def btn_niet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, "niet")

    @discord.ui.button(label="Geen", style=discord.ButtonStyle.success, custom_id="ng:geen")
    async def btn_geen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, "geen")


class _DisabledView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Niet", style=discord.ButtonStyle.secondary, disabled=True
        ))
        self.add_item(discord.ui.Button(
            label="Geen", style=discord.ButtonStyle.secondary, disabled=True
        ))


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

        self._active: dict[int, _GameState] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def start_game(self, channel: discord.TextChannel) -> None:
        if channel.id in self._active:
            await channel.send(
                "Er loopt al een spel. Typ **/stopnietgeen** om te stoppen.",
                delete_after=8,
            )
            return

        state = _GameState()
        self._active[channel.id] = state
        await channel.send(
            "**Niet vs Geen**\n\n"
            "Druk op de knop als je weet welke het is. "
            "Iedereen kan meedoen.\n\n"
            "De sneltest: *kan ik 'een' zeggen? Ja = geen. Nee = niet.*"
        )
        task = asyncio.create_task(self._game_loop(channel, state))
        self._tasks[channel.id] = task

    async def stop_game(self, channel: discord.TextChannel) -> None:
        state = self._active.pop(channel.id, None)
        task = self._tasks.pop(channel.id, None)
        if task:
            task.cancel()
        if not state:
            await channel.send("Er is geen actief spel.")
            return

        if state.scores:
            top = sorted(state.scores.items(), key=lambda x: -x[1])
            lines = [
                f"{i+1}. {state.names.get(u, u)} - {s} pt"
                for i, (u, s) in enumerate(top[:5])
            ]
            await channel.send("**Spel gestopt. Eindstand:**\n" + "\n".join(lines))
        else:
            await channel.send("Spel gestopt.")

    async def _game_loop(self, channel: discord.TextChannel, state: _GameState) -> None:
        try:
            while channel.id in self._active:
                available = [i for i in range(len(QUESTIONS)) if i not in state.used_indices]
                if not available:
                    state.used_indices.clear()
                    available = list(range(len(QUESTIONS)))
                idx = random.choice(available)
                state.used_indices.add(idx)
                sentence, answer, explanation = QUESTIONS[idx]

                round_state = _RoundState(
                    sentence=sentence,
                    answer=answer,
                    explanation=explanation,
                    question_number=state.round_number,
                )

                view = AnswerView(round_state=round_state, game_state=state)
                embed = _question_embed(round_state, state, ROUND_SECONDS)
                msg = await channel.send(embed=embed, view=view)

                # Countdown: update embed at 10s and 5s remaining
                await asyncio.sleep(ROUND_SECONDS - 10)
                try:
                    await msg.edit(embed=_question_embed(round_state, state, 10), view=view)
                except Exception:
                    pass

                await asyncio.sleep(5)
                try:
                    await msg.edit(embed=_question_embed(round_state, state, 5), view=view)
                except Exception:
                    pass

                await asyncio.sleep(5)

                # Disable buttons
                try:
                    await msg.edit(
                        embed=_question_embed(round_state, state, 0),
                        view=_DisabledView(),
                    )
                except Exception:
                    pass

                # Score and award beans
                correct = round_state.answer
                for uid, given in round_state.answers.items():
                    name = round_state.names.get(uid, uid)
                    state.scores.setdefault(uid, 0)
                    state.streaks.setdefault(uid, 0)

                    if given == correct:
                        state.scores[uid] += 1
                        state.streaks[uid] += 1
                        beans = BEANS_CORRECT + (
                            BEANS_STREAK_BONUS if state.streaks[uid] % 3 == 0 else 0
                        )
                        try:
                            await self._economy.award_beans_discord(
                                user_id=int(uid),
                                amount=beans,
                                reason="Niet vs Geen correct",
                                game_key=self.key,
                                display_name=name,
                                guild_id=GUILD_NL,
                            )
                        except Exception:
                            log.exception("NietGeen: bean award failed uid=%s", uid)
                    else:
                        state.streaks[uid] = 0

                await channel.send(embed=_result_embed(round_state, state))
                state.round_number += 1
                await asyncio.sleep(4)

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("NietGeen: crash channel=%s", channel.id)
            self._active.pop(channel.id, None)
            self._tasks.pop(channel.id, None)

    async def handle_discord_message(self, message: discord.Message) -> bool:
        return False
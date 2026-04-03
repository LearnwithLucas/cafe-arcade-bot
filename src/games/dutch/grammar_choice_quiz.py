from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

import discord

log = logging.getLogger("games.grammar_choice_quiz")

ROUND_SECONDS = 20


@dataclass
class GrammarQuestion:
    prompt: str
    options: tuple[str, str]
    correct_index: int
    reveal: str


@dataclass
class _RoundState:
    question: GrammarQuestion
    number: int
    answers: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class _GameState:
    title: str
    start_text: str
    done_text: str
    help_footer: str
    questions: list[GrammarQuestion]
    scores: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


class _ChoiceView(discord.ui.View):
    def __init__(self, *, rs: _RoundState, gs: _GameState) -> None:
        super().__init__(timeout=float(ROUND_SECONDS + 5))
        self._rs = rs
        self._gs = gs

        for idx, option in enumerate(rs.question.options):
            style = discord.ButtonStyle.primary if idx == 0 else discord.ButtonStyle.success
            btn = discord.ui.Button(label=option, style=style, custom_id=f"grammar:{rs.number}:{idx}")
            btn.callback = self._mk_cb(idx)
            self.add_item(btn)

    def _mk_cb(self, idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            uid = str(interaction.user.id)
            if uid in self._rs.answers:
                await interaction.response.send_message("Je hebt al geantwoord op deze vraag.", ephemeral=True)
                return

            self._rs.answers[uid] = idx
            self._rs.names[uid] = interaction.user.display_name
            self._gs.names[uid] = interaction.user.display_name
            if idx == self._rs.question.correct_index:
                self._gs.scores[uid] = self._gs.scores.get(uid, 0) + 1
                await interaction.response.send_message("✅ Correct.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Niet correct.", ephemeral=True)

        return callback


class _DisabledView(discord.ui.View):
    def __init__(self, question: GrammarQuestion) -> None:
        super().__init__(timeout=None)
        for idx, option in enumerate(question.options):
            style = discord.ButtonStyle.success if idx == question.correct_index else discord.ButtonStyle.secondary
            self.add_item(discord.ui.Button(label=option, style=style, disabled=True, custom_id=f"done:{idx}"))


class GrammarChoiceQuizGame:
    def __init__(
        self,
        *,
        title: str,
        start_text: str,
        done_text: str,
        help_footer: str,
        questions: list[GrammarQuestion],
        allowed_channel_ids: set[int],
    ) -> None:
        self._title = title
        self._start_text = start_text
        self._done_text = done_text
        self._help_footer = help_footer
        self._questions = questions
        self._allowed_channel_ids = {int(x) for x in allowed_channel_ids}
        self._active: dict[int, _GameState] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def _allowed(self, channel: discord.TextChannel) -> bool:
        return channel.id in self._allowed_channel_ids

    async def start(self, channel: discord.TextChannel) -> None:
        if not self._allowed(channel):
            await channel.send("Dit spel kan alleen in het juiste kanaal worden gestart.", delete_after=8)
            return

        if channel.id in self._active:
            await channel.send("Er loopt al een spel in dit kanaal.", delete_after=8)
            return

        gs = _GameState(
            title=self._title,
            start_text=self._start_text,
            done_text=self._done_text,
            help_footer=self._help_footer,
            questions=list(self._questions),
        )
        self._active[channel.id] = gs
        await channel.send(self._start_text)
        self._tasks[channel.id] = asyncio.create_task(self._run(channel, gs))

    async def stop(self, channel: discord.TextChannel) -> None:
        gs = self._active.pop(channel.id, None)
        task = self._tasks.pop(channel.id, None)
        if task:
            task.cancel()
        if not gs:
            await channel.send("Er is geen actief spel in dit kanaal.", delete_after=6)
            return
        await channel.send(embed=self._final_embed(gs))

    def _question_embed(self, rs: _RoundState, gs: _GameState, seconds_left: int) -> discord.Embed:
        options = "\n".join(f"**{chr(65 + i)}** — {opt}" for i, opt in enumerate(rs.question.options))
        total = len(gs.questions)
        embed = discord.Embed(
            title=f"{gs.title} — Vraag {rs.number}/{total}",
            description=f"**{rs.question.prompt}**\n\n{options}\n\n⏱️ {seconds_left}s",
        )
        top = sorted(gs.scores.items(), key=lambda x: -x[1])[:5]
        if top:
            lead = "  ·  ".join(f"{gs.names.get(uid, uid)}: {score}" for uid, score in top)
            embed.set_footer(text=f"{len(rs.answers)} antwoord(en)  |  {lead}")
        else:
            embed.set_footer(text=f"{len(rs.answers)} antwoord(en)")
        return embed

    def _result_embed(self, rs: _RoundState, gs: _GameState) -> discord.Embed:
        correct = rs.question.options[rs.question.correct_index]
        right = [rs.names.get(uid, uid) for uid, choice in rs.answers.items() if choice == rs.question.correct_index]
        wrong = [f"{rs.names.get(uid, uid)} ({rs.question.options[choice]})" for uid, choice in rs.answers.items() if choice != rs.question.correct_index]

        lines: list[str] = []
        for idx, (uid, score) in enumerate(sorted(gs.scores.items(), key=lambda x: -x[1])[:5], start=1):
            lines.append(f"{idx}. {gs.names.get(uid, uid)} — {score}")
        board = "\n".join(lines) if lines else "Nog geen punten."

        embed = discord.Embed(
            title=f"Antwoord: **{correct}**",
            description=(
                f"{rs.question.reveal}\n\n"
                f"**Goed:** {', '.join(right) if right else 'Niemand'}\n"
                f"**Fout:** {', '.join(wrong) if wrong else 'Niemand'}\n\n"
                f"**Tussenstand:**\n{board}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Volgende vraag over 3 seconden...")
        return embed

    def _final_embed(self, gs: _GameState) -> discord.Embed:
        top = sorted(gs.scores.items(), key=lambda x: -x[1])
        if not top:
            desc = "Niemand heeft meegedaan."
        else:
            medals = ["🥇", "🥈", "🥉"]
            rows = []
            for i, (uid, score) in enumerate(top[:10]):
                rank = medals[i] if i < 3 else f"{i + 1}."
                rows.append(f"{rank} {gs.names.get(uid, uid)} — {score} pt")
            desc = "\n".join(rows)
        embed = discord.Embed(title=gs.done_text, description=desc, color=discord.Color.gold())
        embed.set_footer(text=gs.help_footer)
        return embed

    async def _run(self, channel: discord.TextChannel, gs: _GameState) -> None:
        try:
            questions = random.sample(gs.questions, k=len(gs.questions))
            for idx, q in enumerate(questions, start=1):
                if channel.id not in self._active:
                    return
                rs = _RoundState(question=q, number=idx)
                view = _ChoiceView(rs=rs, gs=gs)
                msg = await channel.send(embed=self._question_embed(rs, gs, ROUND_SECONDS), view=view)

                for left in range(ROUND_SECONDS - 1, -1, -1):
                    await asyncio.sleep(1)
                    if left % 4 == 0 and left > 0:
                        try:
                            await msg.edit(embed=self._question_embed(rs, gs, left), view=view)
                        except Exception:
                            pass

                for item in view.children:
                    item.disabled = True  # type: ignore[attr-defined]
                await msg.edit(view=_DisabledView(q), embed=self._result_embed(rs, gs))
                await asyncio.sleep(3)

            if channel.id in self._active:
                await channel.send(embed=self._final_embed(gs))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Grammar quiz failed in channel %s", channel.id)
            await channel.send("Er ging iets mis tijdens de quiz.")
        finally:
            self._active.pop(channel.id, None)
            self._tasks.pop(channel.id, None)


BIJVOEGLIJK_E_QUESTIONS: list[GrammarQuestion] = [
    GrammarQuestion("Ik heb een ___ huis gekocht. (groot)", ("groot", "grote"), 0, "groot — het-woord + een = geen -e."),
    GrammarQuestion("De ___ vrouw werkt hier al jaren. (oud)", ("oud", "oude"), 1, "oude — de-woord = altijd -e."),
    GrammarQuestion("Het is een ___ boek. (goed)", ("goed", "goede"), 0, "goed — het-woord + een = geen -e."),
    GrammarQuestion("We hebben een ___ auto. (nieuw)", ("nieuw", "nieuwe"), 1, "nieuwe — de-woord + een = wel -e."),
    GrammarQuestion("Het ___ kind speelt buiten. (klein)", ("klein", "kleine"), 1, "kleine — het-woord + het = wel -e."),
    GrammarQuestion("Ik zie een ___ man op straat. (lang)", ("lang", "lange"), 1, "lange — de-woord (man) = altijd -e."),
    GrammarQuestion("Dat is het ___ antwoord. (juist)", ("juist", "juiste"), 1, "juiste — het-woord + het = wel -e."),
    GrammarQuestion("Zij kocht een ___ tafel. (rond)", ("rond", "ronde"), 1, "ronde — de-woord (tafel) = altijd -e."),
    GrammarQuestion("Wij wonen in een ___ dorp. (klein)", ("klein", "kleine"), 0, "klein — het-woord + een = geen -e."),
    GrammarQuestion("Hij leest het ___ nieuws. (laat)", ("laat", "late"), 1, "late — het-woord + het = wel -e."),
    GrammarQuestion("Ze dragen een ___ jas. (warm)", ("warm", "warme"), 1, "warme — de-woord (jas) = altijd -e."),
    GrammarQuestion("Dit is een ___ verhaal. (spannend)", ("spannend", "spannende"), 0, "spannend — het-woord + een = geen -e."),
    GrammarQuestion("Het ___ meisje lacht. (blij)", ("blij", "blije"), 1, "blije — het-woord + het = wel -e."),
    GrammarQuestion("Ik heb een ___ fiets. (snel)", ("snel", "snelle"), 1, "snelle — de-woord (fiets) = altijd -e."),
    GrammarQuestion("Een ___ raam stond open. (breed)", ("breed", "brede"), 0, "breed — het-woord + een = geen -e."),
    GrammarQuestion("Het ___ raam stond open. (breed)", ("breed", "brede"), 1, "brede — het-woord + het = wel -e."),
    GrammarQuestion("De ___ docent legt alles uit. (duidelijk)", ("duidelijk", "duidelijke"), 1, "duidelijke — de-woord = altijd -e."),
    GrammarQuestion("Een ___ boek ligt op tafel. (dik)", ("dik", "dikke"), 0, "dik — het-woord + een = geen -e."),
    GrammarQuestion("Het ___ boek ligt op tafel. (dik)", ("dik", "dikke"), 1, "dikke — het-woord + het = wel -e."),
    GrammarQuestion("Een ___ stoel ontbreekt. (extra)", ("extra", "extrae"), 0, "extra — de-woord maar adjectief op -a blijft meestal onveranderd: extra stoel."),
    GrammarQuestion("De ___ stoel ontbreekt. (extra)", ("extra", "extrae"), 0, "extra — onverbuigbaar adjectief: extra stoel/de extra stoel."),
    GrammarQuestion("Hij heeft een ___ idee. (goed)", ("goed", "goede"), 0, "goed — het-woord (idee) + een = geen -e."),
    GrammarQuestion("Het ___ idee helpt ons. (goed)", ("goed", "goede"), 1, "goede — het-woord + het = wel -e."),
    GrammarQuestion("Wij zoeken een ___ baan. (vast)", ("vast", "vaste"), 1, "vaste — de-woord (baan) = altijd -e."),
    GrammarQuestion("Dat is een ___ museum. (modern)", ("modern", "moderne"), 0, "modern — het-woord + een = geen -e."),
    GrammarQuestion("Dat is het ___ museum. (modern)", ("modern", "moderne"), 1, "moderne — het-woord + het = wel -e."),
    GrammarQuestion("Ze heeft een ___ mening. (sterk)", ("sterk", "sterke"), 1, "sterke — de-woord (mening) = altijd -e."),
    GrammarQuestion("Ik wil een ___ cadeau kopen. (mooi)", ("mooi", "mooie"), 0, "mooi — het-woord (cadeau) + een = geen -e."),
    GrammarQuestion("Het ___ cadeau ligt klaar. (mooi)", ("mooi", "mooie"), 1, "mooie — het-woord + het = wel -e."),
    GrammarQuestion("Hij huurt een ___ kamer. (klein)", ("klein", "kleine"), 1, "kleine — de-woord (kamer) = altijd -e."),
    GrammarQuestion("Ze bouwden een ___ huis. (nieuw)", ("nieuw", "nieuwe"), 0, "nieuw — het-woord + een = geen -e."),
    GrammarQuestion("Ze bouwden het ___ huis. (nieuw)", ("nieuw", "nieuwe"), 1, "nieuwe — het-woord + het = wel -e."),
    GrammarQuestion("Ik hoor een ___ stem. (zacht)", ("zacht", "zachte"), 1, "zachte — de-woord (stem) = altijd -e."),
    GrammarQuestion("Het ___ seizoen begint. (nat)", ("nat", "natte"), 1, "natte — het-woord + het = wel -e."),
    GrammarQuestion("We hebben een ___ seizoen gehad. (nat)", ("nat", "natte"), 0, "nat — het-woord + een = geen -e."),
    GrammarQuestion("De ___ bloemen ruiken lekker. (fris)", ("fris", "frisse"), 1, "frisse — de-woord meervoud = -e."),
    GrammarQuestion("Een ___ kind leert snel. (slim)", ("slim", "slimme"), 0, "slim — het-woord + een = geen -e."),
    GrammarQuestion("Het ___ kind leert snel. (slim)", ("slim", "slimme"), 1, "slimme — het-woord + het = wel -e."),
    GrammarQuestion("Een ___ appel per dag. (groen)", ("groen", "groene"), 1, "groene — de-woord (appel) = altijd -e."),
    GrammarQuestion("Het ___ water is koud. (helder)", ("helder", "heldere"), 1, "heldere — het-woord + het = wel -e."),
    GrammarQuestion("Hij draagt een ___ pak. (net)", ("net", "nette"), 0, "net — het-woord (pak) + een = geen -e."),
    GrammarQuestion("De ___ hond blaft veel. (druk)", ("druk", "drukke"), 1, "drukke — de-woord (hond) = altijd -e."),
    GrammarQuestion("Een ___ restaurant zit daar. (duur) [correctie]", ("duur", "dure"), 0, "duur — het-woord + een = geen -e."),
    GrammarQuestion("Het ___ restaurant zit daar. (duur)", ("duur", "dure"), 1, "dure — het-woord + het = wel -e."),
    GrammarQuestion("Ik koop een ___ trui. (dik)", ("dik", "dikke"), 1, "dikke — de-woord (trui) = altijd -e."),
    GrammarQuestion("Het ___ gebouw is oud. (hoog)", ("hoog", "hoge"), 1, "hoge — het-woord + het = wel -e."),
    GrammarQuestion("Wij zoeken een ___ gebouw. (hoog)", ("hoog", "hoge"), 0, "hoog — het-woord + een = geen -e."),
    GrammarQuestion("Dat is een ___ school. (groot)", ("groot", "grote"), 1, "grote — de-woord (school) = altijd -e."),
    GrammarQuestion("Ik heb een ___ bedrijf gevonden. (klein)", ("klein", "kleine"), 0, "klein — het-woord (bedrijf) + een = geen -e."),
    GrammarQuestion("Het ___ bedrijf groeit snel. (klein)", ("klein", "kleine"), 1, "kleine — het-woord + het = wel -e."),
]


DE_OF_HET_QUESTIONS: list[GrammarQuestion] = [

    GrammarQuestion("___ tafel", ("de", "het"), 0, "de tafel — de-woord; meubels zijn vaak de-woorden."),
    GrammarQuestion("___ huis", ("de", "het"), 1, "het huis — verkleinwoord/neutraal zelfstandig naamwoord: het huis."),
    GrammarQuestion("___ auto", ("de", "het"), 0, "de auto — de auto is een de-woord."),
    GrammarQuestion("___ boek", ("de", "het"), 1, "het boek — het boek is een vast het-woord."),
    GrammarQuestion("___ fiets", ("de", "het"), 0, "de fiets — de fiets is een de-woord."),
    GrammarQuestion("___ kind", ("de", "het"), 1, "het kind — het kind is een het-woord."),
    GrammarQuestion("___ man", ("de", "het"), 0, "de man — de man is een de-woord."),
    GrammarQuestion("___ vrouw", ("de", "het"), 0, "de vrouw — de vrouw is een de-woord."),
    GrammarQuestion("___ meisje", ("de", "het"), 1, "het meisje — verkleinwoord op -je krijgt altijd het."),
    GrammarQuestion("___ jongen", ("de", "het"), 0, "de jongen — de jongen is een de-woord."),
    GrammarQuestion("___ water", ("de", "het"), 1, "het water — stofnaam: meestal het bij enkelvoud."),
    GrammarQuestion("___ koffie", ("de", "het"), 0, "de koffie — de koffie als dranknaam."),
    GrammarQuestion("___ brood", ("de", "het"), 1, "het brood — het brood is een het-woord."),
    GrammarQuestion("___ zon", ("de", "het"), 0, "de zon — de zon is een de-woord."),
    GrammarQuestion("___ maan", ("de", "het"), 0, "de maan — de maan is een de-woord."),
    GrammarQuestion("___ jaar", ("de", "het"), 1, "het jaar — het jaar is een het-woord."),
    GrammarQuestion("___ week", ("de", "het"), 0, "de week — de week is een de-woord."),
    GrammarQuestion("___ dag", ("de", "het"), 0, "de dag — de dag is een de-woord."),
    GrammarQuestion("___ uur", ("de", "het"), 1, "het uur — het uur is een het-woord."),
    GrammarQuestion("___ moment", ("de", "het"), 1, "het moment — het moment is een het-woord."),
    GrammarQuestion("___ deur", ("de", "het"), 0, "de deur — de deur is een de-woord."),
    GrammarQuestion("___ raam", ("de", "het"), 1, "het raam — het raam is een het-woord."),
    GrammarQuestion("___ dak", ("de", "het"), 1, "het dak — het dak is een het-woord."),
    GrammarQuestion("___ muur", ("de", "het"), 0, "de muur — de muur is een de-woord."),
    GrammarQuestion("___ vloer", ("de", "het"), 0, "de vloer — de vloer is een de-woord."),
    GrammarQuestion("___ plafond", ("de", "het"), 1, "het plafond — het plafond is een het-woord."),
    GrammarQuestion("___ straat", ("de", "het"), 0, "de straat — de straat is een de-woord."),
    GrammarQuestion("___ dorp", ("de", "het"), 1, "het dorp — het dorp is een het-woord."),
    GrammarQuestion("___ stad", ("de", "het"), 0, "de stad — de stad is een de-woord."),
    GrammarQuestion("___ land", ("de", "het"), 1, "het land — het land is een het-woord."),
    GrammarQuestion("___ wereld", ("de", "het"), 0, "de wereld — de wereld is een de-woord."),
    GrammarQuestion("___ leven", ("de", "het"), 1, "het leven — het leven is een het-woord."),
    GrammarQuestion("___ werk", ("de", "het"), 1, "het werk — het werk is een het-woord."),
    GrammarQuestion("___ baan", ("de", "het"), 0, "de baan — de baan is een de-woord."),
    GrammarQuestion("___ school", ("de", "het"), 0, "de school — de school is een de-woord."),
    GrammarQuestion("___ bedrijf", ("de", "het"), 1, "het bedrijf — het bedrijf is een het-woord."),
    GrammarQuestion("___ kantoor", ("de", "het"), 1, "het kantoor — het kantoor is een het-woord."),
    GrammarQuestion("___ winkel", ("de", "het"), 0, "de winkel — de winkel is een de-woord."),
    GrammarQuestion("___ markt", ("de", "het"), 0, "de markt — de markt is een de-woord."),
    GrammarQuestion("___ station", ("de", "het"), 1, "het station — het station is een het-woord."),
    GrammarQuestion("___ trein", ("de", "het"), 0, "de trein — de trein is een de-woord."),
    GrammarQuestion("___ bus", ("de", "het"), 0, "de bus — de bus is een de-woord."),
    GrammarQuestion("___ vliegtuig", ("de", "het"), 1, "het vliegtuig — het vliegtuig is een het-woord."),
    GrammarQuestion("___ schip", ("de", "het"), 1, "het schip — het schip is een het-woord."),
    GrammarQuestion("___ boot", ("de", "het"), 0, "de boot — de boot is een de-woord."),
    GrammarQuestion("___ kamer", ("de", "het"), 0, "de kamer — de kamer is een de-woord."),
    GrammarQuestion("___ bed", ("de", "het"), 1, "het bed — het bed is een het-woord."),
    GrammarQuestion("___ stoel", ("de", "het"), 0, "de stoel — de stoel is een de-woord."),
    GrammarQuestion("___ bureau", ("de", "het"), 1, "het bureau — het bureau is een het-woord."),
    GrammarQuestion("___ kast", ("de", "het"), 0, "de kast — de kast is een de-woord."),
    GrammarQuestion("___ telefoon", ("de", "het"), 0, "de telefoon — de telefoon is een de-woord."),
    GrammarQuestion("___ bericht", ("de", "het"), 1, "het bericht — het bericht is een het-woord."),
    GrammarQuestion("___ woord", ("de", "het"), 1, "het woord — het woord is een het-woord."),
    GrammarQuestion("___ zin", ("de", "het"), 0, "de zin — de zin is een de-woord."),
    GrammarQuestion("___ vraag", ("de", "het"), 0, "de vraag — de vraag is een de-woord."),
    GrammarQuestion("___ antwoord", ("de", "het"), 1, "het antwoord — het antwoord is een het-woord."),
    GrammarQuestion("___ spel", ("de", "het"), 1, "het spel — het spel is een het-woord."),
    GrammarQuestion("___ wedstrijd", ("de", "het"), 0, "de wedstrijd — de wedstrijd is een de-woord."),
    GrammarQuestion("___ film", ("de", "het"), 0, "de film — de film is een de-woord."),
    GrammarQuestion("___ lied", ("de", "het"), 1, "het lied — het lied is een het-woord."),
    GrammarQuestion("___ geluid", ("de", "het"), 1, "het geluid — het geluid is een het-woord."),
    GrammarQuestion("___ muziek", ("de", "het"), 0, "de muziek — de muziek is een de-woord."),
    GrammarQuestion("___ taal", ("de", "het"), 0, "de taal — de taal is een de-woord."),
    GrammarQuestion("___ landschap", ("de", "het"), 1, "het landschap — het landschap is een het-woord."),
    GrammarQuestion("___ strand", ("de", "het"), 1, "het strand — het strand is een het-woord."),
    GrammarQuestion("___ berg", ("de", "het"), 0, "de berg — de berg is een de-woord."),
    GrammarQuestion("___ rivier", ("de", "het"), 0, "de rivier — de rivier is een de-woord."),
    GrammarQuestion("___ meer", ("de", "het"), 1, "het meer — het meer is een het-woord."),
    GrammarQuestion("___ bos", ("de", "het"), 1, "het bos — het bos is een het-woord."),
    GrammarQuestion("___ boom", ("de", "het"), 0, "de boom — de boom is een de-woord."),
    GrammarQuestion("___ bloem", ("de", "het"), 0, "de bloem — de bloem is een de-woord."),
    GrammarQuestion("___ blad", ("de", "het"), 1, "het blad — het blad is een het-woord."),
    GrammarQuestion("___ fruit", ("de", "het"), 1, "het fruit — het fruit is een het-woord."),
    GrammarQuestion("___ appel", ("de", "het"), 0, "de appel — de appel is een de-woord."),
    GrammarQuestion("___ ei", ("de", "het"), 1, "het ei — het ei is een het-woord."),
    GrammarQuestion("___ melk", ("de", "het"), 0, "de melk — de melk als dranknaam."),
    GrammarQuestion("___ suiker", ("de", "het"), 0, "de suiker — de suiker is een de-woord."),
    GrammarQuestion("___ zout", ("de", "het"), 1, "het zout — het zout is een het-woord."),
    GrammarQuestion("___ papier", ("de", "het"), 1, "het papier — het papier is een het-woord."),
    GrammarQuestion("___ pen", ("de", "het"), 0, "de pen — de pen is een de-woord."),
    GrammarQuestion("___ potlood", ("de", "het"), 1, "het potlood — het potlood is een het-woord."),
    GrammarQuestion("___ computer", ("de", "het"), 0, "de computer — de computer is een de-woord."),
    GrammarQuestion("___ programma", ("de", "het"), 1, "het programma — het programma is een het-woord."),
    GrammarQuestion("___ probleem", ("de", "het"), 1, "het probleem — woorden op -em zijn vaak het-woorden."),
    GrammarQuestion("___ idee", ("de", "het"), 1, "het idee — het idee is een het-woord."),
    GrammarQuestion("___ kans", ("de", "het"), 0, "de kans — de kans is een de-woord."),
    GrammarQuestion("___ plan", ("de", "het"), 1, "het plan — het plan is een het-woord."),
    GrammarQuestion("___ feest", ("de", "het"), 1, "het feest — het feest is een het-woord."),
    GrammarQuestion("___ familie", ("de", "het"), 0, "de familie — de familie is een de-woord."),
    GrammarQuestion("___ vriend", ("de", "het"), 0, "de vriend — de vriend is een de-woord."),
    GrammarQuestion("___ meubel", ("de", "het"), 1, "het meubel — het meubel is een het-woord."),
    GrammarQuestion("___ museum", ("de", "het"), 1, "het museum — het museum is een het-woord."),
    GrammarQuestion("___ restaurant", ("de", "het"), 1, "het restaurant — het restaurant is een het-woord."),
    GrammarQuestion("___ menu", ("de", "het"), 1, "het menu — het menu is een het-woord."),
    GrammarQuestion("___ rekening", ("de", "het"), 0, "de rekening — de rekening is een de-woord."),
    GrammarQuestion("___ dokter", ("de", "het"), 0, "de dokter — de dokter is een de-woord."),
    GrammarQuestion("___ ziekenhuis", ("de", "het"), 1, "het ziekenhuis — het ziekenhuis is een het-woord."),
    GrammarQuestion("___ medicijn", ("de", "het"), 1, "het medicijn — het medicijn is een het-woord."),
    GrammarQuestion("___ neus", ("de", "het"), 0, "de neus — de neus is een de-woord."),
    GrammarQuestion("___ hart", ("de", "het"), 1, "het hart — het hart is een het-woord."),
]

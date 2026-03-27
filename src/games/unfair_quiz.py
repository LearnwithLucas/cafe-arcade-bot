from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import discord

log = logging.getLogger("games.unfair_quiz")

ROUND_SECONDS = 20
BEANS_CORRECT = 5


# =======================================================
# ENGLISH QUESTIONS
# (question, options [A,B,C,D], correct_index 0-3, explanation)
# The joke is that the answer is technically correct but the
# question is designed to make you pick the wrong one.
# =======================================================

EN_QUESTIONS: list[tuple[str, list[str], int, str]] = [
    (
        "What is the first letter of the alphabet?",
        ["B", "C", "D", "A"],
        3,
        "D. A is the first letter. But it was option D.",
    ),
    (
        "How many sides does a triangle have?",
        ["4", "5", "6", "3"],
        3,
        "D. Three sides. You knew this. Option D got you.",
    ),
    (
        "What color do you get when you mix red and white?",
        ["Purple", "Orange", "Pink", "Blue"],
        2,
        "C. Pink. Classic.",
    ),
    (
        "What is 1 + 1?",
        ["3", "4", "1", "2"],
        3,
        "D. Two. It was always D.",
    ),
    (
        "Which planet is closest to the sun?",
        ["Venus", "Earth", "Mars", "Mercury"],
        3,
        "D. Mercury. Not Venus — Mercury.",
    ),
    (
        "How many legs does a spider have?",
        ["6", "10", "4", "8"],
        3,
        "D. Eight. Spiders have eight legs, insects have six.",
    ),
    (
        "What is the capital of France?",
        ["Lyon", "Nice", "Marseille", "Paris"],
        3,
        "D. Paris. You were distracted by the other cities.",
    ),
    (
        "What do you call a baby dog?",
        ["Kitten", "Cub", "Foal", "Puppy"],
        3,
        "D. Puppy. Option D again.",
    ),
    (
        "Which of these is NOT a primary color?",
        ["Red", "Blue", "Green", "Yellow"],
        2,
        "C. Green is not a primary color in traditional color theory. Red, blue and yellow are.",
    ),
    (
        "How many minutes are in an hour?",
        ["100", "90", "30", "60"],
        3,
        "D. 60. The other numbers were there to confuse you.",
    ),
    (
        "What is the longest river in the world?",
        ["Amazon", "Congo", "Mississippi", "Nile"],
        3,
        "D. The Nile. The Amazon is debated but the Nile is still the classic answer.",
    ),
    (
        "What is the chemical symbol for water?",
        ["CO2", "O2", "NaCl", "H2O"],
        3,
        "D. H2O. But you saw CO2 first and doubted yourself.",
    ),
    (
        "How many continents are there?",
        ["5", "6", "8", "7"],
        3,
        "D. Seven. The number depends on the model but 7 is the standard answer.",
    ),
    (
        "What is the fastest land animal?",
        ["Lion", "Horse", "Falcon", "Cheetah"],
        3,
        "D. Cheetah. The falcon is faster but it flies — land animal is cheetah.",
    ),
    (
        "Which ocean is the largest?",
        ["Atlantic", "Indian", "Arctic", "Pacific"],
        3,
        "D. The Pacific Ocean.",
    ),
    (
        "What is the hardest natural substance on Earth?",
        ["Gold", "Iron", "Quartz", "Diamond"],
        3,
        "D. Diamond. Classic trick question that isn't actually a trick.",
    ),
    (
        "How many days are in a leap year?",
        ["363", "364", "365", "366"],
        3,
        "D. 366. But you saw 365 and thought that was it.",
    ),
    (
        "What language do they speak in Brazil?",
        ["Spanish", "English", "French", "Portuguese"],
        3,
        "D. Portuguese. Not Spanish — Brazil was colonized by Portugal.",
    ),
    (
        "What is the smallest planet in our solar system?",
        ["Venus", "Mars", "Earth", "Mercury"],
        3,
        "D. Mercury. Pluto was removed from the list in 2006.",
    ),
    (
        "How many zeros are in one million?",
        ["5", "7", "8", "6"],
        3,
        "D. Six zeros. 1,000,000.",
    ),
    (
        "What is 10 x 10?",
        ["110", "1000", "10", "100"],
        3,
        "D. 100. You second-guessed yourself.",
    ),
    (
        "In which country is the Eiffel Tower located?",
        ["Belgium", "Italy", "Spain", "France"],
        3,
        "D. France. Paris, France.",
    ),
    (
        "What is the color of grass?",
        ["Blue", "Yellow", "Brown", "Green"],
        3,
        "D. Green. Unless it's very dry.",
    ),
    (
        "How many hours are in a day?",
        ["12", "48", "36", "24"],
        3,
        "D. 24 hours.",
    ),
    (
        "What do bees produce?",
        ["Milk", "Silk", "Wax only", "Honey"],
        3,
        "D. Honey. They also produce wax but the main answer is honey.",
    ),
    (
        "What is the boiling point of water at sea level in Celsius?",
        ["90", "80", "120", "100"],
        3,
        "D. 100 degrees Celsius.",
    ),
    (
        "Which gas do plants absorb from the air?",
        ["Oxygen", "Nitrogen", "Hydrogen", "Carbon dioxide"],
        3,
        "D. Carbon dioxide. Plants absorb CO2 and release oxygen.",
    ),
    (
        "What is the last letter of the English alphabet?",
        ["X", "Y", "W", "Z"],
        3,
        "D. Z. It was always D. That's the whole joke.",
    ),
    (
        "How many sides does a square have?",
        ["3", "5", "6", "4"],
        3,
        "D. Four. A square has four equal sides.",
    ),
    (
        "What is the opposite of hot?",
        ["Warm", "Cool", "Wet", "Cold"],
        3,
        "D. Cold. Warm and cool are in the middle.",
    ),
]

# =======================================================
# DUTCH QUESTIONS
# =======================================================

NL_QUESTIONS: list[tuple[str, list[str], int, str]] = [
    (
        "Wat is de eerste letter van het alfabet?",
        ["B", "C", "D", "A"],
        3,
        "D. A is de eerste letter. Maar het was optie D.",
    ),
    (
        "Hoeveel zijden heeft een driehoek?",
        ["4", "5", "6", "3"],
        3,
        "D. Drie zijden. Je wist het. Optie D heeft je te pakken.",
    ),
    (
        "Welke kleur krijg je als je rood en wit mengt?",
        ["Paars", "Oranje", "Roze", "Blauw"],
        2,
        "C. Roze. Een klassieker.",
    ),
    (
        "Wat is 1 + 1?",
        ["3", "4", "1", "2"],
        3,
        "D. Twee. Het was altijd D.",
    ),
    (
        "Welke planeet staat het dichtst bij de zon?",
        ["Venus", "Aarde", "Mars", "Mercurius"],
        3,
        "D. Mercurius. Niet Venus — Mercurius.",
    ),
    (
        "Hoeveel poten heeft een spin?",
        ["6", "10", "4", "8"],
        3,
        "D. Acht. Spinnen hebben acht poten, insecten zes.",
    ),
    (
        "Wat is de hoofdstad van Nederland?",
        ["Rotterdam", "Den Haag", "Utrecht", "Amsterdam"],
        3,
        "D. Amsterdam. Den Haag is de regeringszetel maar Amsterdam is de hoofdstad.",
    ),
    (
        "Hoe noem je een jonge hond?",
        ["Kitten", "Veulen", "Welp", "Puppy"],
        3,
        "D. Puppy. Optie D opnieuw.",
    ),
    (
        "Welke kleur heeft de zon overdag?",
        ["Rood", "Oranje", "Wit", "Geel"],
        3,
        "D. Geel. Technisch gezien is ze wit maar geel is het standaardantwoord.",
    ),
    (
        "Hoeveel minuten zitten er in een uur?",
        ["100", "90", "30", "60"],
        3,
        "D. 60. De andere getallen waren er om je af te leiden.",
    ),
    (
        "Wat is de langste rivier ter wereld?",
        ["Amazone", "Congo", "Mississippi", "Nijl"],
        3,
        "D. De Nijl. Klassiek antwoord.",
    ),
    (
        "Wat is de chemische formule voor water?",
        ["CO2", "O2", "NaCl", "H2O"],
        3,
        "D. H2O. Maar je zag CO2 als eerste en twijfelde.",
    ),
    (
        "Hoeveel continenten zijn er?",
        ["5", "6", "8", "7"],
        3,
        "D. Zeven. Het standaardantwoord in de meeste landen.",
    ),
    (
        "Wat is het snelste landdier?",
        ["Leeuw", "Paard", "Valk", "Cheetah"],
        3,
        "D. Cheetah. De valk vliegt — het gaat om landdieren.",
    ),
    (
        "Welke oceaan is de grootste?",
        ["Atlantische", "Indische", "Arctische", "Stille"],
        3,
        "D. De Stille Oceaan.",
    ),
    (
        "Wat is de hardste natuurlijke stof op aarde?",
        ["Goud", "IJzer", "Kwarts", "Diamant"],
        3,
        "D. Diamant.",
    ),
    (
        "Hoeveel dagen heeft een schrikkeljaar?",
        ["363", "364", "365", "366"],
        3,
        "D. 366. Maar je zag 365 en dacht dat het was.",
    ),
    (
        "Welke taal spreken ze in Brazilië?",
        ["Spaans", "Engels", "Frans", "Portugees"],
        3,
        "D. Portugees. Niet Spaans — Brazilië is gekoloniseerd door Portugal.",
    ),
    (
        "Wat is de kleinste planeet in ons zonnestelsel?",
        ["Venus", "Mars", "Aarde", "Mercurius"],
        3,
        "D. Mercurius. Pluto is in 2006 van de lijst gehaald.",
    ),
    (
        "Hoeveel nullen heeft één miljoen?",
        ["5", "7", "8", "6"],
        3,
        "D. Zes nullen. 1.000.000.",
    ),
    (
        "Wat is 10 x 10?",
        ["110", "1000", "10", "100"],
        3,
        "D. 100. Je twijfelde aan jezelf.",
    ),
    (
        "In welk land staat de Eiffeltoren?",
        ["België", "Italië", "Spanje", "Frankrijk"],
        3,
        "D. Frankrijk. Parijs, Frankrijk.",
    ),
    (
        "Welke kleur heeft gras?",
        ["Blauw", "Geel", "Bruin", "Groen"],
        3,
        "D. Groen. Tenzij het erg droog is.",
    ),
    (
        "Hoeveel uur heeft een dag?",
        ["12", "48", "36", "24"],
        3,
        "D. 24 uur.",
    ),
    (
        "Wat produceren bijen?",
        ["Melk", "Zijde", "Alleen was", "Honing"],
        3,
        "D. Honing. Ze produceren ook was maar honing is het hoofdantwoord.",
    ),
    (
        "Op welk kookpunt kookt water op zeeniveau in Celsius?",
        ["90", "80", "120", "100"],
        3,
        "D. 100 graden Celsius.",
    ),
    (
        "Welk gas nemen planten op uit de lucht?",
        ["Zuurstof", "Stikstof", "Waterstof", "Koolstofdioxide"],
        3,
        "D. Koolstofdioxide. Planten nemen CO2 op en geven zuurstof af.",
    ),
    (
        "Wat is de laatste letter van het Nederlandse alfabet?",
        ["X", "Y", "W", "Z"],
        3,
        "D. Z. Het was altijd D. Dat is de hele grap.",
    ),
    (
        "Hoeveel zijden heeft een vierkant?",
        ["3", "5", "6", "4"],
        3,
        "D. Vier. Een vierkant heeft vier gelijke zijden.",
    ),
    (
        "Wat is het tegenovergestelde van warm?",
        ["Lauw", "Koel", "Nat", "Koud"],
        3,
        "D. Koud. Lauw en koel zitten er tussenin.",
    ),
]

LABELS = ["A", "B", "C", "D"]
STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.success,
    discord.ButtonStyle.danger,
    discord.ButtonStyle.secondary,
]


# =======================================================
# STATE
# =======================================================

@dataclass
class _RoundState:
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    question_number: int
    is_nl: bool
    answers: dict[str, int] = field(default_factory=dict)   # uid -> option index
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class _GameState:
    is_nl: bool
    round_number: int = 1
    scores: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    used_indices: set[int] = field(default_factory=set)


# =======================================================
# EMBEDS
# =======================================================

def _question_embed(rs: _RoundState, gs: _GameState, seconds_left: int) -> discord.Embed:
    filled = int(seconds_left / ROUND_SECONDS * 10)
    bar = "🟦" * filled + "⬜" * (10 - filled)

    options_text = "\n".join(
        f"**{LABELS[i]}** — {opt}" for i, opt in enumerate(rs.options)
    )
    top = sorted(gs.scores.items(), key=lambda x: -x[1])[:3]
    score_str = "  ·  ".join(f"{gs.names.get(u, u)}: {s}" for u, s in top) if top else ""

    title = f"{'Oneerlijke Quiz' if rs.is_nl else 'Unfair Quiz'} — {'Vraag' if rs.is_nl else 'Question'} {rs.question_number}/30"
    embed = discord.Embed(
        title=title,
        description=f"**{rs.question}**\n\n{options_text}\n\n{bar} {seconds_left}s",
    )
    if score_str:
        embed.set_footer(text=score_str)
    embed.set_footer(text=f"{len(rs.answers)} {'antwoord(en)' if rs.is_nl else 'answer(s)'}  |  {score_str}")
    return embed


def _result_embed(rs: _RoundState, gs: _GameState) -> discord.Embed:
    correct_label = LABELS[rs.correct_index]
    correct_option = rs.options[rs.correct_index]

    right, wrong = [], []
    for uid, chosen in rs.answers.items():
        name = rs.names.get(uid, uid)
        if chosen == rs.correct_index:
            right.append(name)
        else:
            wrong.append(f"{name} ({LABELS[chosen]})")

    top = sorted(gs.scores.items(), key=lambda x: -x[1])[:5]
    score_lines = "\n".join(
        f"{i+1}. {gs.names.get(u, u)} — {s} pt"
        for i, (u, s) in enumerate(top)
    ) if top else ("Nog geen punten" if rs.is_nl else "No points yet")

    if rs.is_nl:
        right_str = ", ".join(right) if right else "Niemand"
        wrong_str = ", ".join(wrong) if wrong else "Niemand"
        next_str = "Volgende vraag over 4 seconden..."
    else:
        right_str = ", ".join(right) if right else "Nobody"
        wrong_str = ", ".join(wrong) if wrong else "Nobody"
        next_str = "Next question in 4 seconds..."

    embed = discord.Embed(
        title=f"{'Antwoord' if rs.is_nl else 'Answer'}: **{correct_label} — {correct_option}**",
        description=(
            f"{rs.explanation}\n\n"
            f"**{'Goed' if rs.is_nl else 'Correct'}:** {right_str}\n"
            f"**{'Fout' if rs.is_nl else 'Wrong'}:** {wrong_str}\n\n"
            f"**{'Stand' if rs.is_nl else 'Scores'}:**\n{score_lines}"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=next_str)
    return embed


def _final_embed(gs: _GameState) -> discord.Embed:
    top = sorted(gs.scores.items(), key=lambda x: -x[1])
    if not top:
        desc = "Niemand heeft meegedaan." if gs.is_nl else "Nobody played."
    else:
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, score) in enumerate(top[:10]):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {gs.names.get(uid, uid)} — {score} pt")
        desc = "\n".join(lines)

    title = "Eindstand — Oneerlijke Quiz" if gs.is_nl else "Final Scores — Unfair Quiz"
    footer = "Gebruik /oneerlijkquiz om opnieuw te spelen." if gs.is_nl else "Use /unfairquiz to play again."
    embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
    embed.set_footer(text=footer)
    return embed


# =======================================================
# BUTTON VIEW
# =======================================================

class QuizView(discord.ui.View):
    def __init__(self, *, round_state: _RoundState, game_state: _GameState) -> None:
        super().__init__(timeout=float(ROUND_SECONDS + 5))
        self._rs = round_state
        self._gs = game_state

        for i, (label, option) in enumerate(zip(LABELS, round_state.options)):
            btn = discord.ui.Button(
                label=f"{label}. {option[:40]}",
                style=STYLES[i],
                custom_id=f"uq:{round_state.question_number}:{i}",
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            uid = str(interaction.user.id)
            if uid in self._rs.answers:
                msg = "Je hebt al geantwoord." if self._rs.is_nl else "You already answered."
                await interaction.response.send_message(msg, ephemeral=True)
                return

            self._rs.answers[uid] = index
            self._rs.names[uid] = interaction.user.display_name
            self._gs.names[uid] = interaction.user.display_name

            is_correct = index == self._rs.correct_index
            chosen_label = LABELS[index]
            if is_correct:
                reply = f"✅ {chosen_label} — {'Goed!' if self._rs.is_nl else 'Correct!'}"
            else:
                reply = f"❌ {chosen_label} — {'Helaas.' if self._rs.is_nl else 'Wrong.'}"
            await interaction.response.send_message(reply, ephemeral=True)
        return callback


class _DisabledView(discord.ui.View):
    def __init__(self, *, options: list[str], correct_index: int) -> None:
        super().__init__(timeout=None)
        for i, (label, option) in enumerate(zip(LABELS, options)):
            style = discord.ButtonStyle.success if i == correct_index else discord.ButtonStyle.secondary
            btn = discord.ui.Button(
                label=f"{label}. {option[:40]}",
                style=style,
                disabled=True,
                custom_id=f"uq_done:{i}",
            )
            self.add_item(btn)


# =======================================================
# GAME CLASS
# =======================================================

class UnfairQuizGame:
    def __init__(self) -> None:
        self._active: dict[int, _GameState] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(self, channel: discord.TextChannel, is_nl: bool) -> None:
        if channel.id in self._active:
            msg = "Er loopt al een quiz. Wacht tot die klaar is." if is_nl else "A quiz is already running. Wait for it to finish."
            await channel.send(msg, delete_after=8)
            return

        state = _GameState(is_nl=is_nl)
        self._active[channel.id] = state

        if is_nl:
            await channel.send(
                "**De Oneerlijke Quiz**\n\n"
                "30 vragen. De antwoorden zijn correct — maar de vragen zijn ontworpen om je te laten twijfelen.\n"
                "Iedereen kan meedoen. Druk op de knop als je het antwoord weet."
            )
        else:
            await channel.send(
                "**The Unfair Quiz**\n\n"
                "30 questions. The answers are correct — but the questions are designed to trick you.\n"
                "Everyone can play. Press a button when you know the answer."
            )

        task = asyncio.create_task(self._run(channel, state))
        self._tasks[channel.id] = task

    async def stop(self, channel: discord.TextChannel) -> None:
        state = self._active.pop(channel.id, None)
        task = self._tasks.pop(channel.id, None)
        if task:
            task.cancel()
        if not state:
            msg = "Er is geen actieve quiz." if False else "No active quiz."
            await channel.send(msg, delete_after=6)
            return
        await channel.send(embed=_final_embed(state))

    async def _run(self, channel: discord.TextChannel, state: _GameState) -> None:
        questions = NL_QUESTIONS if state.is_nl else EN_QUESTIONS
        try:
            indices = random.sample(range(len(questions)), k=min(30, len(questions)))

            for q_num, idx in enumerate(indices, start=1):
                if channel.id not in self._active:
                    return

                question, options, correct_index, explanation = questions[idx]
                rs = _RoundState(
                    question=question,
                    options=options,
                    correct_index=correct_index,
                    explanation=explanation,
                    question_number=q_num,
                    is_nl=state.is_nl,
                )
                state.round_number = q_num

                view = QuizView(round_state=rs, game_state=state)
                embed = _question_embed(rs, state, ROUND_SECONDS)
                msg = await channel.send(embed=embed, view=view)

                # Countdown updates at 10s and 5s
                await asyncio.sleep(ROUND_SECONDS - 10)
                try:
                    await msg.edit(embed=_question_embed(rs, state, 10), view=view)
                except Exception:
                    pass

                await asyncio.sleep(5)
                try:
                    await msg.edit(embed=_question_embed(rs, state, 5), view=view)
                except Exception:
                    pass

                await asyncio.sleep(5)

                # Lock buttons — green = correct, grey = wrong
                try:
                    await msg.edit(
                        embed=_question_embed(rs, state, 0),
                        view=_DisabledView(options=rs.options, correct_index=rs.correct_index),
                    )
                except Exception:
                    pass

                # Score
                for uid, chosen in rs.answers.items():
                    state.scores.setdefault(uid, 0)
                    if chosen == rs.correct_index:
                        state.scores[uid] += BEANS_CORRECT

                await channel.send(embed=_result_embed(rs, state))
                await asyncio.sleep(4)

            # Game over
            self._active.pop(channel.id, None)
            self._tasks.pop(channel.id, None)
            await channel.send(embed=_final_embed(state))

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("UnfairQuiz: crash channel=%s", channel.id)
            self._active.pop(channel.id, None)
            self._tasks.pop(channel.id, None)

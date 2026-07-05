from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscordInstruction:
    channel_id: int
    marker: str
    title: str
    body: str
    commands: tuple[str, ...]
    notes: tuple[str, ...] = ()


# Discord hubs
EN_HUB_CHANNEL_ID = 1482998106898563092
NL_HUB_CHANNEL_ID = 1482998054121377903

# Discord game channels - English server
WORD_CHAIN_CHANNEL_ID = 1481745881123520573
WORDLE_CHANNEL_ID = 1481745735652474920
UNSCRAMBLE_CHANNEL_ID = 1481745817021845607
LEADERBOARD_CHANNEL_ID = 1523039716063182858
ENGLISH_PRACTICE_GAME_CHANNEL_ID = 1523035685068607612
GEO_LEARNING_CHANNEL_ID = 0
GEO_FLAGS_CHANNEL_ID = 1481763185668395263
GEO_LANGUAGE_CHANNEL_ID = 1481763326164865087

# Discord game channels - Dutch server
DUTCH_WORDLE_CHANNEL_ID = 1482763022173995119
DUTCH_UNSCRAMBLE_CHANNEL_ID = 1482763069238153419
DUTCH_WORD_CHAIN_CHANNEL_ID = 1482763114842816765
DUTCH_NIET_GEEN_CHANNEL_ID = 1487175077702275273
DUTCH_BIJVOEGLIJK_CHANNEL_ID = 1489703986129801216
DUTCH_DE_OF_HET_CHANNEL_ID = 1489704987859615895


DISCORD_GAME_INSTRUCTIONS: tuple[DiscordInstruction, ...] = (
    DiscordInstruction(
        channel_id=WORDLE_CHANNEL_ID,
        marker="instructions:discord:wordle:en:v1",
        title="Wordle",
        body="Guess the hidden 5-letter English word. Everyone plays in this channel.",
        commands=(
            "/games wordle_start - start today's puzzle",
            "/games wordle_hint - reveal one letter",
            "/games wordle_restart - restart the channel puzzle",
        ),
        notes=("Type each 5-letter guess as a normal message in this channel.",),
    ),
    DiscordInstruction(
        channel_id=UNSCRAMBLE_CHANNEL_ID,
        marker="instructions:discord:unscramble:en:v1",
        title="Unscramble",
        body="Unscramble the letters before your guesses run out.",
        commands=(
            "/games unscramble_start - start your puzzle panel",
            "/games unscramble_hint - reveal the first letter",
            "/games unscramble_stop - stop your current round",
            "/games unscramble_restart - start a fresh word",
        ),
        notes=("Type your answer as a normal message after the panel appears.",),
    ),
    DiscordInstruction(
        channel_id=WORD_CHAIN_CHANNEL_ID,
        marker="instructions:discord:word-chain:en:v1",
        title="Word Chain",
        body="Build a chain where each word starts with the previous word's last letter.",
        commands=(
            "/games wordchain_start - start a live chain",
        ),
        notes=("Type each next word as a normal message in this channel.",),
    ),
    DiscordInstruction(
        channel_id=ENGLISH_PRACTICE_GAME_CHANNEL_ID,
        marker="instructions:discord:english-practice-game:en:v1",
        title="English Practice Game",
        body="Play the Learn with Lucas website practice game and use the games leaderboard to compare Discord game progress.",
        commands=(
            "https://learnwithlucas.com/english-practice-game/ - play the website game",
            "/play - choose a Discord game",
        ),
        notes=(
            "Website scores are not sent to Discord automatically yet.",
            "Discord game scores appear in <#1523039716063182858>.",
        ),
    ),
    DiscordInstruction(
        channel_id=GEO_FLAGS_CHANNEL_ID,
        marker="instructions:discord:geo-flags:en:v1",
        title="GeoGuessr Flags",
        body="Guess the country from the flag challenge.",
        commands=(
            "/geoguessr flags_start - start a flag round",
            "/geoguessr flags_stop - stop your current round",
            "/geoguessr flags_help - show the rules",
        ),
        notes=("Answer in this channel when a round is active.",),
    ),
    DiscordInstruction(
        channel_id=DUTCH_WORDLE_CHANNEL_ID,
        marker="instructions:discord:wordle:nl:v1",
        title="Woordle",
        body="Raad het verborgen Nederlandse woord van 5 letters.",
        commands=(
            "/games wordle_nl_start - start de dagelijkse Woordle",
            "/games wordle_nl_hint - onthul een letter",
            "/games wordle_nl_restart - herstart de puzzel in dit kanaal",
        ),
        notes=("Typ elk woord van 5 letters als normaal bericht in dit kanaal.",),
    ),
    DiscordInstruction(
        channel_id=DUTCH_NIET_GEEN_CHANNEL_ID,
        marker="instructions:discord:niet-geen:nl:v1",
        title="Niet vs Geen",
        body="Oefen wanneer je 'niet' of 'geen' gebruikt.",
        commands=(
            "/nietgeen - start het spel",
            "/stopnietgeen - stop je sessie",
        ),
    ),
    DiscordInstruction(
        channel_id=DUTCH_BIJVOEGLIJK_CHANNEL_ID,
        marker="instructions:discord:bijvoeglijk:nl:v1",
        title="Bijvoeglijk naamwoord",
        body="Oefen -e of geen -e bij Nederlandse bijvoeglijke naamwoorden.",
        commands=(
            "/bijvoeglijk_start - start de quiz",
            "/bijvoeglijk_stop - stop de quiz",
        ),
    ),
    DiscordInstruction(
        channel_id=DUTCH_DE_OF_HET_CHANNEL_ID,
        marker="instructions:discord:de-of-het:nl:v1",
        title="De of Het",
        body="Oefen Nederlandse lidwoorden met korte uitleg na elke vraag.",
        commands=(
            "/deofhet_start - start de quiz",
            "/deofhet_stop - stop de quiz",
        ),
    ),
)


def build_telegram_instruction_text(bot_username: str | None = None) -> str:
    suffix = f"@{bot_username}" if bot_username else ""
    return (
        "Jerry The Duck game instructions\n"
        "instructions:telegram:main:v1\n\n"
        "Use the bot mention in groups if plain slash commands do not wake Jerry up.\n\n"
        "Wordle\n"
        f"/wordle{suffix} - start today's 5-letter puzzle\n"
        f"/hint{suffix} - reveal one letter\n\n"
        "Unscramble\n"
        f"/unscramble{suffix} - start a scrambled word\n"
        f"/hint{suffix} - get a clue\n"
        f"/skip{suffix} - reveal the answer\n\n"
        "Word Chain\n"
        f"/wordchain{suffix} - start a word chain\n"
        f"/stopchain{suffix} - end the chain\n\n"
        f"/end{suffix} - end your active game\n"
        f"/help{suffix} - show the full command list"
    )

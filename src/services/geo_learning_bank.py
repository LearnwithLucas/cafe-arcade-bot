from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple


_SCRIPT_CHOICES = [
    "Latin",
    "Cyrillic",
    "Arabic",
    "Devanagari",
    "Han",
    "Hangul",
    "Hiragana",
    "Katakana",
    "Thai",
    "Hebrew",
    "Greek",
    "Georgian",
    "Armenian",
    "Bengali",
    "Gurmukhi",
    "Tamil",
    "Telugu",
]


def normalize_text(s: str) -> str:
    """
    Normalizes user input for matching country names:
    - lowercase
    - strips punctuation
    - collapses whitespace
    """
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass(frozen=True)
class FlagItem:
    country: str
    emoji: str
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptItem:
    country: str
    script: str
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Question:
    qtype: str  # "flag" | "script"
    prompt: str
    answer: str
    accepted: Tuple[str, ...]


class GeoLearningBank:
    """
    Small curated bank for learning quizzes.
    Extend by adding to _FLAGS and _SCRIPTS below.
    """

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.Random()
        self._flags: List[FlagItem] = list(_FLAGS)
        self._scripts: List[ScriptItem] = list(_SCRIPTS)

        self._country_to_flag: Dict[str, FlagItem] = {f.country: f for f in self._flags}
        self._country_to_script: Dict[str, ScriptItem] = {s.country: s for s in self._scripts}

    def script_choices(self) -> List[str]:
        return list(_SCRIPT_CHOICES)

    def get_random_question(
        self,
        qtype: str,
        exclude_answers: Optional[Set[str]] = None,
    ) -> Question:
        """
        qtype:
          - "flag": show flag emoji, answer is country
          - "script": show country, answer is script
        exclude_answers: normalized answers to avoid repeats
        """
        exclude_answers = exclude_answers or set()

        if qtype == "flag":
            pool = self._flags
            if not pool:
                raise ValueError("Flag pool is empty.")
            for _ in range(50):
                item = self._rng.choice(pool)
                ans_norm = normalize_text(item.country)
                if ans_norm not in exclude_answers:
                    return Question(
                        qtype="flag",
                        prompt=f"🌍 **Which country is this flag?** {item.emoji}",
                        answer=item.country,
                        accepted=self._build_country_accepts(item.country, item.aliases),
                    )
            item = self._rng.choice(pool)
            return Question(
                qtype="flag",
                prompt=f"🌍 **Which country is this flag?** {item.emoji}",
                answer=item.country,
                accepted=self._build_country_accepts(item.country, item.aliases),
            )

        if qtype == "script":
            pool = self._scripts
            if not pool:
                raise ValueError("Script pool is empty.")
            for _ in range(50):
                item = self._rng.choice(pool)
                ans_norm = normalize_text(item.script)
                key = f"{normalize_text(item.country)}::{ans_norm}"
                if key not in exclude_answers:
                    return Question(
                        qtype="script",
                        prompt=(
                            f"📝 **Which script is most commonly used for official writing in:** "
                            f"**{item.country}**?\n"
                            f"Reply with one of: `{', '.join(self.script_choices())}`"
                        ),
                        answer=item.script,
                        accepted=self._build_script_accepts(item.script),
                    )
            item = self._rng.choice(pool)
            return Question(
                qtype="script",
                prompt=(
                    f"📝 **Which script is most commonly used for official writing in:** "
                    f"**{item.country}**?\n"
                    f"Reply with one of: `{', '.join(self.script_choices())}`"
                ),
                answer=item.script,
                accepted=self._build_script_accepts(item.script),
            )

        raise ValueError(f"Unknown qtype: {qtype}")

    @staticmethod
    def _build_country_accepts(country: str, aliases: Sequence[str]) -> Tuple[str, ...]:
        accepts = {normalize_text(country)}
        for a in aliases:
            accepts.add(normalize_text(a))
        # Some common variants:
        accepts.add(normalize_text(country.replace("&", "and")))
        return tuple(sorted(accepts))

    @staticmethod
    def _build_script_accepts(script: str) -> Tuple[str, ...]:
        # Accept exact script name, case-insensitive, minor punctuation
        accepts = {normalize_text(script)}
        # Common shortcuts
        shortcuts = {
            "han": ["chinese", "chinese characters"],
            "latin": ["roman", "roman script"],
            "cyrillic": ["cyrillic script"],
            "arabic": ["arabic script"],
            "devanagari": ["devnagari", "devanagri"],
        }
        base = normalize_text(script)
        for s in shortcuts.get(base, []):
            accepts.add(normalize_text(s))
        return tuple(sorted(accepts))


# --- Curated bank (starter set) ---
# Flags: emoji flags are widely supported in Discord.
_FLAGS: Tuple[FlagItem, ...] = (
    FlagItem("United States", "🇺🇸", ("USA", "United States of America", "US", "America")),
    FlagItem("Canada", "🇨🇦"),
    FlagItem("Mexico", "🇲🇽"),
    FlagItem("Brazil", "🇧🇷"),
    FlagItem("Argentina", "🇦🇷"),
    FlagItem("United Kingdom", "🇬🇧", ("UK", "Great Britain", "Britain")),
    FlagItem("Ireland", "🇮🇪"),
    FlagItem("France", "🇫🇷"),
    FlagItem("Germany", "🇩🇪"),
    FlagItem("Spain", "🇪🇸"),
    FlagItem("Portugal", "🇵🇹"),
    FlagItem("Italy", "🇮🇹"),
    FlagItem("Netherlands", "🇳🇱", ("Holland",)),
    FlagItem("Belgium", "🇧🇪"),
    FlagItem("Switzerland", "🇨🇭"),
    FlagItem("Austria", "🇦🇹"),
    FlagItem("Sweden", "🇸🇪"),
    FlagItem("Norway", "🇳🇴"),
    FlagItem("Denmark", "🇩🇰"),
    FlagItem("Finland", "🇫🇮"),
    FlagItem("Poland", "🇵🇱"),
    FlagItem("Czechia", "🇨🇿", ("Czech Republic",)),
    FlagItem("Greece", "🇬🇷"),
    FlagItem("Turkey", "🇹🇷", ("Türkiye",)),
    FlagItem("Ukraine", "🇺🇦"),
    FlagItem("Russia", "🇷🇺", ("Russian Federation",)),
    FlagItem("Egypt", "🇪🇬"),
    FlagItem("Morocco", "🇲🇦"),
    FlagItem("Nigeria", "🇳🇬"),
    FlagItem("South Africa", "🇿🇦"),
    FlagItem("Kenya", "🇰🇪"),
    FlagItem("India", "🇮🇳"),
    FlagItem("Pakistan", "🇵🇰"),
    FlagItem("China", "🇨🇳", ("PRC", "People's Republic of China")),
    FlagItem("Japan", "🇯🇵"),
    FlagItem("South Korea", "🇰🇷", ("Korea", "Republic of Korea")),
    FlagItem("Vietnam", "🇻🇳"),
    FlagItem("Thailand", "🇹🇭"),
    FlagItem("Indonesia", "🇮🇩"),
    FlagItem("Philippines", "🇵🇭", ("The Philippines",)),
    FlagItem("Australia", "🇦🇺"),
    FlagItem("New Zealand", "🇳🇿"),
)

# Scripts: simplified “most common official writing script” (starter set).
_SCRIPTS: Tuple[ScriptItem, ...] = (
    ScriptItem("United States", "Latin", ("USA", "United States of America")),
    ScriptItem("Canada", "Latin"),
    ScriptItem("Mexico", "Latin"),
    ScriptItem("Brazil", "Latin"),
    ScriptItem("France", "Latin"),
    ScriptItem("Germany", "Latin"),
    ScriptItem("Spain", "Latin"),
    ScriptItem("Greece", "Greek"),
    ScriptItem("Russia", "Cyrillic"),
    ScriptItem("Ukraine", "Cyrillic"),
    ScriptItem("Serbia", "Cyrillic", ("Republic of Serbia",)),
    ScriptItem("Bulgaria", "Cyrillic"),
    ScriptItem("Turkey", "Latin", ("Türkiye",)),
    ScriptItem("Egypt", "Arabic"),
    ScriptItem("Saudi Arabia", "Arabic"),
    ScriptItem("Israel", "Hebrew"),
    ScriptItem("India", "Devanagari", ("Republic of India",)),
    ScriptItem("Pakistan", "Arabic"),
    ScriptItem("Bangladesh", "Bengali"),
    ScriptItem("Thailand", "Thai"),
    ScriptItem("China", "Han"),
    ScriptItem("Japan", "Kanji", ("Japan (mixed)", "Japanese")),  # see note below
    ScriptItem("South Korea", "Hangul", ("Korea", "Republic of Korea")),
    ScriptItem("Georgia", "Georgian"),
    ScriptItem("Armenia", "Armenian"),
)

"""
NOTE:
Japan uses a mix (Kanji/Hiragana/Katakana). For a “simple quiz”, we accept "Han"/"Kanji"/etc.
If you prefer strict script list, replace "Kanji" with "Han" or expand accepted variants in GeoLearningBank.
"""

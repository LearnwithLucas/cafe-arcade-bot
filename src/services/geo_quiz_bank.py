from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoQuizItem:
    """
    One quiz prompt item.

    prompt: what we show (flag emoji, text sample, etc.)
    answer: canonical expected answer (normalized by the game)
    aliases: other accepted answers (normalized by the game)
    """
    prompt: str
    answer: str
    aliases: list[str]


class GeoQuizBank:
    """
    Loads Geo quiz datasets from JSON files on disk (Render-safe).
    This is intentionally simple and dependency-free.

    Expected JSON format:
      [
        {"prompt": "🇯🇵", "answer": "japan", "aliases": ["nippon"]},
        ...
      ]
    """

    def __init__(self, *, flags_items: list[GeoQuizItem], language_items: list[GeoQuizItem]) -> None:
        self._flags = flags_items
        self._language = language_items

    @staticmethod
    def _parse_items(raw: Any) -> list[GeoQuizItem]:
        if not isinstance(raw, list):
            return []
        out: list[GeoQuizItem] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            prompt = str(row.get("prompt", "")).strip()
            answer = str(row.get("answer", "")).strip()
            aliases_raw = row.get("aliases", [])
            aliases: list[str] = []
            if isinstance(aliases_raw, list):
                aliases = [str(x).strip() for x in aliases_raw if str(x).strip()]
            if prompt and answer:
                out.append(GeoQuizItem(prompt=prompt, answer=answer, aliases=aliases))
        return out

    @staticmethod
    def _load_json_file(path: Path) -> list[GeoQuizItem]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return GeoQuizBank._parse_items(data)
        except FileNotFoundError:
            logger.warning("GeoQuizBank: file not found: %s", path)
            return []
        except Exception:
            logger.exception("GeoQuizBank: failed reading %s", path)
            return []

    @classmethod
    def load_from_assets(
        cls,
        *,
        flags_path: Path = Path("src/assets/geo/flags.json"),
        language_path: Path = Path("src/assets/geo/languages.json"),
    ) -> "GeoQuizBank":
        flags_items = cls._load_json_file(flags_path)
        language_items = cls._load_json_file(language_path)

        # Fallback minimal sets so the bot remains usable even if JSON isn't present yet.
        if not flags_items:
            flags_items = [
                GeoQuizItem(prompt="🇺🇸", answer="united states", aliases=["usa", "us", "united states of america", "america"]),
                GeoQuizItem(prompt="🇯🇵", answer="japan", aliases=["nippon"]),
                GeoQuizItem(prompt="🇧🇷", answer="brazil", aliases=["brasil"]),
                GeoQuizItem(prompt="🇫🇷", answer="france", aliases=[]),
            ]
            logger.warning("GeoQuizBank: using fallback FLAGS dataset (add %s to customize)", flags_path)

        if not language_items:
            language_items = [
                GeoQuizItem(prompt="Καλημέρα", answer="greek", aliases=["hellenic", "ellinika"]),
                GeoQuizItem(prompt="こんにちは", answer="japanese", aliases=["nihongo"]),
                GeoQuizItem(prompt="Bonjour", answer="french", aliases=["français", "francais"]),
                GeoQuizItem(prompt="Hola", answer="spanish", aliases=["español", "espanol"]),
            ]
            logger.warning("GeoQuizBank: using fallback LANGUAGE dataset (add %s to customize)", language_path)

        logger.info("GeoQuizBank loaded: flags=%s language=%s", len(flags_items), len(language_items))
        return cls(flags_items=flags_items, language_items=language_items)

    def random_flag(self) -> GeoQuizItem:
        return random.choice(self._flags)

    def random_language(self) -> GeoQuizItem:
        return random.choice(self._language)

    def flags_count(self) -> int:
        return len(self._flags)

    def language_count(self) -> int:
        return len(self._language)

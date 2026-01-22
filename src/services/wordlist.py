from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WordList:
    """
    In-memory English word list loaded from a text file (one word per line).
    Designed to be loaded once at startup and reused across games.

    File format:
      each
      apple
      eagle
      ...
    """

    words: set[str]

    @classmethod
    def load_from_txt(cls, path: Path) -> "WordList":
        if not path.exists():
            raise FileNotFoundError(f"Word list file not found: {path}")

        words: set[str] = set()
        loaded = 0

        # utf-8 with errors ignored to be resilient to odd characters
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                w = line.strip().lower()
                if not w:
                    continue
                # keep only simple alphabetic words; your list likely already is
                if not w.isalpha():
                    continue
                words.add(w)
                loaded += 1

        logger.info("Loaded %s words from %s", len(words), path)
        return cls(words=words)

    def is_word(self, w: str) -> bool:
        """
        True if the normalized lowercase word is in the list.
        """
        return w in self.words

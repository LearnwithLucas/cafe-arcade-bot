from __future__ import annotations

import re

_WORD_RE = re.compile(r"^[a-zA-Z]+$")


def normalize_word(raw: str) -> str:
    return raw.strip().lower()


def is_valid_word_shape(word: str, *, min_len: int = 3) -> bool:
    if len(word) < min_len:
        return False
    return bool(_WORD_RE.match(word))

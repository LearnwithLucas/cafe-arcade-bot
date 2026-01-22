from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class RewardKey:
    # English games
    WORDLE_SOLVE = "wordle.solve"
    WORDLE_FAIL_PER_GREEN = "wordle.fail_per_green"

    WORD_CHAIN_ROUND_PAYOUT = "word_chain.round_payout"

    UNSCRAMBLE_SOLVE = "unscramble.solve"
    UNSCRAMBLE_FAIL_PER_REVEALED = "unscramble.fail_per_revealed"

    # Core economy actions
    CORE_DAILY = "core.daily"
    CORE_WORK = "core.work"

    # Geo learning (existing/legacy training)
    GEO_LEARNING_CORRECT = "geo_learning.correct"
    GEO_LEARNING_COMPLETE = "geo_learning.complete"

    # GeoGuessr arcade games (new)
    GEO_FLAGS_CORRECT = "geo_flags.correct"
    GEO_LANGUAGE_CORRECT = "geo_language.correct"


@dataclass(frozen=True)
class RewardRule:
    """
    Simple rule: integer amount.

    You can interpret this amount in two ways:
      - a fixed payout (e.g. wordle.solve = 20)
      - a multiplier (e.g. unscramble.fail_per_revealed = 2)

    We keep it intentionally minimal for now.
    """
    amount: int


class RewardsService:
    """
    Central source of truth for bean payouts.

    Games should NOT hardcode beans. They should ask:
      rewards.amount(RewardKey.UNSCRAMBLE_SOLVE)
      rewards.amount(RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED) * revealed_letters

    Later, you can extend this to:
      - load overrides from SQLite
      - admin commands to change rules without redeploy
      - caps/cooldowns at the reward layer
    """

    def __init__(self, overrides: Mapping[str, int] | None = None) -> None:
        base: dict[str, int] = dict(self.default_rules())
        if overrides:
            for k, v in overrides.items():
                if isinstance(k, str):
                    base[k] = int(v)
        self._rules: dict[str, RewardRule] = {k: RewardRule(int(v)) for k, v in base.items()}

    @staticmethod
    def default_rules() -> Mapping[str, int]:
        """
        Default payout configuration.

        Change values here to rebalance the economy.
        """
        return {
            # Wordle
            RewardKey.WORDLE_SOLVE: 20,
            RewardKey.WORDLE_FAIL_PER_GREEN: 2,

            # Word Chain
            # (If your WordChain currently awards per accepted word, set this to 1.
            #  If your WordChain awards a fixed round payout, set this to that number.)
            RewardKey.WORD_CHAIN_ROUND_PAYOUT: 1,

            # Unscramble
            RewardKey.UNSCRAMBLE_SOLVE: 5,
            RewardKey.UNSCRAMBLE_FAIL_PER_REVEALED: 2,

            # Core economy
            RewardKey.CORE_DAILY: 25,
            RewardKey.CORE_WORK: 5,

            # Geo learning (legacy training module)
            RewardKey.GEO_LEARNING_CORRECT: 2,
            RewardKey.GEO_LEARNING_COMPLETE: 10,

            # GeoGuessr arcade games (new)
            RewardKey.GEO_FLAGS_CORRECT: 3,
            RewardKey.GEO_LANGUAGE_CORRECT: 4,
        }

    def amount(self, key: str) -> int:
        """
        Return the integer amount for a reward key.
        Raises KeyError if missing (intentional: catches typos early).
        """
        rule = self._rules.get(key)
        if not rule:
            raise KeyError(f"Unknown reward key: {key}")
        return int(rule.amount)

    def has(self, key: str) -> bool:
        return key in self._rules

    def snapshot(self) -> dict[str, int]:
        """
        Useful for debugging or exposing later via an admin command.
        """
        return {k: int(v.amount) for k, v in self._rules.items()}

    def with_overrides(self, overrides: Mapping[str, int]) -> "RewardsService":
        """
        Convenience: create a new RewardsService with merged overrides.
        """
        merged = self.snapshot()
        for k, v in overrides.items():
            merged[k] = int(v)
        return RewardsService(overrides=merged)

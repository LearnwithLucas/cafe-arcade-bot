from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class CooldownResult:
    allowed: bool
    retry_after_seconds: float


class Cooldowns:
    """
    Cooldown tracker with two layers:

    1) In-memory cooldowns (fast; NOT restart-safe)
       - good for spam prevention and high-frequency checks

    2) Restart-safe cooldowns (time-based; uses epoch seconds)
       - optional: can be backed by SQLite or any persistent store later

    This file now supports both, without changing existing callers.

    How it works:
      - .try_acquire(...) and .check(...) remain in-memory (existing behavior).
      - .try_acquire_epoch(...) and .check_epoch(...) are restart-safe *in principle*
        because they store epoch seconds (not monotonic), and can be backed by DB.

    To make /core daily and /core work restart-safe on Render, we will:
      - add a small SQLite repo/table that stores (action, user_id, location_id, last_ts_utc)
      - plug it into commands.py for those two commands only
    """

    def __init__(self) -> None:
        # -------------------------
        # In-memory (monotonic) layer
        # -------------------------
        # key = (action, user_id, location_id) -> last_timestamp (monotonic)
        self._last: dict[tuple[str, str, str], float] = {}

        # -------------------------
        # Restart-safe (epoch) layer
        # -------------------------
        # key = (action, user_id, location_id) -> last_timestamp_epoch_seconds
        # NOTE: This dict itself is NOT persistent, but the values are restart-safe.
        # Later we can wire these values to SQLite without changing the API.
        self._last_epoch: dict[tuple[str, str, str], int] = {}

    # =========================================================
    # In-memory (monotonic) cooldowns — existing behavior
    # =========================================================

    def check(
        self,
        *,
        action: str,
        user_id: str,
        location_id: str,
        cooldown_seconds: float,
    ) -> CooldownResult:
        now = time.monotonic()
        key = (action, user_id, location_id)
        last = self._last.get(key)

        if last is None:
            self._last[key] = now
            return CooldownResult(True, 0.0)

        elapsed = now - last
        if elapsed >= cooldown_seconds:
            self._last[key] = now
            return CooldownResult(True, 0.0)

        return CooldownResult(False, cooldown_seconds - elapsed)

    def try_acquire(
        self,
        action: str,
        user_id: int | str,
        location_id: int | str,
        cooldown_seconds: float,
    ) -> tuple[bool, int]:
        """
        Stable helper for commands/services.

        Returns:
          (allowed, retry_after_seconds_int)

        Uses the existing .check() method (monotonic, in-memory).
        """
        res = self.check(
            action=str(action),
            user_id=str(user_id),
            location_id=str(location_id),
            cooldown_seconds=float(cooldown_seconds),
        )
        return bool(res.allowed), int(res.retry_after_seconds)

    # =========================================================
    # Restart-safe (epoch) cooldowns — ready for DB wiring
    # =========================================================

    @staticmethod
    def _now_epoch() -> int:
        return int(time.time())

    def check_epoch(
        self,
        *,
        action: str,
        user_id: str,
        location_id: str,
        cooldown_seconds: int,
        now_epoch: int | None = None,
    ) -> CooldownResult:
        """
        Epoch-based cooldown check.

        Unlike monotonic(), epoch timestamps are meaningful across restarts.
        This method is *API-compatible* with persistence: if you later back
        last_ts with SQLite, this logic stays the same.

        By default, it uses an in-memory epoch cache (still resets on restart),
        but the *timestamp semantics* are restart-safe.
        """
        now = int(now_epoch if now_epoch is not None else self._now_epoch())
        key = (action, user_id, location_id)
        last = self._last_epoch.get(key)

        if last is None:
            self._last_epoch[key] = now
            return CooldownResult(True, 0.0)

        elapsed = now - int(last)
        if elapsed >= int(cooldown_seconds):
            self._last_epoch[key] = now
            return CooldownResult(True, 0.0)

        return CooldownResult(False, float(int(cooldown_seconds) - elapsed))

    def try_acquire_epoch(
        self,
        action: str,
        user_id: int | str,
        location_id: int | str,
        cooldown_seconds: int,
        now_epoch: int | None = None,
    ) -> tuple[bool, int]:
        """
        Epoch-based acquire.

        Returns:
          (allowed, retry_after_seconds_int)

        This is the API we will use for restart-safe cooldowns once we back it
        with SQLite. For now it uses an in-memory epoch cache.
        """
        res = self.check_epoch(
            action=str(action),
            user_id=str(user_id),
            location_id=str(location_id),
            cooldown_seconds=int(cooldown_seconds),
            now_epoch=now_epoch,
        )
        return bool(res.allowed), int(res.retry_after_seconds)

    # =========================================================
    # Hooks for persistence (optional future)
    # =========================================================

    def seed_epoch(
        self,
        *,
        action: str,
        user_id: int | str,
        location_id: int | str,
        last_epoch: int,
    ) -> None:
        """
        Seed epoch cache (useful when loading from SQLite on startup).

        Example future flow:
          last = await cooldowns_repo.get_last_epoch(...)
          cooldowns.seed_epoch(..., last_epoch=last)
        """
        self._last_epoch[(str(action), str(user_id), str(location_id))] = int(last_epoch)

    def get_epoch(
        self,
        *,
        action: str,
        user_id: int | str,
        location_id: int | str,
    ) -> int | None:
        """
        Read the current cached epoch timestamp for a key (for debugging or persistence).
        """
        return self._last_epoch.get((str(action), str(user_id), str(location_id)))

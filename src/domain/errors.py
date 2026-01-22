from __future__ import annotations


class ArcadeBotError(Exception):
    """Base class for all domain/service errors."""


# -------------------------
# Generic / infrastructure
# -------------------------

class NotFound(ArcadeBotError):
    """Requested entity was not found."""


class ValidationError(ArcadeBotError):
    """Input or state failed validation."""


class ConflictError(ArcadeBotError):
    """Operation conflicts with current state (e.g., already exists)."""


# -------------------------
# Economy / beans
# -------------------------

class InsufficientBeans(ArcadeBotError):
    """User tried to spend more beans than they have."""


class EconomyDisabled(ArcadeBotError):
    """Economy feature is disabled by configuration or maintenance."""


# -------------------------
# Game / session errors
# -------------------------

class GameNotFound(ArcadeBotError):
    """Game key is unknown / not registered."""


class GameAlreadyActive(ArcadeBotError):
    """A game session is already active in the same location/thread."""


class GameNotActive(ArcadeBotError):
    """No active game session found for the location/thread."""


class CooldownActive(ArcadeBotError):
    """User is currently on cooldown for an action."""

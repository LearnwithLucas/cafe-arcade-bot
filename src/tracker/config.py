"""Tracker settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import math
import os
import re
import statistics


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else default


GUILD_ID = _int("TRACKER_GUILD_ID", 1450190803707367456)

CHANNELS = {
    "daily_brief": _int("TRACKER_CH_DAILY_BRIEF", 1554478790229626920),
    "weekly_report": _int("TRACKER_CH_WEEKLY_REPORT", _int("TRACKER_CH_DAILY_BRIEF", 1554478790229626920)),
    "alerts": _int("TRACKER_CH_ALERTS", 1554478809208590366),
    "yt_en": _int("TRACKER_CH_YT_EN", 1554478863155724298),
    "yt_nl": _int("TRACKER_CH_YT_NL", 1554478910803157072),
    "competitors": _int("TRACKER_CH_COMPETITORS", 1554478937017421884),
    "trends": _int("TRACKER_CH_TRENDS", 1554479008811323552),
    "video_ideas": _int("TRACKER_CH_VIDEO_IDEAS", 1554479032555540621),
    "tiktok": _int("TRACKER_CH_TIKTOK", 1554479104387055677),
    "instagram": _int("TRACKER_CH_INSTAGRAM", 1555110255455641651),
    "commands": _int("TRACKER_CH_COMMANDS", 1554479166840246373),
    "bot_status": _int("TRACKER_CH_BOT_STATUS", 1554479180249440256),
}

# Your own YouTube channels: key -> (handle, language)
OWN_YOUTUBE = {
    "en": (os.getenv("TRACKER_YT_EN", "@learnenglishlucas"), "en"),
    "nl": (os.getenv("TRACKER_YT_NL", "@learndutchlucas"), "nl"),
}

# Starting watchlist. Add or remove more with /watchlist in Discord.
DEFAULT_TIKTOK_COMPETITORS = {
    "en": ["antonioparlati", "iamthatenglishteacher", "speakenglishwithzach", "mrthomasenglish", "carolinakowanz",
           "english.with.lucy", "teacher.aliona", "englishteacherjason", "englishunderstood", "englishteachergrace"],
    "nl": ["learndutchwithyas", "estelledutch", "taalbureaueemland", "taaltrainingen", "learn.with.esther"],
}

TIMEZONE = os.getenv("TRACKER_TIMEZONE", "Europe/Amsterdam")
REPORT_HOUR = _int("TRACKER_REPORT_HOUR", 8)          # Monday report, local time
REPORT_DEADLINE_HOUR = _int("TRACKER_REPORT_DEADLINE_HOUR", 12)  # post without PC data after this hour

# Codes that mark a weekly-topic video (first letters of the code). W1, W2... by default.
TOPIC_PREFIXES = tuple(x.strip().upper() for x in os.getenv("TRACKER_TOPIC_CODES", "W").split(",") if x.strip())

# Rating needs this much history, else the result is "not enough data".
MIN_COMPARABLE = _int("TRACKER_MIN_COMPARABLE", 5)   # earlier videos of the same kind
MIN_EXPECTED = _int("TRACKER_MIN_EXPECTED", 5)       # their median subscribers or follows

CODE_RE = re.compile(r"^[ \t]*code[ \t]*[:=\-]?[ \t]*([A-Za-z]{1,3}[0-9]{1,3})[ \t]*$", re.I | re.M)


def parse_code(text: str | None) -> str | None:
    """The post's format code from a line like "code: D1" (the last one wins)."""
    found = CODE_RE.findall(text or "")
    return found[-1].upper() if found else None


def kind_of(is_short: int, code: str | None) -> str:
    """short, topic or long: videos are only compared with their own kind."""
    if is_short:
        return "short"
    if code and code.upper().startswith(TOPIC_PREFIXES):
        return "topic"
    return "long"


# Automatic codes for YouTube, used when there is no /tag and no "code:" line. Words match the title or a playlist name.
# Letters differ from the short-form codes (D duet, T mistake test, M mistake explainer, I invite, C carousel, Q story quote).
# Change them in Discord with /coderule. Anything that matches nothing gets S1 (Short) or G1 (other long video).
DEFAULT_CODE_RULES = [
    ("W1", "out loud|hardop|oefen|practi[cs]e \\d+ questions|\\d+ vragen"),   # weekly-topic practice episode
    ("P1", "langzaam nederlands|podcast|praten over"),                           # podcast
    ("F1", "mistake|fout|wrong|stop saying|niet of geen|fixed"),                  # one mistake fixed
    ("K1", "speak with confidence|confiden|freeze|nervous|fear|afraid"),         # confidence and fear
    ("L1", "level|niveau"),                                                      # level checks
]
FALLBACK_CODES = {1: "S1", 0: "G1"}

KIND_LABEL = {"short": "Short", "topic": "Weekly-topic video", "long": "Long video"}


def _poisson_cdf(k: int, lam: float) -> float:
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def rate(value: float | None, earlier: list[float]) -> tuple[str, str]:
    """Rate subscribers or follows against earlier posts of the same kind. Returns (rating, explanation)."""
    if value is None:
        return "not enough data", "no subscriber or follow numbers for this post"
    if len(earlier) < MIN_COMPARABLE:
        return "not enough data", f"only {len(earlier)} earlier posts of this kind to compare with (needs {MIN_COMPARABLE})"
    usual = statistics.median(earlier)
    if usual < MIN_EXPECTED:
        return "not enough data", f"the usual is {usual:g}, too few to tell a real difference from luck"
    ratio = value / usual
    name = "flop"
    for limit, label in BUCKETS:
        if ratio >= limit:
            name = label
            break
    # Outlier and flop must also be beyond normal luck (Poisson, 5%), else they count as over or under.
    if name == "outlier" and 1 - _poisson_cdf(int(value) - 1, usual) >= 0.05:
        name = "over"
    if name == "flop" and _poisson_cdf(int(value), usual) >= 0.05:
        name = "under"
    return name, f"{value:g} vs usual {usual:g} ({ratio:.2f}x, {len(earlier)} earlier posts)"


# Reach buckets: the same thresholds Grabber uses for TikTok.
BUCKETS = [(3.6, "outlier"), (1.6, "over"), (0.72, "par"), (0.5, "under"), (0.0, "flop")]


def bucket(ratio: float | None) -> str:
    if ratio is None:
        return "no baseline yet"
    for limit, name in BUCKETS:
        if ratio >= limit:
            return name
    return "flop"

"""Tracker settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else default


GUILD_ID = _int("TRACKER_GUILD_ID", 1450190803707367456)

CHANNELS = {
    "daily_brief": _int("TRACKER_CH_DAILY_BRIEF", 1554478790229626920),
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
DAILY_BRIEF_HOUR = _int("TRACKER_DAILY_BRIEF_HOUR", 8)

# Reach buckets: the same thresholds Grabber uses for TikTok.
BUCKETS = [(3.6, "outlier"), (1.6, "over"), (0.72, "par"), (0.5, "under"), (0.0, "flop")]


def bucket(ratio: float | None) -> str:
    if ratio is None:
        return "no baseline yet"
    for limit, name in BUCKETS:
        if ratio >= limit:
            return name
    return "flop"

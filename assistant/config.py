"""Configuration loaded from the environment (and `.env`), validated at import.

The bot fails closed: if a required value is missing or malformed, importing
this module raises ConfigError and the bot refuses to start.
"""

from __future__ import annotations

import datetime
import os
import re
import zoneinfo

from dotenv import load_dotenv

from .paths import ENV_FILE

# Tests set ASSISTANT_SKIP_DOTENV=1 so a developer's real .env is never read.
if os.getenv("ASSISTANT_SKIP_DOTENV") != "1":
    load_dotenv(ENV_FILE)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


_errors: list[str] = []


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        _errors.append(f"{name} must be an integer (got {raw!r}).")
        return default
    if not minimum <= value <= maximum:
        _errors.append(f"{name} must be between {minimum} and {maximum} (got {value}).")
        return default
    return value


# --- Required secrets ---------------------------------------------------------

TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", TELEGRAM_BOT_TOKEN):
    _errors.append("TELEGRAM_BOT_TOKEN is missing or not a valid BotFather token.")

GROQ_API_KEY = _get("GROQ_API_KEY")
if not GROQ_API_KEY or "your_" in GROQ_API_KEY:
    _errors.append("GROQ_API_KEY is missing or still the placeholder value.")

# --- Access control (required: there is no "allow everyone" mode) ------------

_raw_user_id = _get("TELEGRAM_ALLOWED_USER_ID")
ALLOWED_USER_ID: int = 0
if not _raw_user_id:
    _errors.append("TELEGRAM_ALLOWED_USER_ID is required (your numeric Telegram user ID).")
else:
    try:
        ALLOWED_USER_ID = int(_raw_user_id)
        if ALLOWED_USER_ID <= 0:
            raise ValueError
    except ValueError:
        _errors.append("TELEGRAM_ALLOWED_USER_ID must be a positive integer Telegram user ID.")

# --- Model, timezone and schedule ---------------------------------------------

GROQ_MODEL = _get("GROQ_MODEL", "openai/gpt-oss-120b")

TIMEZONE = _get("TIMEZONE", "Asia/Dubai")
try:
    TZ = zoneinfo.ZoneInfo(TIMEZONE)
except (zoneinfo.ZoneInfoNotFoundError, ValueError):
    _errors.append(f"TIMEZONE {TIMEZONE!r} is not a valid IANA timezone (e.g. Asia/Dubai).")
    TZ = zoneinfo.ZoneInfo("UTC")

BRIEFING_TIME = _get("BRIEFING_TIME", "08:00")
_match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", BRIEFING_TIME)
if _match:
    BRIEFING_HOUR, BRIEFING_MINUTE = int(_match.group(1)), int(_match.group(2))
else:
    _errors.append(f"BRIEFING_TIME must be HH:MM in 24-hour time (got {BRIEFING_TIME!r}).")
    BRIEFING_HOUR, BRIEFING_MINUTE = 8, 0

# --- Operational limits -------------------------------------------------------

GOG_TIMEOUT_SECONDS = _get_int("GOG_TIMEOUT_SECONDS", 60, 5, 600)
LLM_TIMEOUT_SECONDS = _get_int("LLM_TIMEOUT_SECONDS", 30, 5, 300)
ACTION_TTL_MINUTES = _get_int("ACTION_TTL_MINUTES", 15, 1, 1440)

if _errors:
    raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(_errors))


def now() -> datetime.datetime:
    """Current time as an aware datetime in the configured timezone."""
    return datetime.datetime.now(TZ)

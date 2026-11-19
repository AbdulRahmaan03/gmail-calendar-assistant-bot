"""Filesystem locations used by the bot."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Files the bot writes while running (log, PID, lock). Git-ignored.
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LOG_FILE = RUNTIME_DIR / "bot.log"
PID_FILE = RUNTIME_DIR / "bot.pid"
LOCK_FILE = RUNTIME_DIR / "bot.lock"


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)

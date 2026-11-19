import logging
import os
import subprocess
import sys

from assistant import logging_setup

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_config(**overrides):
    env = {k: v for k, v in os.environ.items()}
    env.update(overrides)
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run([sys.executable, "-c", "import assistant.config"], cwd=PROJECT, env=env,
                          capture_output=True, text=True, check=False)


def test_config_valid_imports():
    assert _import_config().returncode == 0


def test_config_fails_closed_without_allowed_user():
    env = dict(os.environ)
    env.pop("TELEGRAM_ALLOWED_USER_ID")
    proc = subprocess.run([sys.executable, "-c", "import assistant.config"], cwd=PROJECT, env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert "TELEGRAM_ALLOWED_USER_ID is required" in proc.stderr


def test_config_rejects_bad_values():
    proc = _import_config(TIMEZONE="Mars/Base", BRIEFING_TIME="25:99", TELEGRAM_ALLOWED_USER_ID="abc")
    assert proc.returncode != 0
    for text in ("TIMEZONE", "BRIEFING_TIME", "positive integer"):
        assert text in proc.stderr


def test_malformed_log_record_does_not_raise_or_leak():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s %s", ("bot123456789:" + "y" * 35,), None)
    assert logging_setup.RedactSecretsFilter().filter(record) is True
    assert "y" * 35 not in str(record.args)


def test_log_redaction_hides_bot_token_and_keys():
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               "POST https://api.telegram.org/bot%s/getUpdates key=%s",
                               ("123456789:" + "x" * 35, "gsk_" + "a" * 30), None)
    logging_setup.RedactSecretsFilter().filter(record)
    message = record.getMessage()
    assert "x" * 35 not in message and "a" * 30 not in message
    assert "bot<redacted>" in message and "gsk_<redacted>" in message

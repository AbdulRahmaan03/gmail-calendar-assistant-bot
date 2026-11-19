"""Google Calendar and Gmail access through the `gog` CLI.

Safety measures applied to every call:
  * no shell; arguments are passed as a list
  * `--enable-commands=<exact command>` so gog itself refuses anything else
  * `--readonly` on every read
  * `--` before positional values so they can never be parsed as flags
  * a timeout, so a hung gog process cannot freeze the bot
  * `--wrap-untrusted` on reads, so email and invite text arrives marked as
    third-party content for the LLM
"""

from __future__ import annotations

import datetime
import email.utils
import json
import logging
import os
import re
import secrets
import subprocess
import urllib.parse
from typing import Any

from . import config

log = logging.getLogger(__name__)

MAX_EMAIL_RESULTS = 20
MAX_CALENDAR_DAYS = 31
DELETE_SEARCH_DAYS = 60
MAX_SUBJECT_LEN = 250
MAX_BODY_LEN = 10_000
MAX_EVENT_TEXT_LEN = 2_000
EMAIL_PREVIEW_CHARS = 1_500

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,1023}")  # must not start with "-"
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_UNTRUSTED_MARKER_RE = re.compile(r'\s*<<<(?:END_)?EXTERNAL_UNTRUSTED_CONTENT id="[^"]*">>>\s*')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GogError(RuntimeError):
    """A gog command failed, timed out, or returned something unusable."""


# ==================== VALIDATION & SANITISING ====================


def validate_id(value: Any, what: str = "ID") -> str:
    value = str(value or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {what}: {value[:40]!r}")
    return value


def validate_email_address(value: Any) -> str:
    """Accept exactly one plain email address (no display name, no lists)."""
    value = str(value or "").strip()
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError(f"Invalid recipient email address: {value[:80]!r}")
    return value


def validate_subject(value: Any) -> str:
    value = " ".join(str(value or "").split())  # no newlines: prevents header tricks
    if not value:
        raise ValueError("Email subject must not be empty.")
    if len(value) > MAX_SUBJECT_LEN:
        raise ValueError(f"Email subject is longer than {MAX_SUBJECT_LEN} characters.")
    return value


def validate_body(value: Any) -> str:
    value = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if not value:
        raise ValueError("Message body must not be empty.")
    if len(value) > MAX_BODY_LEN:
        raise ValueError(f"Message body is longer than {MAX_BODY_LEN} characters.")
    return value


def _clean_text(value: Any, limit: int) -> str:
    value = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if len(value) > limit:
        raise ValueError(f"Text is longer than {limit} characters.")
    return value


def parse_event_time(value: Any) -> datetime.datetime:
    """Parse an RFC3339/ISO time. Naive times are taken as the configured timezone."""
    raw = str(value or "").strip()
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid date/time {raw[:40]!r}; use e.g. 2026-11-18T19:00:00+04:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.TZ)
    return parsed


def clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def unwrap_untrusted(text: Any) -> str:
    """Remove gog's untrusted-content markers (for showing text to the user)."""
    return _UNTRUSTED_MARKER_RE.sub(" ", str(text or "")).strip()


def wrap_untrusted(text: str) -> str:
    """Mark text as third-party content, in the same format gog uses."""
    marker_id = secrets.token_hex(8)
    return (
        f'<<<EXTERNAL_UNTRUSTED_CONTENT id="{marker_id}">>>\n{text}\n'
        f'<<<END_EXTERNAL_UNTRUSTED_CONTENT id="{marker_id}">>>'
    )


def _markdown_safe(text: str) -> str:
    """Strip characters that would let a sender inject Telegram Markdown/links."""
    text = re.sub(r"[\[\]()*_`\\<>]", "", text)
    return " ".join(text.split())


def parse_sender(raw_from: Any) -> dict[str, str]:
    """Return a sanitised sender name, validated address and a Gmail compose link."""
    name, addr = email.utils.parseaddr(unwrap_untrusted(raw_from))
    try:
        addr = validate_email_address(addr)
    except ValueError:
        addr = ""
    name = _markdown_safe(name.strip().strip("\"'"))[:80]
    if not name:
        name = addr.split("@")[0] if addr else "Unknown"
        name = _markdown_safe(name) or "Unknown"
    if addr:
        url = "https://mail.google.com/mail/?view=cm&fs=1&to=" + urllib.parse.quote(addr, safe="@")
        formatted = f"[{name}]({url})"
    else:
        formatted = name
    return {"name": name, "email": addr, "formatted": formatted}


def _clean_html_text(text: str) -> str:
    """Strip HTML tags, styles and scripts; collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ==================== GOG RUNNER ====================


def _run_gog(command: list[str], flags: list[str] | None = None, positional: list[str] | None = None,
             *, readonly: bool) -> dict[str, Any]:
    args = ["gog", *command, "--json", "--no-input", f"--enable-commands={'.'.join(command)}"]
    if readonly:
        args.append("--readonly")
    args.extend(flags or [])
    if positional:
        args.append("--")
        args.extend(positional)

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=config.GOG_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GogError("The gog CLI is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GogError(f"gog {' '.join(command)} timed out after {config.GOG_TIMEOUT_SECONDS}s.") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[0][:300] if detail else f"exit code {proc.returncode}"
        log.warning("gog %s failed (exit %s): %s", " ".join(command), proc.returncode, message)
        raise GogError(f"gog {' '.join(command)} failed: {message}")

    output = proc.stdout.strip()
    if not output:
        return {}
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise GogError(f"gog {' '.join(command)} returned non-JSON output.") from exc
    return data if isinstance(data, dict) else {"data": data}


# ==================== CALENDAR ====================


def _event_time(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return value.get("dateTime") or value.get("date") or ""


def _normalise_event(e: dict[str, Any]) -> dict[str, Any]:
    original = e.get("originalStartTime")
    return {
        "id": e.get("id"),
        "summary": e.get("summary") or "No Title",
        "start": _event_time(e.get("start")),
        "end": _event_time(e.get("end")),
        "location": e.get("location") or "",
        "description": str(e.get("description") or "")[:500],
        "recurring_event_id": e.get("recurringEventId") or "",
        "original_start": _event_time(original) if original else "",
    }


def list_calendar_events(days: Any = 0, query: Any = None) -> list[dict[str, Any]]:
    """Events from the start of today through `days` days ahead (0 = today only)."""
    days = clamp(days, 0, MAX_CALENDAR_DAYS, 0)
    start = config.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=days + 1)
    flags = [
        f"--from={start.isoformat()}",
        f"--to={end.isoformat()}",
        "--max=250",
        "--wrap-untrusted",
    ]
    query = str(query or "").strip()
    if query:
        flags.append(f"--query={query[:200]}")
    data = _run_gog(["calendar", "events"], flags, ["primary"], readonly=True)
    return [_normalise_event(e) for e in data.get("events") or [] if isinstance(e, dict)]


def find_events_for_deletion(identifier: Any) -> list[dict[str, Any]]:
    """Upcoming events whose ID equals, or whose title contains, `identifier`."""
    needle = str(identifier or "").strip()
    if not needle:
        raise ValueError("Tell me which event to delete.")
    events = list_calendar_events(days=DELETE_SEARCH_DAYS)
    exact = [e for e in events if e["id"] == needle]
    if exact:
        return exact
    lowered = needle.lower()
    return [e for e in events if lowered in unwrap_untrusted(e["summary"]).lower()]


def create_calendar_event(summary: Any, start_iso: Any, end_iso: Any, description: Any = "",
                          location: Any = "") -> dict[str, Any]:
    summary = " ".join(_clean_text(summary, 250).split())
    if not summary:
        raise ValueError("Event title must not be empty.")
    start, end = parse_event_time(start_iso), parse_event_time(end_iso)
    if end <= start:
        raise ValueError("Event end time must be after its start time.")
    if end - start > datetime.timedelta(days=14):
        raise ValueError("Events longer than 14 days are not supported.")

    flags = [f"--summary={summary}", f"--from={start.isoformat()}", f"--to={end.isoformat()}"]
    description = _clean_text(description, MAX_EVENT_TEXT_LEN)
    location = _clean_text(location, 500)
    if description:
        flags.append(f"--description={description}")
    if location:
        flags.append(f"--location={location}")

    data = _run_gog(["calendar", "create"], flags, ["primary"], readonly=False)
    event = data.get("event") or {}
    return {
        "event_id": event.get("id"),
        "summary": event.get("summary") or summary,
        "start": _event_time(event.get("start")) or start.isoformat(),
        "end": _event_time(event.get("end")) or end.isoformat(),
    }


def delete_calendar_event(event_id: Any, recurring_event_id: str = "", original_start: str = "") -> dict[str, Any]:
    """Delete one event. For an instance of a recurring series, only that occurrence."""
    event_id = validate_id(event_id, "event ID")
    flags = ["--force"]  # gog's own prompt is replaced by the Telegram confirmation
    if recurring_event_id and original_start:
        flags += ["--scope=single", f"--original-start={original_start}"]
    _run_gog(["calendar", "delete"], flags, ["primary", event_id], readonly=False)
    return {"event_id": event_id}


# ==================== GMAIL ====================


def search_emails(query: Any = "is:unread", limit: Any = 5) -> list[dict[str, Any]]:
    query = str(query or "is:unread").strip()[:500]
    limit = clamp(limit, 1, MAX_EMAIL_RESULTS, 5)
    data = _run_gog(["gmail", "search"], [f"--max={limit}", "--wrap-untrusted"], [query], readonly=True)
    results = []
    for t in data.get("threads") or []:
        if not isinstance(t, dict):
            continue
        sender = parse_sender(t.get("from"))
        results.append({
            "id": t.get("id"),
            "from": sender["formatted"],
            "sender_name": sender["name"],
            "sender_email": sender["email"],
            "subject": t.get("subject") or "(No Subject)",
            "date": t.get("date") or "",
            "labels": t.get("labels") or [],
        })
    return results


def get_email_details(message_id: Any) -> dict[str, Any]:
    message_id = validate_id(message_id, "message ID")
    data = _run_gog(["gmail", "get"], ["--wrap-untrusted"], [message_id], readonly=True)

    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    sender = parse_sender(headers.get("from"))

    body = _clean_html_text(unwrap_untrusted(data.get("body")))[:EMAIL_PREVIEW_CHARS]
    snippet = message.get("snippet") or ""

    return {
        "id": message_id,
        "from": sender["formatted"],
        "sender_name": sender["name"],
        "sender_email": sender["email"],
        "to": headers.get("to") or "",
        "subject": headers.get("subject") or "",
        "date": headers.get("date") or "",
        "snippet": snippet,
        "content_preview": wrap_untrusted(body) if body else snippet,
    }


def send_email(to: Any, subject: Any, body: Any) -> dict[str, Any]:
    to, subject, body = validate_email_address(to), validate_subject(subject), validate_body(body)
    _run_gog(["gmail", "send"], [f"--to={to}", f"--subject={subject}", f"--body={body}"], readonly=False)
    return {"to": to}


def reply_email(message_id: Any, body: Any) -> dict[str, Any]:
    message_id, body = validate_id(message_id, "message ID"), validate_body(body)
    _run_gog(["gmail", "reply"], [f"--body={body}"], [message_id], readonly=False)
    return {"message_id": message_id}

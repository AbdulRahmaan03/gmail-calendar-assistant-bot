"""Google Calendar and Gmail access through the `gog` CLI.

Safety measures applied to every call:
  * no shell; arguments are passed as a list
  * `--enable-commands=<exact command>` so gog itself refuses anything else
  * `--readonly` on every read
  * `--` before positional values so they can never be parsed as flags
  * email bodies go through stdin, not the command line
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
MAX_REPLY_SUBJECT_LEN = 900  # "Re: <original>" can be long; it must not be cut or threading breaks
MAX_BODY_LEN = 3_000  # keeps the full text visible in one Telegram confirmation message
MAX_REPLY_RECIPIENTS = 5
MAX_EVENT_TITLE_LEN = 250
MAX_EVENT_TEXT_LEN = 1_500
MAX_EVENT_LOCATION_LEN = 300
MAX_EVENT_DURATION = datetime.timedelta(days=14)
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


def validate_subject(value: Any, max_len: int = MAX_SUBJECT_LEN) -> str:
    value = " ".join(str(value or "").split())  # no newlines: prevents header tricks
    if not value:
        raise ValueError("Email subject must not be empty.")
    if len(value) > max_len:
        raise ValueError(f"Email subject is longer than {max_len} characters.")
    return value


def validate_body(value: Any) -> str:
    value = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if not value:
        raise ValueError("Message body must not be empty.")
    if len(value) > MAX_BODY_LEN:
        raise ValueError(f"Message body is {len(value)} characters; the limit is {MAX_BODY_LEN}. Shorten it.")
    return value


def _clean_text(value: Any, limit: int, what: str) -> str:
    value = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if len(value) > limit:
        raise ValueError(f"{what} is longer than {limit} characters.")
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


def validate_event(summary: Any, start_iso: Any, end_iso: Any, description: Any = "",
                   location: Any = "") -> dict[str, str]:
    """Validate and normalise event fields. Used both for the preview and for creation,
    so what the user confirms is exactly what gets created."""
    summary = " ".join(_clean_text(summary, MAX_EVENT_TITLE_LEN, "Event title").split())
    if not summary:
        raise ValueError("Event title must not be empty.")
    start, end = parse_event_time(start_iso), parse_event_time(end_iso)
    if end <= start:
        raise ValueError("Event end time must be after its start time.")
    if end - start > MAX_EVENT_DURATION:
        raise ValueError("Events longer than 14 days are not supported.")
    return {
        "summary": summary,
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "description": _clean_text(description, MAX_EVENT_TEXT_LEN, "Event notes"),
        "location": " ".join(_clean_text(location, MAX_EVENT_LOCATION_LEN, "Event location").split()),
    }


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


def _addresses(header: Any) -> list[str]:
    """Valid, de-duplicated addresses from an address-list header."""
    found: list[str] = []
    for _name, addr in email.utils.getaddresses([unwrap_untrusted(header)]):
        try:
            addr = validate_email_address(addr)
        except ValueError:
            continue
        if addr.lower() not in (a.lower() for a in found):
            found.append(addr)
    return found


def _clean_html_text(text: str) -> str:
    """Strip HTML tags, styles and scripts; collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ==================== GOG RUNNER ====================


def _run_gog(command: list[str], flags: list[str] | None = None, positional: list[str] | None = None,
             *, readonly: bool, stdin_text: str | None = None) -> dict[str, Any]:
    args = ["gog", *command, "--json", "--no-input", f"--enable-commands={'.'.join(command)}"]
    if readonly:
        args.append("--readonly")
    args.extend(flags or [])
    if positional:
        args.append("--")
        args.extend(positional)

    # Bytes in and out: no newline translation of bodies, explicit UTF-8.
    io_kwargs: dict[str, Any] = (
        {"input": stdin_text.encode("utf-8")} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    )
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=config.GOG_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            check=False,
            **io_kwargs,
        )
    except FileNotFoundError as exc:
        raise GogError("The gog CLI is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GogError(f"gog {' '.join(command)} timed out after {config.GOG_TIMEOUT_SECONDS}s.") from exc

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = (stderr or stdout).splitlines()
        message = detail[0][:300] if detail else f"exit code {proc.returncode}"
        log.warning("gog %s failed (exit %s): %s", " ".join(command), proc.returncode, message)
        raise GogError(f"gog {' '.join(command)} failed: {message}")

    if not stdout:
        return {}
    try:
        data = json.loads(stdout)
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
    attendees = e.get("attendees") if isinstance(e.get("attendees"), list) else []
    guests = [a for a in attendees if isinstance(a, dict) and not a.get("self") and not a.get("resource")]
    return {
        "id": e.get("id"),
        "summary": e.get("summary") or "No Title",
        "start": _event_time(e.get("start")),
        "end": _event_time(e.get("end")),
        "location": e.get("location") or "",
        "description": str(e.get("description") or "")[:500],
        "recurring_event_id": e.get("recurringEventId") or "",
        "original_start": _event_time(original) if original else "",
        "guest_count": len(guests),
    }


def _fetch_events(days: int, query: Any = None) -> list[dict[str, Any]]:
    """Events from the start of today (local time) through `days` days ahead.
    gog expands recurring events into individual occurrences."""
    start = config.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=days + 1)
    flags = [f"--from={start.isoformat()}", f"--to={end.isoformat()}", "--max=250", "--wrap-untrusted"]
    query = str(query or "").strip()
    if query:
        flags.append(f"--query={query[:200]}")
    data = _run_gog(["calendar", "events"], flags, ["primary"], readonly=True)
    return [_normalise_event(e) for e in data.get("events") or [] if isinstance(e, dict)]


def list_calendar_events(days: Any = 0, query: Any = None) -> list[dict[str, Any]]:
    """Events from today through `days` days ahead (0 = today only, max 31)."""
    return _fetch_events(clamp(days, 0, MAX_CALENDAR_DAYS, 0), query)


def find_events_for_deletion(identifier: Any, on_date: Any = None) -> list[dict[str, Any]]:
    """Events in the next 60 days whose ID equals, or whose title contains, `identifier`,
    optionally limited to events starting on `on_date` (YYYY-MM-DD, local time)."""
    needle = str(identifier or "").strip()
    if not needle:
        raise ValueError("Tell me which event to delete.")
    day: datetime.date | None = None
    if on_date:
        try:
            day = datetime.date.fromisoformat(str(on_date).strip()[:10])
        except ValueError as exc:
            raise ValueError(f"Invalid date {str(on_date)[:20]!r}; use YYYY-MM-DD.") from exc

    events = _fetch_events(DELETE_SEARCH_DAYS)
    matches = [e for e in events if e["id"] == needle]
    if not matches:
        lowered = needle.lower()
        matches = [e for e in events if lowered in unwrap_untrusted(e["summary"]).lower()]
    if day is not None:
        matches = [e for e in matches if _local_date(e["start"]) == day]
    return matches


def _local_date(value: str) -> datetime.date | None:
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(config.TZ)
    return parsed.date()


def create_calendar_event(summary: Any, start_iso: Any, end_iso: Any, description: Any = "",
                          location: Any = "") -> dict[str, Any]:
    p = validate_event(summary, start_iso, end_iso, description, location)
    flags = [f"--summary={p['summary']}", f"--from={p['start_iso']}", f"--to={p['end_iso']}"]
    if p["description"]:
        flags.append(f"--description={p['description']}")
    if p["location"]:
        flags.append(f"--location={p['location']}")
    data = _run_gog(["calendar", "create"], flags, ["primary"], readonly=False)
    event = data.get("event") or {}
    return {"event_id": event.get("id"), "summary": event.get("summary") or p["summary"]}


def delete_calendar_event(event_id: Any, recurring_event_id: str = "", original_start: str = "") -> dict[str, Any]:
    """Delete one event. For an occurrence of a recurring series, only that occurrence."""
    event_id = validate_id(event_id, "event ID")
    flags = ["--force"]  # gog's own prompt is replaced by the Telegram confirmation
    if recurring_event_id and original_start:
        flags += ["--scope=single", f"--original-start={original_start}"]
    _run_gog(["calendar", "delete"], flags, ["primary", event_id], readonly=False)
    return {"event_id": event_id}


# ==================== GMAIL ====================


def search_emails(query: Any = "is:unread", limit: Any = 5) -> list[dict[str, Any]]:
    """Search individual messages (not threads), so every ID is a real message ID."""
    query = str(query or "is:unread").strip()[:500]
    limit = clamp(limit, 1, MAX_EMAIL_RESULTS, 5)
    data = _run_gog(["gmail", "messages", "search"], [f"--max={limit}", "--wrap-untrusted"], [query],
                    readonly=True)
    results = []
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        sender = parse_sender(m.get("from"))
        results.append({
            "id": m.get("id"),
            "thread_id": m.get("threadId"),
            "from": sender["formatted"],
            "sender_name": sender["name"],
            "sender_email": sender["email"],
            "subject": m.get("subject") or "(No Subject)",
            "date": m.get("date") or "",
            "labels": m.get("labels") or [],
        })
    return results


def _get_message(message_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    data = _run_gog(["gmail", "get"], ["--wrap-untrusted"], [message_id], readonly=True)
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    return data, headers, message


def get_email_details(message_id: Any) -> dict[str, Any]:
    message_id = validate_id(message_id, "message ID")
    data, headers, message = _get_message(message_id)
    sender = parse_sender(headers.get("from"))
    body = _clean_html_text(unwrap_untrusted(data.get("body")))[:EMAIL_PREVIEW_CHARS]
    snippet = message.get("snippet") or ""
    return {
        "id": message_id,
        "thread_id": message.get("threadId"),
        "from": sender["formatted"],
        "sender_name": sender["name"],
        "sender_email": sender["email"],
        "to": headers.get("to") or "",
        "subject": headers.get("subject") or "",
        "date": headers.get("date") or "",
        "snippet": snippet,
        "content_preview": wrap_untrusted(body) if body else snippet,
    }


def get_reply_context(message_id: Any) -> dict[str, Any]:
    """Work out exactly who a reply goes to, using the standard rules (the same ones gog uses):
    the Reply-To address if the email sets one, otherwise the sender; for your own sent
    messages, the original recipients."""
    message_id = validate_id(message_id, "message ID")
    _data, headers, message = _get_message(message_id)
    labels = message.get("labelIds") if isinstance(message.get("labelIds"), list) else []
    from_addrs = _addresses(headers.get("from"))
    reply_to_addrs = _addresses(headers.get("reply_to"))
    sent_by_me = "SENT" in labels

    recipients = _addresses(headers.get("to")) if sent_by_me else (reply_to_addrs or from_addrs)
    if not recipients:
        raise ValueError("Couldn't find a valid address to reply to in that email.")
    if len(recipients) > MAX_REPLY_RECIPIENTS:
        raise ValueError(f"That email would be replied to {len(recipients)} addresses; the limit is "
                         f"{MAX_REPLY_RECIPIENTS}. Reply from Gmail instead.")

    subject = " ".join(unwrap_untrusted(headers.get("subject")).split())
    reply_subject = subject if subject.lower().startswith("re:") else (f"Re: {subject}" if subject else "Re:")
    lowered = {a.lower() for a in from_addrs}
    return {
        "message_id": message_id,
        "recipients": recipients,
        "subject": reply_subject[:MAX_REPLY_SUBJECT_LEN],
        "from": from_addrs[0] if from_addrs else "",
        "reply_to_differs": bool(reply_to_addrs) and not sent_by_me
        and {a.lower() for a in reply_to_addrs} != lowered,
        "sent_by_me": sent_by_me,
    }


def send_email(to: Any, subject: Any, body: Any) -> dict[str, Any]:
    to, subject, body = validate_email_address(to), validate_subject(subject), validate_body(body)
    _run_gog(["gmail", "send"], [f"--to={to}", f"--subject={subject}", "--body-file=-"],
             readonly=False, stdin_text=body)
    return {"to": to}


def send_reply(message_id: Any, recipients: list[str], subject: Any, body: Any) -> dict[str, Any]:
    """Reply in the original thread to exactly `recipients` (as shown in the confirmation)."""
    message_id = validate_id(message_id, "message ID")
    recipients = [validate_email_address(r) for r in recipients]
    if not 1 <= len(recipients) <= MAX_REPLY_RECIPIENTS:
        raise ValueError("A reply needs between 1 and 5 recipients.")
    subject = validate_subject(subject, MAX_REPLY_SUBJECT_LEN)
    body = validate_body(body)
    _run_gog(
        ["gmail", "send"],
        [f"--to={','.join(recipients)}", f"--subject={subject}", f"--reply-to-message-id={message_id}",
         "--body-file=-"],
        readonly=False,
        stdin_text=body,
    )
    return {"message_id": message_id, "to": recipients}

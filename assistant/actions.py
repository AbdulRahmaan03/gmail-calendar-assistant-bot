"""Confirmation gate for every action that changes something.

The LLM can only *propose* an action (send, reply, create, delete). A proposal
is validated, stored here, and shown to the user with a preview built from the
stored parameters -- never from the model's own text. Nothing runs until the
same user taps Confirm, and a proposal can be confirmed at most once.

Previews are never truncated: if one would not fit in a Telegram message, the
proposal is rejected and the model is asked to shorten it. What the user reads
is exactly what gets executed.
"""

from __future__ import annotations

import datetime
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config
from . import google_service as gs

EMAIL_SEND = "email_send"
EMAIL_REPLY = "email_reply"
CALENDAR_CREATE = "calendar_create"
CALENDAR_DELETE = "calendar_delete"

# Telegram allows 4096 characters; leave room for the footer / status line.
PREVIEW_LIMIT = 3_800

_BUTTON_LABELS = {
    EMAIL_SEND: "✅ Send email",
    EMAIL_REPLY: "✅ Send reply",
    CALENDAR_CREATE: "✅ Create event",
    CALENDAR_DELETE: "🗑 Delete event",
}


@dataclass
class PendingAction:
    id: str
    user_id: int
    kind: str
    params: dict[str, Any]
    preview: str
    created_at: float = field(default_factory=time.monotonic)

    @property
    def confirm_label(self) -> str:
        return _BUTTON_LABELS[self.kind]

    def expired(self) -> bool:
        return time.monotonic() - self.created_at > config.ACTION_TTL_MINUTES * 60


_lock = threading.Lock()
_pending: dict[str, PendingAction] = {}


def _purge_expired_locked() -> None:
    for action_id in [k for k, a in _pending.items() if a.expired()]:
        del _pending[action_id]


def _store(user_id: int, kind: str, params: dict[str, Any], preview: str) -> PendingAction:
    if len(preview) > PREVIEW_LIMIT:
        raise ValueError(f"The confirmation preview would be {len(preview)} characters, more than Telegram can "
                         f"show in one message ({PREVIEW_LIMIT}). Shorten the text and try again.")
    action = PendingAction(id=secrets.token_urlsafe(9), user_id=user_id, kind=kind, params=params, preview=preview)
    with _lock:
        _purge_expired_locked()
        _pending[action.id] = action
    return action


def take(action_id: str, user_id: int) -> PendingAction | None:
    """Remove and return the action if it exists, is unexpired and belongs to user_id."""
    with _lock:
        _purge_expired_locked()
        action = _pending.get(action_id)
        if action is None or action.user_id != user_id:
            return None
        return _pending.pop(action_id)


def _fmt_time(value: str) -> str:
    if len(value) == 10:  # all-day events carry a date only
        try:
            return datetime.date.fromisoformat(value).strftime("%a %d %b %Y (all day)")
        except ValueError:
            return value
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return value or "?"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(config.TZ)
    return parsed.strftime("%a %d %b %Y, %H:%M")


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ==================== PROPOSALS (validated, nothing executed) ====================


def propose_email_send(user_id: int, to: Any, subject: Any, body: Any) -> PendingAction:
    params = {
        "to": gs.validate_email_address(to),
        "subject": gs.validate_subject(subject),
        "body": gs.validate_body(body),
    }
    preview = (
        "📧 Send this email?\n\n"
        f"To: {params['to']}\n"
        f"Subject: {params['subject']}\n"
        "──────────\n"
        f"{params['body']}"
    )
    return _store(user_id, EMAIL_SEND, params, preview)


def propose_email_reply(user_id: int, message_id: Any, body: Any) -> PendingAction:
    body = gs.validate_body(body)
    # Resolve the real recipients now and pin them: the reply is sent to exactly these.
    ctx = gs.get_reply_context(message_id)
    params = {
        "message_id": ctx["message_id"],
        "recipients": ctx["recipients"],
        "subject": ctx["subject"],
        "body": body,
    }
    lines = [
        "↩️ Send this reply?\n",
        f"To: {', '.join(ctx['recipients'])}",
        f"Subject: {_shorten(ctx['subject'], 200)}",
    ]
    if ctx["reply_to_differs"]:
        lines.append(f"⚠️ This email was sent from {ctx['from'] or 'another address'} but asks for replies to go "
                     "to the address above. Make sure you trust it.")
    if ctx["sent_by_me"]:
        lines.append("(This is your own message, so the reply goes to its original recipients.)")
    lines += ["──────────", body]
    return _store(user_id, EMAIL_REPLY, params, "\n".join(lines))


def propose_calendar_create(user_id: int, summary: Any, start_iso: Any, end_iso: Any,
                            description: Any = "", location: Any = "") -> PendingAction:
    params = gs.validate_event(summary, start_iso, end_iso, description, location)
    lines = [
        "📅 Create this event?\n",
        f"Title: {params['summary']}",
        f"Starts: {_fmt_time(params['start_iso'])}",
        f"Ends: {_fmt_time(params['end_iso'])}",
    ]
    if params["location"]:
        lines.append(f"Location: {params['location']}")
    if params["description"]:
        lines.append(f"Notes: {params['description']}")
    return _store(user_id, CALENDAR_CREATE, params, "\n".join(lines))


def propose_calendar_delete(user_id: int, event: dict[str, Any]) -> PendingAction:
    params = {
        "event_id": gs.validate_id(event.get("id"), "event ID"),
        "recurring_event_id": event.get("recurring_event_id") or "",
        "original_start": event.get("original_start") or "",
    }
    title = _shorten(gs.unwrap_untrusted(event.get("summary")) or "No Title", 250)
    lines = [
        "🗑 Delete this event?\n",
        f"Title: {title}",
        f"Starts: {_fmt_time(event.get('start') or '')}",
        f"Ends: {_fmt_time(event.get('end') or '')}",
    ]
    if params["recurring_event_id"]:
        lines.append("(Only this occurrence of the recurring event will be deleted.)")
    guests = int(event.get("guest_count") or 0)
    if guests:
        lines.append(f"This event has {guests} other guest(s). They will not be notified.")
    return _store(user_id, CALENDAR_DELETE, params, "\n".join(lines))


# ==================== EXECUTION (only after the user confirms) ====================


def execute(action: PendingAction) -> str:
    """Run a confirmed action. Raises GogError / ValueError on failure."""
    p = action.params
    if action.kind == EMAIL_SEND:
        gs.send_email(p["to"], p["subject"], p["body"])
        return f"Email sent to {p['to']}."
    if action.kind == EMAIL_REPLY:
        gs.send_reply(p["message_id"], p["recipients"], p["subject"], p["body"])
        return f"Reply sent to {', '.join(p['recipients'])}."
    if action.kind == CALENDAR_CREATE:
        gs.create_calendar_event(p["summary"], p["start_iso"], p["end_iso"], p["description"], p["location"])
        return f"Event created: {p['summary']}."
    if action.kind == CALENDAR_DELETE:
        gs.delete_calendar_event(p["event_id"], p["recurring_event_id"], p["original_start"])
        return "Event deleted."
    raise ValueError(f"Unknown action kind: {action.kind}")

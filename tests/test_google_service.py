import datetime
import json
import subprocess

import pytest

from assistant import config
from assistant import google_service as gs


class FakeProc:
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout.encode(), returncode, stderr.encode()


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProc(json.dumps({"messages": [], "events": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _message_payload(headers, labels=("INBOX",)):
    return {"headers": headers, "message": {"labelIds": list(labels), "threadId": "t1"}, "body": ""}


def _serve(monkeypatch, payload):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(json.dumps(payload)))


# ---------- command construction ----------

def test_reads_are_least_privilege_and_flag_safe(captured):
    gs.search_emails("-in:spam --help", limit=999)
    args, kwargs = captured[0]
    assert args[:4] == ["gog", "gmail", "messages", "search"]  # messages, not threads
    assert "--enable-commands=gmail.messages.search" in args
    assert "--readonly" in args and "--no-input" in args and "--wrap-untrusted" in args
    assert "--max=20" in args  # clamped
    assert args[-2:] == ["--", "-in:spam --help"]  # query can't be parsed as flags
    assert kwargs["timeout"] > 0 and kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in kwargs


def test_send_passes_body_on_stdin_not_command_line(captured):
    gs.send_email("bob@example.com", "Hi", "Line 1\nLine 2")
    args, kwargs = captured[0]
    assert "--enable-commands=gmail.send" in args and "--readonly" not in args
    assert "--body-file=-" in args and not any(a.startswith("--body=") for a in args)
    assert kwargs["input"] == b"Line 1\nLine 2"  # exact bytes, no newline translation


def test_reply_is_pinned_to_confirmed_recipients(captured):
    gs.send_reply("abc123", ["a@example.com", "b@example.com"], "Re: Hello", "Thanks")
    args, kwargs = captured[0]
    assert args[:3] == ["gog", "gmail", "send"]
    assert "--to=a@example.com,b@example.com" in args
    assert "--reply-to-message-id=abc123" in args and "--subject=Re: Hello" in args
    assert kwargs["input"] == b"Thanks"


def test_delete_search_covers_sixty_days(captured):
    gs.find_events_for_deletion("gym")
    args, _ = captured[0]
    start = datetime.datetime.fromisoformat(next(a for a in args if a.startswith("--from="))[7:])
    end = datetime.datetime.fromisoformat(next(a for a in args if a.startswith("--to="))[5:])
    assert (end - start).days == gs.DELETE_SEARCH_DAYS + 1
    assert start.utcoffset() == config.now().utcoffset()  # configured timezone, not a fixed offset


def test_delete_recurring_instance_only_deletes_one(captured):
    gs.delete_calendar_event("abc_20261120T100000Z", "abc", "2026-11-20T14:00:00+04:00")
    args, _ = captured[0]
    assert "--scope=single" in args and "--original-start=2026-11-20T14:00:00+04:00" in args
    assert args[-3:] == ["--", "primary", "abc_20261120T100000Z"]


def test_timeout_becomes_gog_error(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gs.GogError, match="timed out"):
        gs.search_emails()


def test_failure_becomes_gog_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc("", 1, "boom\nmore"))
    with pytest.raises(gs.GogError, match="boom"):
        gs.get_email_details("abc123")


# ---------- validation ----------

@pytest.mark.parametrize("bad", ["", "a b", "--force", "x;rm", "../etc", "é"])
def test_ids_are_validated(bad):
    with pytest.raises(ValueError):
        gs.validate_id(bad)


@pytest.mark.parametrize("bad", ["", "bob", "a@b", "Bob <bob@example.com>", "a@x.com,b@y.com", "a@x.com\nBcc: c@z.com"])
def test_single_plain_email_address_required(bad):
    with pytest.raises(ValueError):
        gs.validate_email_address(bad)


def test_subject_cannot_contain_newlines():
    assert gs.validate_subject("Hello\r\nBcc: evil@example.com") == "Hello Bcc: evil@example.com"


def test_body_limit():
    assert gs.validate_body("x" * gs.MAX_BODY_LEN)
    with pytest.raises(ValueError, match="Shorten"):
        gs.validate_body("x" * (gs.MAX_BODY_LEN + 1))


def test_event_validation():
    with pytest.raises(ValueError):
        gs.validate_event("x", "2026-11-20T10:00:00+04:00", "2026-11-20T09:00:00+04:00")
    with pytest.raises(ValueError, match="14 days"):
        gs.validate_event("x", "2026-11-01T10:00:00+04:00", "2026-11-20T10:00:00+04:00")
    with pytest.raises(ValueError):
        gs.parse_event_time("tomorrow at 3")
    event = gs.validate_event(" Team\x07  sync ", "2026-11-20T10:00:00", "2026-11-20T11:00:00", "notes\x00", "")
    assert event["summary"] == "Team sync" and event["description"] == "notes"
    assert event["start_iso"].endswith("+04:00")  # naive time -> configured timezone


# ---------- parsing email content ----------

def test_parse_sender_strips_markdown_injection():
    raw = '"[Click me](https://evil.example) *urgent*" <bob@example.com>'
    sender = gs.parse_sender(raw)
    assert "evil" in sender["name"] and "[" not in sender["name"] and "(" not in sender["name"]
    assert sender["formatted"].count("](") == 1
    assert sender["formatted"].endswith("to=bob@example.com)")


def test_parse_sender_drops_invalid_address():
    sender = gs.parse_sender("Mallory <x)(evil>")
    assert sender["email"] == "" and "](" not in sender["formatted"]


def test_unwrap_untrusted_round_trip():
    wrapped = gs.wrap_untrusted("hello world")
    assert "EXTERNAL_UNTRUSTED_CONTENT" in wrapped
    assert gs.unwrap_untrusted(wrapped) == "hello world"


def test_get_email_details_handles_dict_headers(monkeypatch):
    payload = {
        "headers": {"from": gs.wrap_untrusted("Alice <alice@example.com>"), "subject": "Hi", "to": "me@x.com"},
        "message": {"snippet": "snip", "threadId": "t9"},
        "body": gs.wrap_untrusted("<p>Hello <b>there</b></p><script>bad()</script>"),
    }
    _serve(monkeypatch, payload)
    details = gs.get_email_details("abc123")
    assert details["sender_email"] == "alice@example.com" and details["thread_id"] == "t9"
    assert "bad()" not in details["content_preview"]
    assert gs.unwrap_untrusted(details["content_preview"]) == "Hello there"


# ---------- who a reply goes to ----------

def test_reply_goes_to_reply_to_and_flags_mismatch(monkeypatch):
    """Phishing pattern: From looks trusted, Reply-To points elsewhere. The preview must show the real target."""
    _serve(monkeypatch, _message_payload({
        "from": gs.wrap_untrusted("Boss <boss@company.example>"),
        "reply_to": gs.wrap_untrusted("attacker@evil.example"),
        "subject": gs.wrap_untrusted("Urgent"),
    }))
    ctx = gs.get_reply_context("m1")
    assert ctx["recipients"] == ["attacker@evil.example"]
    assert ctx["reply_to_differs"] is True and ctx["subject"] == "Re: Urgent"


def test_reply_falls_back_to_sender(monkeypatch):
    _serve(monkeypatch, _message_payload({"from": "Alice <alice@example.com>", "reply_to": "", "subject": "Re: Hi"}))
    ctx = gs.get_reply_context("m1")
    assert ctx["recipients"] == ["alice@example.com"] and ctx["reply_to_differs"] is False
    assert ctx["subject"] == "Re: Hi"  # no double "Re:"


def test_reply_to_own_sent_message_targets_original_recipients(monkeypatch):
    _serve(monkeypatch, _message_payload(
        {"from": "Me <me@example.com>", "to": "Bob <bob@example.com>, carol@example.com", "subject": "Plan"},
        labels=("SENT",)))
    ctx = gs.get_reply_context("m1")
    assert ctx["recipients"] == ["bob@example.com", "carol@example.com"] and ctx["sent_by_me"] is True


def test_reply_without_valid_address_is_refused(monkeypatch):
    _serve(monkeypatch, _message_payload({"from": "Nobody <not-an-address>", "subject": "x"}))
    with pytest.raises(ValueError):
        gs.get_reply_context("m1")

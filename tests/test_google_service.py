import json
import subprocess

import pytest

from assistant import google_service as gs


class FakeProc:
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProc(json.dumps({"threads": [], "events": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_reads_are_least_privilege_and_flag_safe(captured):
    gs.search_emails("-in:spam --help", limit=999)
    args, kwargs = captured[0]
    assert args[:3] == ["gog", "gmail", "search"]
    assert "--enable-commands=gmail.search" in args
    assert "--readonly" in args and "--no-input" in args and "--wrap-untrusted" in args
    assert "--max=20" in args  # clamped
    assert args[-2:] == ["--", "-in:spam --help"]  # query can't be parsed as flags
    assert kwargs["timeout"] > 0
    assert "shell" not in kwargs


def test_writes_are_not_readonly_but_restricted(captured):
    gs.send_email("bob@example.com", "Hi", "Body")
    args, _ = captured[0]
    assert "--enable-commands=gmail.send" in args
    assert "--readonly" not in args


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
        "message": {"snippet": "snip"},
        "body": gs.wrap_untrusted("<p>Hello <b>there</b></p><script>bad()</script>"),
    }
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(json.dumps(payload)))
    details = gs.get_email_details("abc123")
    assert details["sender_email"] == "alice@example.com"
    assert "bad()" not in details["content_preview"]
    assert gs.unwrap_untrusted(details["content_preview"]) == "Hello there"


def test_delete_recurring_instance_only_deletes_one(captured):
    gs.delete_calendar_event("abc_20261120T100000Z", "abc", "2026-11-20T14:00:00+04:00")
    args, _ = captured[0]
    assert "--scope=single" in args and "--original-start=2026-11-20T14:00:00+04:00" in args
    assert args[-3:] == ["--", "primary", "abc_20261120T100000Z"]


def test_event_times_validated():
    with pytest.raises(ValueError):
        gs.create_calendar_event("x", "2026-11-20T10:00:00+04:00", "2026-11-20T09:00:00+04:00")
    with pytest.raises(ValueError):
        gs.parse_event_time("tomorrow at 3")
    assert gs.parse_event_time("2026-11-20T10:00:00").utcoffset().total_seconds() == 4 * 3600

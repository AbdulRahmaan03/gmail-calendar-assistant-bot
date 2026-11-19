import json

import pytest

from assistant import actions, agent
from assistant import google_service as gs

OWNER = 42

EVENTS = [
    {"id": "ev1", "summary": "Gym Session", "start": "2026-11-20T18:00:00+04:00",
     "end": "2026-11-20T19:00:00+04:00", "recurring_event_id": "", "original_start": "", "guest_count": 0},
    {"id": "ev2", "summary": "Team Sync", "start": "2026-11-21T10:00:00+04:00",
     "end": "2026-11-21T11:00:00+04:00", "recurring_event_id": "", "original_start": "", "guest_count": 3},
    {"id": "ev3", "summary": "Team Lunch", "start": "2026-11-22T13:00:00+04:00",
     "end": "2026-11-22T14:00:00+04:00", "recurring_event_id": "", "original_start": "", "guest_count": 0},
]


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Record every write instead of performing it; reads return fixtures."""
    calls = []
    monkeypatch.setattr(gs, "send_email", lambda *a: calls.append(("send", a)) or {})
    monkeypatch.setattr(gs, "send_reply", lambda *a: calls.append(("reply", a)) or {})
    monkeypatch.setattr(gs, "create_calendar_event", lambda *a: calls.append(("create", a)) or {})
    monkeypatch.setattr(gs, "delete_calendar_event", lambda *a: calls.append(("delete", a)) or {})
    monkeypatch.setattr(gs, "get_reply_context", lambda mid: {
        "message_id": mid, "recipients": ["real.sender@example.com"], "subject": "Re: Original",
        "from": "real.sender@example.com", "reply_to_differs": False, "sent_by_me": False})
    monkeypatch.setattr(gs, "_fetch_events", lambda days, query=None: [dict(e) for e in EVENTS])
    monkeypatch.setattr(gs, "search_emails", lambda query="is:unread", limit=5: [
        {"id": "m1", "sender_email": "alice@example.com", "sender_name": "Alice", "subject": "Invoice"},
        {"id": "m2", "sender_email": "bob@example.com", "sender_name": "Bob", "subject": "Lunch?"},
    ])
    agent.conversation_histories.clear()
    actions._pending.clear()
    return calls


# ---------- confirmation gate ----------

def test_proposal_does_not_execute(no_real_side_effects):
    action = actions.propose_email_send(OWNER, "bob@example.com", "Hi", "Hello Bob")
    assert no_real_side_effects == []
    assert "To: bob@example.com" in action.preview and "Hello Bob" in action.preview


def test_only_owner_can_take_and_only_once():
    action = actions.propose_email_send(OWNER, "bob@example.com", "Hi", "Hello")
    assert actions.take(action.id, 999) is None          # someone else
    assert actions.take(action.id, OWNER) is action       # owner
    assert actions.take(action.id, OWNER) is None         # double tap


def test_actions_expire(monkeypatch):
    action = actions.propose_email_send(OWNER, "bob@example.com", "Hi", "Hello")
    monkeypatch.setattr(actions.time, "monotonic", lambda: action.created_at + 10**6)
    assert actions.take(action.id, OWNER) is None


def test_execute_runs_the_stored_parameters(no_real_side_effects):
    action = actions.propose_email_send(OWNER, "bob@example.com", "Hi", "Hello")
    actions.execute(action)
    assert no_real_side_effects == [("send", ("bob@example.com", "Hi", "Hello"))]


def test_reply_preview_and_execution_use_the_same_pinned_recipients(no_real_side_effects):
    action = actions.propose_email_reply(OWNER, "msg1", "Thanks!")
    assert "To: real.sender@example.com" in action.preview and "Subject: Re: Original" in action.preview
    actions.execute(action)
    assert no_real_side_effects == [("reply", ("msg1", ["real.sender@example.com"], "Re: Original", "Thanks!"))]


def test_reply_preview_warns_when_reply_to_differs(monkeypatch):
    monkeypatch.setattr(gs, "get_reply_context", lambda mid: {
        "message_id": mid, "recipients": ["attacker@evil.example"], "subject": "Re: Urgent",
        "from": "boss@company.example", "reply_to_differs": True, "sent_by_me": False})
    preview = actions.propose_email_reply(OWNER, "m1", "OK").preview
    assert "To: attacker@evil.example" in preview and "⚠️" in preview and "boss@company.example" in preview


def test_longest_allowed_email_fits_one_telegram_message():
    action = actions.propose_email_send(OWNER, "a" * 60 + "@example.com", "S" * gs.MAX_SUBJECT_LEN,
                                        "B" * gs.MAX_BODY_LEN)
    assert len(action.preview) <= actions.PREVIEW_LIMIT


def test_oversized_preview_is_rejected_not_truncated(monkeypatch):
    monkeypatch.setattr(actions, "PREVIEW_LIMIT", 50)
    with pytest.raises(ValueError, match="Shorten"):
        actions.propose_email_send(OWNER, "bob@example.com", "Hi", "x" * 100)
    assert actions._pending == {}


def test_invalid_proposals_rejected():
    with pytest.raises(ValueError):
        actions.propose_email_send(OWNER, "a@x.com, b@y.com", "Hi", "x")
    with pytest.raises(ValueError):
        actions.propose_calendar_create(OWNER, "X", "2026-11-20T10:00:00+04:00", "2026-11-20T09:00:00+04:00")
    with pytest.raises(ValueError, match="14 days"):  # caught at proposal time, not after Confirm
        actions.propose_calendar_create(OWNER, "X", "2026-11-01T10:00:00+04:00", "2026-11-30T10:00:00+04:00")


def test_delete_preview_mentions_guests():
    preview = actions.propose_calendar_delete(OWNER, EVENTS[1]).preview
    assert "3 other guest(s)" in preview and "not be notified" in preview


def test_all_day_event_times_shown_as_dates():
    assert actions._fmt_time("2026-11-20") == "Fri 20 Nov 2026 (all day)"


# ---------- agent loop ----------

def _tool_call(name, args, call_id="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _text(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _script(monkeypatch, responses):
    seen = []

    def fake_llm(messages, tools=None):
        seen.append(list(messages))
        return responses.pop(0)

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    return seen


def test_injected_delete_only_proposes(monkeypatch, no_real_side_effects):
    """Even if an email convinces the model to delete an event, nothing is deleted."""
    _script(monkeypatch, [_tool_call("prepare_calendar_delete", {"identifier": "Gym"}), _text("Tap confirm.")])
    reply = agent.process_user_message(OWNER, "summarise my email")
    assert no_real_side_effects == []
    assert len(reply.actions) == 1 and reply.actions[0].kind == actions.CALENDAR_DELETE
    assert "Gym Session" in reply.actions[0].preview


def test_ambiguous_delete_returns_candidates(monkeypatch):
    seen = _script(monkeypatch, [_tool_call("prepare_calendar_delete", {"identifier": "team"}), _text("Which one?")])
    reply = agent.process_user_message(OWNER, "delete the team event")
    assert reply.actions == []
    tool_result = json.loads(seen[1][-1]["content"])
    assert tool_result["status"] == "ambiguous" and len(tool_result["candidates"]) == 2


def test_delete_can_be_narrowed_by_date(monkeypatch):
    _script(monkeypatch, [_tool_call("prepare_calendar_delete", {"identifier": "team", "date": "2026-11-22"}),
                          _text("Tap confirm.")])
    reply = agent.process_user_message(OWNER, "delete the team event on the 22nd")
    assert len(reply.actions) == 1 and "Team Lunch" in reply.actions[0].preview


def test_follow_up_turn_can_see_item_ids(monkeypatch):
    """After an ambiguous delete or an email list, the next turn needs the IDs to act on "the second one"."""
    _script(monkeypatch, [_tool_call("search_emails", {}), _text("You have 2 emails.")])
    agent.process_user_message(OWNER, "show my email")
    seen = _script(monkeypatch, [_text("Which reply?")])
    agent.process_user_message(OWNER, "reply to the second one")
    history_text = json.dumps(seen[0])
    assert "email id=m2" in history_text and "EXTERNAL_UNTRUSTED_CONTENT" in history_text


def test_item_ids_only_kept_for_recent_turns(monkeypatch):
    _script(monkeypatch, [_tool_call("search_emails", {}), _text("Listed.")])
    agent.process_user_message(OWNER, "show my email")
    for i in range(agent.REFERENCE_TURNS):
        _script(monkeypatch, [_text(f"ok {i}")])
        agent.process_user_message(OWNER, f"chat {i}")
    seen = _script(monkeypatch, [_text("done")])
    agent.process_user_message(OWNER, "next")
    assert "email id=m1" not in json.dumps(seen[0])


def test_send_is_proposed_not_sent(monkeypatch, no_real_side_effects):
    _script(monkeypatch, [
        _tool_call("prepare_email_send", {"to": "bob@example.com", "subject": "Hi", "body": "Yo"}),
        _text("Draft ready."),
    ])
    reply = agent.process_user_message(OWNER, "email bob")
    assert no_real_side_effects == [] and reply.actions[0].kind == actions.EMAIL_SEND


def test_bad_tool_args_return_error_to_model(monkeypatch):
    seen = _script(monkeypatch, [_tool_call("prepare_email_send", {"to": "bob"}), _text("Sorry")])
    agent.process_user_message(OWNER, "email bob")
    assert "error" in json.loads(seen[1][-1]["content"])


def test_step_limit_is_reported_honestly(monkeypatch):
    responses = [_tool_call("list_calendar_events", {}, f"c{i}") for i in range(agent.MAX_TOOL_ROUNDS)]
    _script(monkeypatch, responses)
    reply = agent.process_user_message(OWNER, "loop forever")
    assert "stopped" in reply.text and "completed" not in reply.text


def test_history_is_capped(monkeypatch):
    for i in range(20):
        _script(monkeypatch, [_text(f"reply {i}")])
        agent.process_user_message(OWNER, f"msg {i}")
    assert len(agent.conversation_histories[OWNER]) == agent.MAX_HISTORY_TURNS


def test_untrusted_markers_stripped_from_reply(monkeypatch):
    _script(monkeypatch, [_text(gs.wrap_untrusted("Subject line"))])
    reply = agent.process_user_message(OWNER, "hi")
    assert "EXTERNAL_UNTRUSTED" not in reply.text


# ---------- LLM HTTP client ----------

class FakeResponse:
    def __init__(self, status, body=None, text="", headers=None):
        self.status_code, self._body, self.text, self.headers = status, body, text, headers or {}

    def json(self):
        return self._body


def _http(monkeypatch, responses):
    sleeps = []
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(agent.time, "sleep", sleeps.append)
    return sleeps


OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


def test_llm_retries_malformed_tool_call(monkeypatch):
    sleeps = _http(monkeypatch, [FakeResponse(400, text='{"error":{"code":"tool_use_failed"}}'), FakeResponse(200, OK)])
    assert agent.call_llm([]) == OK and len(sleeps) == 1


def test_llm_gives_up_without_sleeping_after_last_attempt(monkeypatch):
    sleeps = _http(monkeypatch, [FakeResponse(429) for _ in range(agent.LLM_ATTEMPTS)])
    with pytest.raises(agent.LLMError, match="429"):
        agent.call_llm([])
    assert len(sleeps) == agent.LLM_ATTEMPTS - 1


def test_llm_auth_error_is_not_retried(monkeypatch):
    sleeps = _http(monkeypatch, [FakeResponse(401, text="bad key")])
    with pytest.raises(agent.LLMError, match="401"):
        agent.call_llm([])
    assert sleeps == []

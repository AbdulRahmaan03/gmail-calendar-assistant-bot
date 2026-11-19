import json

import pytest

from assistant import actions, agent
from assistant import google_service as gs

OWNER = 42


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Record every write instead of performing it; reads return fixtures."""
    calls = []
    monkeypatch.setattr(gs, "send_email", lambda *a: calls.append(("send", a)) or {})
    monkeypatch.setattr(gs, "reply_email", lambda *a: calls.append(("reply", a)) or {})
    monkeypatch.setattr(gs, "create_calendar_event", lambda *a: calls.append(("create", a)) or {})
    monkeypatch.setattr(gs, "delete_calendar_event", lambda *a: calls.append(("delete", a)) or {})
    monkeypatch.setattr(gs, "get_email_details", lambda mid: {
        "sender_email": "real.sender@example.com", "sender_name": "Real", "subject": "Original"})
    monkeypatch.setattr(gs, "list_calendar_events", lambda days=0, query=None: [
        {"id": "ev1", "summary": "Gym Session", "start": "2026-11-20T18:00:00+04:00",
         "end": "2026-11-20T19:00:00+04:00", "recurring_event_id": "", "original_start": ""},
        {"id": "ev2", "summary": "Team Sync", "start": "2026-11-21T10:00:00+04:00",
         "end": "2026-11-21T11:00:00+04:00", "recurring_event_id": "", "original_start": ""},
        {"id": "ev3", "summary": "Team Lunch", "start": "2026-11-22T13:00:00+04:00",
         "end": "2026-11-22T14:00:00+04:00", "recurring_event_id": "", "original_start": ""},
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


def test_reply_preview_shows_real_recipient():
    action = actions.propose_email_reply(OWNER, "msg1", "Thanks!")
    assert "real.sender@example.com" in action.preview


def test_invalid_proposals_rejected():
    with pytest.raises(ValueError):
        actions.propose_email_send(OWNER, "a@x.com, b@y.com", "Hi", "x")
    with pytest.raises(ValueError):
        actions.propose_calendar_create(OWNER, "X", "2026-11-20T10:00:00+04:00", "2026-11-20T09:00:00+04:00")


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
    assert len(agent.conversation_histories[OWNER]) == agent.MAX_HISTORY_MESSAGES


def test_untrusted_markers_stripped_from_reply(monkeypatch):
    _script(monkeypatch, [_text(gs.wrap_untrusted("Subject line"))])
    reply = agent.process_user_message(OWNER, "hi")
    assert "EXTERNAL_UNTRUSTED" not in reply.text

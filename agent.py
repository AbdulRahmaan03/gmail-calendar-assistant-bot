import json
import datetime
import requests
import uuid
import time
from typing import Dict, Any, List, Optional, Tuple
import config
import google_service

# Store pending email actions awaiting Telegram button confirmation
# Format: {draft_id: {"action": "send" | "reply", "to": ..., "subject": ..., "body": ..., "message_id": ...}}
pending_drafts: Dict[str, Dict[str, Any]] = {}

# Simple in-memory conversation history per user_id: {user_id: [messages]}
conversation_histories: Dict[int, List[Dict[str, Any]]] = {}

# Available tool schemas for OpenAI/Groq function calling
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "List calendar events for today or upcoming days, or search by query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days ahead to look (0 = today only, 1 = today and tomorrow, 7 = full week)."
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional search keyword to find specific events (e.g. 'gym', 'meeting')."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new event on Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Title or summary of the event (e.g. 'Gym Session', 'Dentist')."
                    },
                    "start_iso": {
                        "type": "string",
                        "description": "Start time in RFC3339 format with timezone (e.g. '2026-11-18T19:00:00+04:00')."
                    },
                    "end_iso": {
                        "type": "string",
                        "description": "End time in RFC3339 format with timezone (e.g. '2026-11-18T20:00:00+04:00')."
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional notes or details for the event."
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional location of the event."
                    }
                },
                "required": ["summary", "start_iso", "end_iso"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Delete a calendar event by its ID or title/summary keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "The event ID or the title/summary of the event to delete (e.g. 'Gym Session' or 'a5ckbr...')."
                    }
                },
                "required": ["identifier"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_emails",
            "description": "Search user's Gmail using Gmail search queries (e.g. 'is:unread', 'from:linkedin', 'newer_than:2d').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Gmail search query syntax."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of emails to fetch (default: 5)."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_email_details",
            "description": "Fetch headers, snippet, and content preview of a specific email message by its message ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID of the email to inspect."
                    }
                },
                "required": ["message_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_email_send",
            "description": "Prepare a new email draft to be reviewed and confirmed by the user before sending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address."
                    },
                    "subject": {
                        "type": "string",
                        "description": "Subject of the email."
                    },
                    "body": {
                        "type": "string",
                        "description": "Body text of the email."
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_email_reply",
            "description": "Prepare a reply to an existing email to be reviewed and confirmed by the user before sending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Gmail message ID being replied to."
                    },
                    "body": {
                        "type": "string",
                        "description": "Body text of the reply."
                    }
                },
                "required": ["message_id", "body"]
            }
        }
    }
]

def _execute_tool(name: str, args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Executes a tool call. Returns (result_dict, draft_id_if_any).
    """
    draft_id = None
    if name == "list_calendar_events":
        res = google_service.list_calendar_events(
            days=args.get("days", 1),
            query=args.get("query")
        )
        return {"events": res}, None

    elif name == "create_calendar_event":
        res = google_service.create_calendar_event(
            summary=args["summary"],
            start_iso=args["start_iso"],
            end_iso=args["end_iso"],
            description=args.get("description", ""),
            location=args.get("location", "")
        )
        return res, None

    elif name == "delete_calendar_event":
        res = google_service.delete_calendar_event(
            identifier=args["identifier"]
        )
        return res, None

    elif name == "search_emails":
        res = google_service.search_emails(
            query=args.get("query", "is:unread"),
            limit=args.get("limit", 5)
        )
        return {"emails": res}, None

    elif name == "get_email_details":
        res = google_service.get_email_details(
            message_id=args["message_id"]
        )
        return res, None

    elif name == "prepare_email_send":
        draft_id = str(uuid.uuid4())[:8]
        pending_drafts[draft_id] = {
            "action": "send",
            "to": args["to"],
            "subject": args["subject"],
            "body": args["body"]
        }
        return {
            "status": "draft_prepared",
            "draft_id": draft_id,
            "to": args["to"],
            "subject": args["subject"],
            "body": args["body"],
            "message": "Draft prepared. User will be shown Send/Cancel buttons."
        }, draft_id

    elif name == "prepare_email_reply":
        draft_id = str(uuid.uuid4())[:8]
        pending_drafts[draft_id] = {
            "action": "reply",
            "message_id": args["message_id"],
            "body": args["body"]
        }
        return {
            "status": "reply_prepared",
            "draft_id": draft_id,
            "message_id": args["message_id"],
            "body": args["body"],
            "message": "Reply draft prepared. User will be shown Send/Cancel buttons."
        }, draft_id

    return {"error": f"Unknown tool: {name}"}, None

def _get_system_prompt() -> str:
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")
    iso_now = now.strftime("%Y-%m-%dT%H:%M:%S+04:00")
    return f"""You are an executive AI assistant managing Google Calendar and Gmail for your user via Telegram.
Current local time: {now_str}
Current local ISO timestamp: {iso_now}
User Timezone: {config.TIMEZONE} (UTC+4, Gulf Standard Time / Dubai).

Critical Formatting & Mobile UX Guidelines:
1. NEVER use ASCII Markdown tables (no `| Col1 | Col2 |` pipe tables). Telegram mobile does not render tables; they become cluttered, broken lines of text.
2. ALWAYS use a clean mobile Card / Bullet layout with bold labels, bullets (`•`), and indented arrows (`  ↳`).
3. NEVER use markdown `#` or `###` headings (Telegram does not render them as headings; they look like raw hashtags). Use `*Bold Titles*` instead.
4. Schedule Display: List ONLY real, confirmed events returned by the calendar tool. NEVER fabricate, insert, or invent hypothetical 'open blocks', lunch windows, or focus gaps as events. If there is only 1 event, display only that 1 event. If empty, say: "No events scheduled today — your calendar is clear! 🎉".
5. Email Display & Sender Hyperlinking:
   - For every email sender, ALWAYS format their name as a clickable hyperlink using the Gmail compose URL: `[Sender Name](https://mail.google.com/mail/?view=cm&fs=1&to=email)`.
   - NEVER show raw emails or noisy angle brackets like `<donotreply@...>` in the sender name.
   - If no email address is available, display ONLY the clean sender name.
6. Job Alert Aggregation:
   - When emails are job alerts (e.g. from LinkedIn, Indeed, etc.), DO NOT list them as individual random emails.
   - AGGREGATE them under a dedicated `💼 Job Alerts` section, grouped by platform:
     • `[LinkedIn](url)` (N roles):
       ↳ Role Title — Company Name
     • `[Indeed](url)` (N roles):
       ↳ Role Title — Company Name
   - Keep non-job emails (banking, security, personal, direct work) under `📬 Priority Emails`.
7. Always resolve relative dates ('today', 'tomorrow', 'this Friday') using the current local time provided above.
8. When user asks to send or reply to an email, ALWAYS use `prepare_email_send` or `prepare_email_reply`.
"""

def call_llm(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    payload: Dict[str, Any] = {
        "model": config.GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2
    }
    if tools:
        payload["tools"] = tools

    for attempt in range(3):
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()

    resp.raise_for_status()
    return resp.json()

def process_user_message(user_id: int, user_text: str) -> Tuple[str, Optional[str]]:
    """
    Process a user message through the agent loop.
    Returns (assistant_reply_text, pending_draft_id_if_any).
    """
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    history = conversation_histories[user_id]
    
    # Keep last 8 turns (16 messages) to avoid memory growth
    if len(history) > 16:
        history = history[-16:]
        conversation_histories[user_id] = history

    messages = [{"role": "system", "content": _get_system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    active_draft_id: Optional[str] = None
    max_turns = 5

    for _ in range(max_turns):
        resp_json = call_llm(messages, tools=TOOL_SCHEMAS)
        choice = resp_json["choices"][0]
        msg = choice["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            # Final text response
            final_content = msg.get("content") or ""
            # Update history with user request and final assistant response
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": final_content})
            return final_content, active_draft_id

        # Execute tools
        for tc in tool_calls:
            fname = tc["function"]["name"]
            try:
                fargs = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                fargs = {}
            tool_res, draft_id = _execute_tool(fname, fargs)
            if draft_id:
                active_draft_id = draft_id

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_res)
            })

    return "I completed the requested operations.", active_draft_id

def generate_morning_briefing() -> str:
    """
    Generate an executive morning briefing by fetching today's events and recent unread emails.
    """
    events = google_service.list_calendar_events(days=0)
    unread_emails = google_service.search_emails(query="is:unread", limit=8)

    today_str = datetime.datetime.now().strftime("%a, %d %b")

    prompt = f"""Generate a crisp, mobile-optimized executive morning briefing for Telegram.

TODAY'S CALENDAR EVENTS:
{json.dumps(events, indent=2)}

RECENT UNREAD EMAILS:
{json.dumps(unread_emails, indent=2)}

Strict Structure Requirements:
1. Header:
   🌅 *Executive Morning Briefing — {today_str} (Dubai)*
   ━━━━━━━━━━━━━━━━━━━━━━

2. Calendar Section:
   📅 *Today's Schedule* (Count Events)
   - List ONLY the real events found in TODAY'S CALENDAR EVENTS above.
   - Format:
     • HH:MM – HH:MM • Event Title ✂️
       ↳ Duration / Notes
   - If 0 events: `• No events scheduled today — your calendar is clear! 🎉`
   - NEVER fabricate, invent, or add open blocks, study blocks, lunch, or filler events into the schedule.

3. Priority Emails Section:
   📬 *Priority Emails*
   - List direct personal, security, banking, or non-job messages.
   - Format:
     • [Sender Name](https://mail.google.com/mail/?view=cm&fs=1&to=email) — Subject
       ↳ 1-line key action or takeaway

4. Job Alerts Section (Aggregated):
   💼 *Job Alerts (Aggregated)*
   - Group job alert emails by platform (e.g. LinkedIn, Indeed).
   - Format:
     • [Platform Name](url) (X new roles):
       ↳ Role Title — Company Name
       ↳ Role Title — Company Name

5. Daily Heads-Up:
   💡 *Daily Heads-Up:*
   - Exactly 1 or 2 concise, executive sentences highlighting the day's key focus or notable lead.
   - No long paragraphs or generic productivity advice.

CRITICAL: NO ASCII PIPES/TABLES. NO '#' HEADINGS. Keep it clean, high-contrast, and fast to read on a phone.
"""
    messages = [
        {"role": "system", "content": _get_system_prompt()},
        {"role": "user", "content": prompt}
    ]
    resp = call_llm(messages)
    return resp["choices"][0]["message"].get("content") or "Good morning! No events or urgent emails found."

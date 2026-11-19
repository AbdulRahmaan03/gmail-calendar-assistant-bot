import subprocess
import json
import re
import datetime
import email.utils
from typing import List, Dict, Any, Optional

def parse_sender(raw_from: str) -> Dict[str, str]:
    """
    Extract clean sender name and email address, creating a markdown hyperlink to compose in Gmail.
    Example: 'Grammarly <hello@mail.grammarly.com>' ->
      name: 'Grammarly'
      email: 'hello@mail.grammarly.com'
      formatted: '[Grammarly](https://mail.google.com/mail/?view=cm&fs=1&to=hello@mail.grammarly.com)'
    """
    if not raw_from:
        return {"name": "Unknown", "email": "", "formatted": "Unknown"}
    name, addr = email.utils.parseaddr(raw_from)
    name = name.strip().strip('"\'')
    if not name:
        name = addr.split("@")[0] if "@" in addr else addr
    name = name or "Unknown"
    if addr:
        formatted = f"[{name}](https://mail.google.com/mail/?view=cm&fs=1&to={addr})"
    else:
        formatted = name
    return {"name": name, "email": addr, "formatted": formatted}

def _clean_html_text(text: str) -> str:
    """Strip HTML tags, styles, scripts and decode basic entities."""
    if not text:
        return ""
    # Remove script and style tags
    text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Condense multiple whitespaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def run_gog_command(cmd: List[str]) -> Dict[str, Any]:
    """Execute a gog CLI command safely and parse its output."""
    try:
        proc = subprocess.run(
            ["gog"] + cmd,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        if proc.returncode != 0:
            return {"success": False, "error": proc.stderr.strip() or f"Command failed with exit code {proc.returncode}"}
        
        output = proc.stdout.strip()
        if not output:
            return {"success": True, "data": None}
            
        try:
            return {"success": True, "data": json.loads(output)}
        except json.JSONDecodeError:
            return {"success": True, "text": output}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==================== CALENDAR OPERATIONS ====================

def list_calendar_events(days: int = 1, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List calendar events.
    If query is provided, searches by query text.
    Otherwise fetches events from today up to `days` ahead.
    """
    now = datetime.datetime.now()
    if query:
        res = run_gog_command(["calendar", "search", query, "-j"])
    else:
        start_date = now.strftime("%Y-%m-%dT00:00:00+04:00")
        end_date = (now + datetime.timedelta(days=max(0, days))).strftime("%Y-%m-%dT23:59:59+04:00")
        res = run_gog_command(["calendar", "events", "primary", "--from", start_date, "--to", end_date, "-j"])
        
    if not res.get("success"):
        return [{"error": res.get("error")}]

    data = res.get("data", {})
    raw_events = data.get("events", []) if isinstance(data, dict) else []
    
    events = []
    for e in raw_events:
        events.append({
            "id": e.get("id"),
            "summary": e.get("summary", "No Title"),
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "location": e.get("location", ""),
            "description": e.get("description", "")
        })
    return events

def create_calendar_event(summary: str, start_iso: str, end_iso: str, description: str = "", location: str = "") -> Dict[str, Any]:
    """
    Create a new calendar event.
    start_iso and end_iso should be RFC3339 formatted strings (e.g. 2026-11-18T19:00:00+04:00).
    """
    cmd = [
        "calendar", "create", "primary",
        f"--summary={summary}",
        f"--from={start_iso}",
        f"--to={end_iso}",
        "-j"
    ]
    if description:
        cmd.append(f"--description={description}")
    if location:
        cmd.append(f"--location={location}")
        
    res = run_gog_command(cmd)
    if not res.get("success"):
        return {"success": False, "error": res.get("error")}
        
    event_data = res.get("data", {}).get("event", {})
    return {
        "success": True,
        "event_id": event_data.get("id"),
        "summary": event_data.get("summary"),
        "start": event_data.get("start", {}).get("dateTime") or event_data.get("start", {}).get("date"),
        "end": event_data.get("end", {}).get("dateTime") or event_data.get("end", {}).get("date")
    }

def delete_calendar_event(identifier: str) -> Dict[str, Any]:
    """
    Delete a calendar event by ID or keyword/title.
    If identifier is not an exact event ID, it will search upcoming events to find a match.
    """
    event_id = identifier.strip()
    summary = identifier
    
    # If identifier doesn't look like an alphanumeric ID (e.g. contains spaces or letters with punctuation),
    # search for matching events first.
    if " " in identifier or len(identifier) < 15:
        search_res = list_calendar_events(days=7, query=identifier)
        if not search_res or "error" in search_res[0]:
            # Try searching without query parameter over next 7 days
            search_res = list_calendar_events(days=7)
            matches = [e for e in search_res if identifier.lower() in (e.get("summary") or "").lower()]
            if not matches:
                return {"success": False, "error": f"No event found matching '{identifier}'."}
            event_id = matches[0]["id"]
            summary = matches[0]["summary"]
        else:
            event_id = search_res[0]["id"]
            summary = search_res[0]["summary"]
            
    res = run_gog_command(["calendar", "delete", "primary", event_id, "-y"])
    if not res.get("success"):
        return {"success": False, "error": res.get("error")}
        
    return {"success": True, "deleted_event": summary, "event_id": event_id}

# ==================== GMAIL OPERATIONS ====================

def search_emails(query: str = "is:unread", limit: int = 5) -> List[Dict[str, Any]]:
    """
    Search Gmail messages matching a Gmail query (e.g. 'is:unread', 'from:Indeed', 'newer_than:2d').
    """
    res = run_gog_command(["gmail", "search", query, "-j", f"--limit={limit}"])
    if not res.get("success"):
        return [{"error": res.get("error")}]
        
    data = res.get("data", {})
    threads = data.get("threads", []) if isinstance(data, dict) else []
    
    results = []
    for t in threads:
        sender_info = parse_sender(t.get("from", ""))
        results.append({
            "id": t.get("id"),
            "from": sender_info["formatted"],
            "sender_name": sender_info["name"],
            "sender_email": sender_info["email"],
            "subject": t.get("subject", "(No Subject)"),
            "date": t.get("date", ""),
            "labels": t.get("labels", [])
        })
    return results

def get_email_details(message_id: str) -> Dict[str, Any]:
    """
    Get email headers and readable snippet/body for a given message ID.
    """
    res = run_gog_command(["gmail", "get", message_id, "-j"])
    if not res.get("success"):
        return {"success": False, "error": res.get("error")}
        
    data = res.get("data", {})
    headers = {h.get("name", "").lower(): h.get("value", "") for h in data.get("headers", [])}
    
    snippet = data.get("snippet", "")
    body_raw = data.get("body", "")
    cleaned_body = _clean_html_text(body_raw)[:1200]
    
    raw_from = headers.get("from") or data.get("from", "")
    sender_info = parse_sender(raw_from)

    return {
        "success": True,
        "id": message_id,
        "from": sender_info["formatted"],
        "sender_name": sender_info["name"],
        "sender_email": sender_info["email"],
        "to": headers.get("to") or data.get("to", ""),
        "subject": headers.get("subject") or data.get("subject", ""),
        "date": headers.get("date") or data.get("date", ""),
        "snippet": snippet,
        "content_preview": cleaned_body or snippet
    }

def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    """
    Send an email to a recipient.
    """
    res = run_gog_command([
        "gmail", "send",
        f"--to={to}",
        f"--subject={subject}",
        f"--body={body}"
    ])
    if not res.get("success"):
        return {"success": False, "error": res.get("error")}
    return {"success": True, "message": f"Email successfully sent to {to}."}

def reply_email(message_id: str, body: str) -> Dict[str, Any]:
    """
    Reply to an existing Gmail message.
    """
    res = run_gog_command([
        "gmail", "reply", message_id,
        f"--body={body}"
    ])
    if not res.get("success"):
        return {"success": False, "error": res.get("error")}
    return {"success": True, "message": f"Reply successfully sent for message {message_id}."}

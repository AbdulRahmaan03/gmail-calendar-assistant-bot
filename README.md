# 🤖 Executive Assistant Bot (Gmail & Google Calendar via Telegram)

A lightweight personal AI assistant that manages your **Google Calendar** and **Gmail** from **Telegram**.

Built with Python, `python-telegram-bot`, [`gog`](https://github.com/steipete/gogcli) and tool calling on **Groq** (`openai/gpt-oss-120b`).

---

## ✨ Features

- **📅 Calendar:** view today or upcoming days, search events, create and delete events in natural language.
- **📬 Gmail:** search and summarise email, draft new emails and replies.
- **💼 Job-alert aggregation:** LinkedIn/Indeed-style alerts are grouped by platform so priority mail stands out.
- **🌅 Morning briefing:** pushed daily at a configurable time, or on demand with `/briefing`.
- **🪟 Windows friendly:** run in a console, silently in the background, or at login.

## 🛡️ Safety guardrails

| Guardrail | How it works |
| :--- | :--- |
| **Single authorised user** | `TELEGRAM_ALLOWED_USER_ID` is required. The bot refuses to start without it and only answers that user, in a private chat. |
| **Confirm before any change** | Sending, replying, creating and deleting are only *proposed* by the AI. You get a preview built from the exact stored details (recipient, subject, body, event time) with Confirm/Cancel buttons. Previews are never cut short: anything too long for one Telegram message is refused. Proposals expire after 15 minutes by default and can be confirmed only once. |
| **Replies go where the preview says** | The bot works out a reply's recipients itself (the email's Reply-To address if it has one, otherwise the sender), shows them, and sends to exactly those addresses. If an email asks for replies to go somewhere other than its sender, the preview warns you, a common phishing trick. |
| **Prompt-injection defence** | Email and calendar text is marked as untrusted third-party content (gog `--wrap-untrusted`). The AI is told never to follow instructions inside it, and even if it did, nothing happens without your confirmation. The briefing has no tools at all. |
| **Least-privilege CLI calls** | Each `gog` call is limited to the one command it needs (`--enable-commands`), reads run with `--readonly`, values are passed after `--` with no shell, email bodies go through stdin rather than the command line, and every call has a timeout. |
| **Input validation** | Recipient addresses, IDs, subjects, bodies and event times are validated; result counts and date ranges are capped. |
| **Secret hygiene** | Secrets live only in `.env` (git-ignored). Logs (`runtime/bot.log`) redact tokens and API keys, and rotate at 1 MB. |
| **Single instance** | An OS file lock prevents two copies from running; `runtime/bot.pid` lets the scripts find the process. |

### Privacy note

To answer you, the bot sends the content of the emails and events it reads (subjects, senders, previews) and your messages to **Groq's API**. Don't use it with mail you aren't comfortable sharing with that provider. Conversation history is kept in memory only: the last 8 exchanges, plus the email and event IDs (with short subjects or titles) from the last 2, so follow-ups like "reply to the second one" work. It's cleared on restart or with `/reset`.

---

## 🏗️ Architecture

```mermaid
flowchart LR
    User["📱 You on Telegram"] <-->|python-telegram-bot| Bot["🤖 assistant/bot.py<br/>auth, buttons, scheduler"]
    Bot <--> Agent["🧠 agent.py<br/>Groq tool calling"]
    Bot -->|on Confirm only| Actions["🛡️ actions.py<br/>pending actions"]
    Agent -->|propose| Actions
    Agent -->|read| Google["⚡ google_service.py<br/>gog CLI"]
    Actions -->|execute| Google
    Google <--> GSuite["📅 Calendar & 📬 Gmail"]
```

## 📁 Repository structure

```
.
├── assistant/               # the bot (Python package, run with `python -m assistant`)
│   ├── __main__.py          # entry point
│   ├── bot.py               # Telegram handlers, authorisation, confirmations, daily briefing
│   ├── agent.py             # LLM tool schemas, tool loop, system prompt, briefing
│   ├── actions.py           # confirmation gate: validated pending actions + execution
│   ├── google_service.py    # gog CLI wrapper (validation, timeouts, least privilege)
│   ├── config.py            # loads and validates .env (fails closed)
│   ├── logging_setup.py     # rotating log with secret redaction
│   ├── instance_lock.py     # single-instance lock + PID file
│   └── paths.py             # file locations
├── scripts/windows/         # run.bat, start_silent.vbs, status.bat, stop_bot.bat
├── tests/                   # pytest suite for the safety behaviour
├── runtime/                 # created at run time: bot.log, bot.pid, bot.lock (git-ignored)
├── .env.example
├── pyproject.toml / requirements.txt / requirements-dev.txt
└── README.md / LICENSE
```

---

## 🚀 Getting started

### 1. Prerequisites
- Python 3.11+.
- [`gog`](https://github.com/steipete/gogcli) v0.48 or later, signed in to your Google account (`gog auth add you@gmail.com`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric user ID (e.g. from @userinfobot).
- A Groq API key from the [Groq Console](https://console.groq.com/).

### 2. Install
```bash
git clone https://github.com/AbdulRahmaan03/gmail-calendar-assistant-bot.git
cd gmail-calendar-assistant-bot
pip install -r requirements.txt
```
Copy `.env.example` to `.env` (`copy .env.example .env` in Command Prompt, `cp .env.example .env` elsewhere) and fill it in.

### Configuration (`.env`)

| Setting | Required | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | yes | | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | yes | | Your numeric Telegram user ID; everyone else is ignored |
| `GROQ_API_KEY` | yes | | Groq API key |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | Groq model with tool calling |
| `TIMEZONE` | no | `Asia/Dubai` | IANA timezone used for dates and the briefing |
| `BRIEFING_TIME` | no | `08:00` | Daily briefing time (24h) |
| `ACTION_TTL_MINUTES` | no | `15` | How long a proposal can be confirmed |
| `GOG_TIMEOUT_SECONDS` | no | `60` | Timeout for each gog call |
| `LLM_TIMEOUT_SECONDS` | no | `30` | Timeout for each Groq request |

The bot checks these at startup and refuses to start if a required value is missing or invalid (the reason is written to `runtime/bot.log`).

### 3. Run
- **Console:** `python -m assistant` from the project folder, or double-click `scripts\windows\run.bat`.
- **Background:** double-click `scripts\windows\start_silent.vbs`.
- **At login:** `Win + R` → `shell:startup` → put a shortcut to `scripts\windows\start_silent.vbs` there.
- **Status / stop:** `scripts\windows\status.bat` / `scripts\windows\stop_bot.bat`.

Logs are written to `runtime/bot.log`.

### 4. Develop
```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

---

## 💬 Usage examples

| Intent | Example |
| :--- | :--- |
| Morning briefing | `/briefing` |
| Check calendar | "What's on my schedule today?" |
| Create event | "Book a doctor appointment tomorrow from 3pm to 4pm" → tap **Create event** |
| Delete event | "Cancel my haircut today" → tap **Delete event** |
| Delete one of several | "Delete the team sync on 26 November" |
| Check email | "Show my unread emails" |
| Reply | "Reply to John saying I received the proposal" → review → tap **Send reply** |
| Forget context | `/reset` |

## ⚠️ Known limits

- Only your **primary** calendar is used. Deleting looks 60 days ahead; listing up to 31.
- Deleting an event doesn't notify its guests (the preview says how many there are).
- Replies go to the sender only (no reply-all) and don't include the quoted original. No attachments.
- Email bodies are limited to 3,000 characters so the whole text fits in one confirmation message.
- Pending confirmations and history live in memory, so a restart clears them.

## 📄 License
[MIT](LICENSE)

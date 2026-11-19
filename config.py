import os
import sys
from dotenv import load_dotenv

# Load .env file
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Dubai")
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "08:00")

# Validation
if not TELEGRAM_BOT_TOKEN:
    print("[ERROR] TELEGRAM_BOT_TOKEN is not set in .env!")
    sys.exit(1)

if not TELEGRAM_ALLOWED_USER_ID:
    print("[WARNING] TELEGRAM_ALLOWED_USER_ID is not set. All users will have access!")
    ALLOWED_USER_ID = None
else:
    try:
        ALLOWED_USER_ID = int(TELEGRAM_ALLOWED_USER_ID)
    except ValueError:
        print("[ERROR] TELEGRAM_ALLOWED_USER_ID must be an integer Telegram user ID!")
        sys.exit(1)

if not GROQ_API_KEY:
    print("[ERROR] GROQ_API_KEY is not set in .env!")
    sys.exit(1)

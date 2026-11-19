import os
import sys

# Dummy configuration so importing config never reads a real .env.
os.environ["ASSISTANT_SKIP_DOTENV"] = "1"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:" + "A" * 35
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "42"
os.environ["GROQ_API_KEY"] = "gsk_test_key_for_unit_tests"
os.environ["TIMEZONE"] = "Asia/Dubai"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

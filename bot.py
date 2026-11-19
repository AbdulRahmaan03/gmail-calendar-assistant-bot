import os
import sys
import logging
import datetime
import zoneinfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

import config
import agent
import google_service

# Logging Setup (Both to bot.log and console if available)
log_handlers = [
    logging.FileHandler(os.path.join(os.path.dirname(__file__), "bot.log"), encoding="utf-8")
]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

def is_authorized(update: Update) -> bool:
    """Verify if the message is from the authorized user."""
    if not config.ALLOWED_USER_ID:
        return True
    user = update.effective_user
    return user is not None and user.id == config.ALLOWED_USER_ID

async def send_safe_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None):
    """Send message with Markdown formatting, falling back to plain text if parsing fails."""
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Markdown parse failed ({e}), sending plain text.")
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup
        )

# ==================== COMMAND HANDLERS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome command handler."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized. You do not have access to this bot.")
        return

    welcome_text = (
        "👋 *Welcome! I am your personal Executive Assistant.*\n\n"
        "I am directly connected to your **Google Calendar** and **Gmail**.\n\n"
        "📅 *Calendar Controls:*\n"
        "• *'What is on my calendar today?'*\n"
        "• *'Schedule meeting with Sarah tomorrow from 2pm to 3pm'*\n"
        "• *'Cancel my gym session today'*\n\n"
        "📬 *Gmail Controls:*\n"
        "• *'Check my unread emails'*\n"
        "• *'Show emails from LinkedIn'*\n"
        "• *'Reply to John saying I received the file'* (always asks for confirmation first!)\n\n"
        "🌅 *Executive Briefing:*\n"
        "• Send `/briefing` for an instant morning overview of your day.\n"
        f"• Automatically delivered every morning at *{config.BRIEFING_TIME}* ({config.TIMEZONE}).\n\n"
        "How can I assist you right now?"
    )
    await send_safe_message(context, update.effective_chat.id, welcome_text)

async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger manual on-demand briefing."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        briefing = agent.generate_morning_briefing()
        await send_safe_message(context, update.effective_chat.id, briefing)
    except Exception as e:
        logger.error(f"Error generating briefing: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error generating briefing: {str(e)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help guide handler."""
    if not is_authorized(update):
        return

    help_text = (
        "💡 *Assistant Help & Commands:*\n\n"
        "• `/briefing` - Instant morning summary of today's schedule & key emails.\n"
        "• `/start` - Restart greeting and instructions.\n"
        "• `/help` - Show this guide.\n\n"
        "You can chat naturally! Some example phrases:\n"
        "👉 *'What events do I have this Thursday?'*\n"
        "👉 *'Add Doctors Appointment today at 4:30pm for 1 hour'*\n"
        "👉 *'Delete the Doctors Appointment'*\n"
        "👉 *'Search emails with subject Invoice'*\n"
        "👉 *'Draft an email to client@example.com about project update'*"
    )
    await send_safe_message(context, update.effective_chat.id, help_text)

# ==================== MESSAGE HANDLER ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler for natural language interaction."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized. You do not have access to this bot.")
        return

    user_text = update.message.text
    if not user_text:
        return

    # Indicate typing while agent processes
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        reply_text, draft_id = agent.process_user_message(update.effective_user.id, user_text)

        reply_markup = None
        if draft_id:
            # Attach confirmation buttons for the pending email draft
            keyboard = [
                [
                    InlineKeyboardButton("✅ Send Email", callback_data=f"send:{draft_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        await send_safe_message(context, update.effective_chat.id, reply_text, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in handle_message: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Error processing your request: {str(e)}")

# ==================== INLINE BUTTON CALLBACK HANDLER ====================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback button clicks (Confirm Send / Cancel)."""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 2:
        return

    action, draft_id = parts[0], parts[1]
    draft = agent.pending_drafts.get(draft_id)

    if not draft:
        await query.edit_message_text("⚠️ This draft has expired or has already been processed.")
        return

    if action == "cancel":
        del agent.pending_drafts[draft_id]
        await query.edit_message_text("❌ **Draft cancelled.** Nothing was sent.", parse_mode="Markdown")
        return

    if action == "send":
        await query.edit_message_text("⏳ *Dispatching email...*", parse_mode="Markdown")
        try:
            if draft["action"] == "send":
                res = google_service.send_email(
                    to=draft["to"],
                    subject=draft["subject"],
                    body=draft["body"]
                )
                recipient = draft["to"]
            else:
                res = google_service.reply_email(
                    message_id=draft["message_id"],
                    body=draft["body"]
                )
                recipient = f"message {draft['message_id']}"

            if res.get("success"):
                await query.edit_message_text(
                    f"✅ **Email successfully dispatched!**\n\nRecipient: `{recipient}`",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    f"❌ **Failed to send email:** {res.get('error')}",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Error sending email: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Error dispatching email: {str(e)}")
        finally:
            if draft_id in agent.pending_drafts:
                del agent.pending_drafts[draft_id]

# ==================== SCHEDULED BRIEFING JOB ====================

async def scheduled_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled task executed daily at the configured briefing time."""
    if not config.ALLOWED_USER_ID:
        logger.warning("No ALLOWED_USER_ID configured; cannot send scheduled briefing.")
        return

    logger.info("Executing scheduled morning briefing...")
    try:
        briefing = agent.generate_morning_briefing()
        await send_safe_message(context, config.ALLOWED_USER_ID, briefing)
        logger.info("Morning briefing sent successfully.")
    except Exception as e:
        logger.error(f"Failed to execute scheduled morning briefing: {e}", exc_info=True)

# ==================== APPLICATION INITIALIZATION ====================

def main():
    logger.info("Starting Executive Assistant Bot...")
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("briefing", briefing_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Configure daily briefing schedule
    if app.job_queue and config.ALLOWED_USER_ID:
        try:
            hour, minute = map(int, config.BRIEFING_TIME.split(":"))
            tz = zoneinfo.ZoneInfo(config.TIMEZONE)
            briefing_time = datetime.time(hour=hour, minute=minute, tzinfo=tz)
            app.job_queue.run_daily(
                scheduled_briefing_job,
                time=briefing_time,
                name="daily_morning_briefing"
            )
            logger.info(f"Scheduled daily morning briefing for {config.BRIEFING_TIME} ({config.TIMEZONE}).")
        except Exception as e:
            logger.error(f"Could not setup scheduled job: {e}")

    logger.info("Bot is polling for updates. Press Ctrl+C to stop.")
    try:
        app.run_polling(drop_pending_updates=True, stop_signals=None)
    except Conflict:
        msg = (
            "\n" + "=" * 60 + "\n"
            "ℹ️  ASSISTANT BOT IS ALREADY RUNNING IN THE BACKGROUND!\n"
            "=" * 60 + "\n"
            "The bot was started automatically when your laptop booted up.\n"
            "You do not need to run it again in this terminal window.\n"
            "👉 Simply open Telegram and message @Naukr_bot!\n"
            "=" * 60 + "\n"
        )
        print(msg)
        logger.info("Another bot instance is already polling. Exiting duplicate instance cleanly.")
    except Exception as e:
        logger.exception(f"Fatal error in run_polling: {e}")
    finally:
        logger.info("Application shut down.")

if __name__ == "__main__":
    main()

"""Telegram front end: authorisation, message handling, confirmations, daily briefing."""

from __future__ import annotations

import asyncio
import datetime
import logging

from . import logging_setup

logging_setup.configure()

# Imported after logging is configured so configuration errors land in runtime/bot.log.
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update  # noqa: E402
from telegram.constants import ChatAction, ChatType  # noqa: E402
from telegram.error import BadRequest, Conflict  # noqa: E402
from telegram.ext import (  # noqa: E402
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import actions, agent, config, google_service, instance_lock  # noqa: E402

logger = logging.getLogger("bot")

TELEGRAM_LIMIT = 4000  # Telegram's hard limit is 4096 characters per message.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
GENERIC_ERROR = "⚠️ Something went wrong while handling that. Details are in runtime/bot.log."


def is_authorized(update: Update) -> bool:
    """Only the configured user, and only in a private chat with the bot."""
    user, chat = update.effective_user, update.effective_chat
    ok = (
        user is not None
        and chat is not None
        and user.id == config.ALLOWED_USER_ID
        and chat.type == ChatType.PRIVATE
    )
    if not ok:
        logger.warning(
            "Rejected update from user_id=%s chat_type=%s",
            user.id if user else None,
            chat.type if chat else None,
        )
    return ok


def _fit(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Defensive cap; previews are already limited in actions.py, so this should never cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunks(text: str, size: int = TELEGRAM_LIMIT) -> list[str]:
    text = text or "(empty response)"
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


async def send_safe_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Send Markdown, falling back to plain text if Telegram can't parse it."""
    for part in _chunks(text):
        try:
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode="Markdown",
                                           link_preview_options=NO_PREVIEW)
        except BadRequest as exc:  # Telegram couldn't parse the Markdown; other errors propagate
            logger.info("Markdown send failed (%s); sending plain text.", exc)
            await context.bot.send_message(chat_id=chat_id, text=part, link_preview_options=NO_PREVIEW)


async def send_confirmation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: actions.PendingAction) -> None:
    """Show the stored action itself (not the model's description of it), as plain text."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(action.confirm_label, callback_data=f"ok:{action.id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"no:{action.id}"),
    ]])
    footer = f"\n\nExpires in {config.ACTION_TTL_MINUTES} min. Nothing happens until you tap a button."
    await context.bot.send_message(chat_id=chat_id, text=_fit(action.preview + footer), reply_markup=keyboard,
                                   link_preview_options=NO_PREVIEW)


# ==================== COMMAND HANDLERS ====================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return
    welcome_text = (
        "👋 *Welcome! I am your personal Executive Assistant.*\n\n"
        "I am connected to your *Google Calendar* and *Gmail*.\n\n"
        "📅 *Calendar:*\n"
        "• 'What is on my calendar today?'\n"
        "• 'Schedule a meeting with Sarah tomorrow from 2pm to 3pm'\n"
        "• 'Cancel my gym session today'\n\n"
        "📬 *Gmail:*\n"
        "• 'Check my unread emails'\n"
        "• 'Show emails from LinkedIn'\n"
        "• 'Reply to John saying I received the file'\n\n"
        "🛡 Every email and calendar change is shown to you first and only happens when you tap Confirm.\n\n"
        "🌅 *Briefing:* send /briefing any time. It is also delivered daily at "
        f"*{config.BRIEFING_TIME}* ({config.TIMEZONE})."
    )
    await send_safe_message(context, update.effective_chat.id, welcome_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    help_text = (
        "💡 *Commands*\n\n"
        "• /briefing - summary of today's schedule and key emails\n"
        "• /reset - forget this conversation's history\n"
        "• /start - greeting and examples\n"
        "• /help - this guide\n\n"
        "You can also just chat, e.g. 'What do I have on Thursday?' or "
        "'Draft an email to client@example.com about the project update'."
    )
    await send_safe_message(context, update.effective_chat.id, help_text)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    agent.conversation_histories.pop(update.effective_user.id, None)
    await update.effective_message.reply_text("🧹 Conversation history cleared.")


async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        briefing = await asyncio.to_thread(agent.generate_morning_briefing)
    except agent.LLMError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Error generating briefing")
        await update.effective_message.reply_text(GENERIC_ERROR)
        return
    await send_safe_message(context, update.effective_chat.id, briefing)


# ==================== MESSAGE HANDLER ====================


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return
    user_text = (update.effective_message.text or "").strip()
    if not user_text:
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        # Blocking HTTP and subprocess work runs off the event loop.
        reply = await asyncio.to_thread(agent.process_user_message, update.effective_user.id, user_text)
    except agent.LLMError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Error in handle_message")
        await update.effective_message.reply_text(GENERIC_ERROR)
        return

    await send_safe_message(context, chat_id, reply.text)
    for action in reply.actions:
        await send_confirmation(context, chat_id, action)


# ==================== CONFIRM / CANCEL BUTTONS ====================


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authorized(update):
        return

    verb, _, action_id = (query.data or "").partition(":")
    if verb not in ("ok", "no") or not action_id:
        return

    # take() removes the action atomically, so a double tap can't run it twice.
    action = actions.take(action_id, update.effective_user.id)
    if action is None:
        await query.edit_message_text("⚠️ This request has expired or was already handled. Nothing was done.")
        return

    if verb == "no":
        await query.edit_message_text(_fit(f"{action.preview}\n\n❌ Cancelled. Nothing was done."))
        return

    await query.edit_message_text(_fit(f"{action.preview}\n\n⏳ Working on it…"))
    try:
        result = await asyncio.to_thread(actions.execute, action)
        status = f"✅ {result}"
    except (google_service.GogError, ValueError) as exc:
        logger.warning("Confirmed %s failed: %s", action.kind, exc)
        status = f"❌ Failed: {exc}"
    except Exception:
        logger.exception("Confirmed %s crashed", action.kind)
        status = "❌ Failed due to an internal error. Details are in runtime/bot.log."
    await query.edit_message_text(_fit(f"{action.preview}\n\n{status}"))


# ==================== SCHEDULED BRIEFING ====================


async def scheduled_briefing_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Running scheduled morning briefing.")
    try:
        briefing = await asyncio.to_thread(agent.generate_morning_briefing)
        await send_safe_message(context, config.ALLOWED_USER_ID, briefing)
        logger.info("Morning briefing sent.")
    except Exception:
        logger.exception("Scheduled morning briefing failed")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)


# ==================== STARTUP ====================


def main() -> None:
    try:
        instance_lock.acquire()
    except instance_lock.AlreadyRunningError:
        msg = "The assistant bot is already running. You don't need to start it again."
        logger.info(msg)
        print(msg)
        return

    try:
        logger.info("Starting Executive Assistant Bot (pid lock acquired).")
        app = (
            ApplicationBuilder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .connect_timeout(20)
            .read_timeout(30)
            .write_timeout(30)
            .get_updates_connect_timeout(20)
            .get_updates_read_timeout(40)
            .build()
        )

        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("reset", reset_command))
        app.add_handler(CommandHandler("briefing", briefing_command))
        app.add_handler(CallbackQueryHandler(handle_callback))
        # New messages only: editing an old message must not re-run the request.
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(on_error)

        if app.job_queue is None:
            logger.error("Job queue unavailable; install python-telegram-bot[job-queue] for daily briefings.")
        else:
            briefing_time = datetime.time(config.BRIEFING_HOUR, config.BRIEFING_MINUTE, tzinfo=config.TZ)
            app.job_queue.run_daily(scheduled_briefing_job, time=briefing_time, name="daily_morning_briefing")
            logger.info("Daily briefing scheduled for %s (%s).", config.BRIEFING_TIME, config.TIMEZONE)

        logger.info("Polling for updates. Press Ctrl+C to stop.")
        # stop_signals=None: signal handlers are unsupported on Windows; Ctrl+C still stops the bot.
        # bootstrap_retries=-1: keep retrying at startup (e.g. network not up yet right after login).
        app.run_polling(drop_pending_updates=True, stop_signals=None, bootstrap_retries=-1)
    except Conflict:
        logger.error("Telegram reports another client polling with this token (another machine?). Exiting.")
    finally:
        instance_lock.release()
        logger.info("Application shut down.")


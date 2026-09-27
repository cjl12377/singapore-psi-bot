import logging
import os
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from psi import format_psi_message, get_psi_data

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 8443))

USER_COOLDOWN_SECS = 30
_user_last_request: dict[str, float] = {}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Singapore PSI Bot\n\n"
        "Get live air quality readings from the National Environment Agency.\n\n"
        "/psi  — current PSI and PM2.5 readings\n"
        "/help — show this message\n\n"
        "Data from data.gov.sg, updated hourly."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/psi  — current PSI and PM2.5 readings (national + regional)\n"
        "/help — show this message\n\n"
        "Readings are fetched from data.gov.sg and cached for 10 minutes."
    )


async def cmd_psi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    now = time.monotonic()

    wait = USER_COOLDOWN_SECS - (now - _user_last_request.get(user_id, 0))
    if wait > 0:
        await update.message.reply_text(f"Please wait {int(wait)}s before requesting again.")
        return

    _user_last_request[user_id] = now

    data, stale_reason = await get_psi_data()
    if data is None:
        await update.message.reply_text(
            f"Could not fetch PSI data: {stale_reason}. Please try again later."
        )
        return

    await update.message.reply_text(format_psi_message(data, stale_reason))


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("psi", cmd_psi))

    logger.info("Starting webhook on port %d → %s/webhook", PORT, WEBHOOK_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook",
    )


if __name__ == "__main__":
    main()

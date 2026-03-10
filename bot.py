"""
Telegram AI Bot — Multi-provider AI bot with Render webhook support.
Run locally  → set WEBHOOK_URL="" → uses polling
Deploy Render → set WEBHOOK_URL="https://your-app.onrender.com" → uses webhook
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import config
from ai_client import (
    AVAILABLE_MODELS,
    get_ai_response,
    get_active_providers,
    get_model_label,
)

# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  IN-MEMORY USER STATE
#  { user_id: { "provider": str, "model": str, "history": [...] } }
# ─────────────────────────────────────────────
user_states: dict = {}


def get_state(user_id: int) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "provider": config.DEFAULT_PROVIDER,
            "model":    config.DEFAULT_MODEL,
            "history":  [],
        }
    return user_states[user_id]


# ─────────────────────────────────────────────
#  COMMAND HANDLERS
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    active = get_active_providers()
    providers_list = "\n".join(
        f"  • {AVAILABLE_MODELS[p]['name']}" for p in active
    ) or "  ⚠️ Koi API key set nahi hai!"

    await update.message.reply_text(
        "🤖 *AI Multi-Model Bot में आपका स्वागत है!*\n\n"
        "Main alag-alag AI models se baat kar sakta hoon.\n\n"
        f"*Active Providers:*\n{providers_list}\n\n"
        "*Commands:*\n"
        "  /model  — AI model select karein\n"
        "  /status — Current model dekhen\n"
        "  /clear  — Chat history saaf karein\n"
        "  /help   — Help\n\n"
        "Bas koi bhi message bhejein! 💬",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 *Help Menu*\n\n"
        "*Basic Commands:*\n"
        "/start  — Bot shuru karein\n"
        "/model  — AI model badlein (2-step picker)\n"
        "/status — Kaunsa model use ho raha hai\n"
        "/clear  — Is session ki history delete karein\n"
        "/help   — Yeh message\n\n"
        "*AI Providers:*\n"
        "• 🤖 OpenAI  — GPT-4o, GPT-3.5 etc.\n"
        "• 🧠 Anthropic  — Claude Sonnet/Opus/Haiku\n"
        "• 🌟 Grok (xAI)  — Grok 2/3/4\n"
        "• 💎 Google Gemini  — Flash / Pro\n"
        "• ⚡ Groq  — Ultra-fast Llama / Mixtral\n"
        "• 🚀 SambaNova  — Llama 405B & ALLaM\n\n"
        "_Render ke liye WEBHOOK\\_URL env var set karein._",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    provider = state["provider"]
    model_id = state["model"]
    label = get_model_label(provider, model_id)
    history_len = len(state["history"])
    pname = AVAILABLE_MODELS.get(provider, {}).get("name", provider)

    await update.message.reply_text(
        f"📊 *Current Status*\n\n"
        f"Provider : {pname}\n"
        f"Model    : `{label}`\n"
        f"History  : {history_len} messages",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_user.id)["history"] = []
    await update.message.reply_text("🧹 Chat history clear ho gayi!")


# ─────────────────────────────────────────────
#  MODEL PICKER  (2-step inline keyboard)
# ─────────────────────────────────────────────
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    active = get_active_providers()
    if not active:
        await update.message.reply_text(
            "⚠️ Koi bhi API key set nahi hai.\n"
            "Render environment me add karein."
        )
        return

    keyboard = [
        [InlineKeyboardButton(AVAILABLE_MODELS[p]["name"], callback_data=f"prov:{p}")]
        for p in active
    ]
    await update.message.reply_text(
        "🔌 *Step 1 — Provider chunein:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def cb_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data.split(":", 1)[1]

    models = AVAILABLE_MODELS.get(provider, {}).get("models", {})
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"model:{provider}:{mid}")]
        for mid, label in models.items()
    ]
    keyboard.append(
        [InlineKeyboardButton("⬅️ Wapas Providers", callback_data="back:providers")]
    )
    pname = AVAILABLE_MODELS[provider]["name"]
    await query.edit_message_text(
        f"🤖 *Step 2 — {pname} ka model chunein:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def cb_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, provider, model_id = query.data.split(":", 2)

    state = get_state(query.from_user.id)
    state["provider"] = provider
    state["model"] = model_id
    state["history"] = []  # clear history on model change

    label = get_model_label(provider, model_id)
    pname = AVAILABLE_MODELS.get(provider, {}).get("name", provider)
    await query.edit_message_text(
        f"✅ *Model set!*\n\n"
        f"Provider : {pname}\n"
        f"Model    : `{label}`\n\n"
        f"_History clear kar di gayi._",
        parse_mode="Markdown",
    )


async def cb_back_providers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    active = get_active_providers()
    keyboard = [
        [InlineKeyboardButton(AVAILABLE_MODELS[p]["name"], callback_data=f"prov:{p}")]
        for p in active
    ]
    await query.edit_message_text(
        "🔌 *Step 1 — Provider chunein:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────
#  MESSAGE HANDLER  — main chat logic
# ─────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    user_text = update.message.text.strip()

    # Show typing indicator
    await ctx.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Append user message to history
    state["history"].append({"role": "user", "content": user_text})

    try:
        reply = await get_ai_response(
            provider=state["provider"],
            model=state["model"],
            messages=state["history"],
        )
        # Append assistant reply
        state["history"].append({"role": "assistant", "content": reply})

        # Keep history within limit
        if len(state["history"]) > config.MAX_HISTORY:
            state["history"] = state["history"][-config.MAX_HISTORY:]

        # Telegram message length limit is 4096 chars
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i: i + 4000])
        else:
            await update.message.reply_text(reply)

    except Exception as exc:
        logger.error("AI error: %s", exc, exc_info=True)
        # Remove the failed user message from history
        if state["history"] and state["history"][-1]["role"] == "user":
            state["history"].pop()
        await update.message.reply_text(
            f"❌ *Error:* `{exc}`\n\n"
            "Dusra model try karein → /model",
            parse_mode="Markdown",
        )


# ─────────────────────────────────────────────
#  APPLICATION SETUP
# ─────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("models", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear",  cmd_clear))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(cb_provider,       pattern=r"^prov:"))
    app.add_handler(CallbackQueryHandler(cb_model,          pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(cb_back_providers, pattern=r"^back:providers$"))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var not set!")

    app = build_app()

    if config.WEBHOOK_URL:
        # ── WEBHOOK MODE (Render / any HTTPS host) ──────────────────
        # python-telegram-bot starts its own aiohttp server on PORT.
        # Render detects the open port → no "No ports detected" error.
        webhook_path = "webhook"
        full_webhook_url = f"{config.WEBHOOK_URL.rstrip('/')}/{webhook_path}"

        logger.info("Starting WEBHOOK mode on port %s → %s", config.PORT, full_webhook_url)

        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            # Render health-check hits "/" — PTB serves 200 OK there automatically
        )
    else:
        # ── POLLING MODE (local development) ────────────────────────
        logger.info("Starting POLLING mode (local dev)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

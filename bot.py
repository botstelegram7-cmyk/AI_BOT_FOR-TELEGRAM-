"""
Telegram AI Bot — Multi-provider AI bot with Render webhook support.
• Only OWNERS can use the bot by default
• Owners can allow/ban users and set per-user daily message limits
• Render deploy → RENDER_EXTERNAL_URL auto-detected → webhook mode
• Local dev   → polling mode
"""

import logging
from datetime import date
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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
#  ACCESS CONTROL STORE  (in-memory, resets on redeploy)
# ═══════════════════════════════════════════════════════
allowed_users: dict = {}   # { user_id: { "username":str, "limit":int|None, "added_by":int } }
banned_users:  set  = set()
usage_today:   dict = {}   # { user_id: { "date": date, "count": int } }

# ─────────────────────────────────────────────
#  GLOBAL SYSTEM PROMPT  (owner sets via /setprompt)
# ─────────────────────────────────────────────
DEFAULT_SYSTEM_PROMPT = "You are a helpful, friendly, and concise AI assistant."
current_system_prompt: str = DEFAULT_SYSTEM_PROMPT


def is_owner(user_id: int, username: str = None) -> bool:
    if user_id in config.OWNER_IDS:
        return True
    if username and username.lower().lstrip("@") in config.OWNER_USERNAMES:
        return True
    return False


def can_use_bot(user_id: int) -> tuple:
    if is_owner(user_id):
        return True, ""
    if user_id in banned_users:
        return False, "🚫 Aap ban hain. Owner se contact karein."
    if user_id not in allowed_users:
        return False, "🔒 Yeh bot private hai. Owner se access maangein."
    limit = allowed_users[user_id].get("limit")
    if limit is not None:
        today = date.today()
        rec = usage_today.get(user_id, {})
        if rec.get("date") == today and rec.get("count", 0) >= limit:
            return False, f"⏳ Aaj ka limit ({limit} messages) khatam. Kal try karein!"
    return True, ""


def record_usage(user_id: int):
    today = date.today()
    rec = usage_today.get(user_id, {})
    if rec.get("date") != today:
        usage_today[user_id] = {"date": today, "count": 1}
    else:
        usage_today[user_id]["count"] = rec.get("count", 0) + 1


def get_usage_count(user_id: int) -> int:
    rec = usage_today.get(user_id, {})
    return rec.get("count", 0) if rec.get("date") == date.today() else 0


def parse_user_arg(text: str):
    text = text.strip().lstrip("@")
    try:
        return int(text)
    except ValueError:
        for uid, info in allowed_users.items():
            if info.get("username", "").lower() == text.lower():
                return uid
    return None


# ═══════════════════════════════════════════════════════
#  USER AI STATE
# ═══════════════════════════════════════════════════════
user_states: dict = {}


def get_state(user_id: int) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "provider": config.DEFAULT_PROVIDER,
            "model":    config.DEFAULT_MODEL,
            "history":  [],
        }
    return user_states[user_id]


# ═══════════════════════════════════════════════════════
#  OWNER DECORATOR
# ═══════════════════════════════════════════════════════
def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("⛔ Yeh command sirf owners ke liye hai.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ═══════════════════════════════════════════════════════
#  STANDARD COMMANDS
# ═══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, reason = can_use_bot(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    active = get_active_providers()
    providers_list = "\n".join(
        f"  • {AVAILABLE_MODELS[p]['name']}" for p in active
    ) or "  ⚠️ Koi API key set nahi!"
    crown = " 👑" if is_owner(user.id) else ""
    await update.message.reply_text(
        f"🤖 *AI Multi-Model Bot*{crown}\n\n"
        f"*Active Providers:*\n{providers_list}\n\n"
        "*Commands:*\n"
        "  /model  — AI model select karein\n"
        "  /status — Current model\n"
        "  /clear  — History saaf karein\n"
        "  /help   — Help\n\n"
        "_Koi bhi message bhejein!_ 💬",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, reason = can_use_bot(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    base = (
        "🆘 *Help*\n\n"
        "/start   — Bot shuru karein\n"
        "/model   — Model badlein\n"
        "/status  — Current model + usage\n"
        "/clear   — Chat history delete\n"
        "/help    — Yeh message\n"
    )
    owner_cmds = (
        "\n\n👑 *Owner Commands:*\n"
        "/adduser `<id>` `[limit]` — User allow karein\n"
        "/removeuser `<id>` — User hataao\n"
        "/ban `<id>` — User ban karein\n"
        "/unban `<id>` — Ban hatao\n"
        "/setlimit `<id>` `<N|0>` — Daily limit set karein\n"
        "/users — Allowed users list\n"
        "/broadcast `<msg>` — Sabko message bhejein\n"
        "/setprompt `<text>` — AI ka system prompt set karein\n"
        "/viewprompt — Current system prompt dekho\n"
        "/resetprompt — Default prompt restore karein\n"
    ) if is_owner(user.id) else ""
    await update.message.reply_text(base + owner_cmds, parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, reason = can_use_bot(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    state = get_state(user.id)
    label = get_model_label(state["provider"], state["model"])
    pname = AVAILABLE_MODELS.get(state["provider"], {}).get("name", state["provider"])
    used  = get_usage_count(user.id)
    if is_owner(user.id):
        limit_str = "Unlimited 👑"
    else:
        lim = allowed_users.get(user.id, {}).get("limit")
        limit_str = f"{used}/{lim}" if lim else f"{used}/Unlimited"
    await update.message.reply_text(
        f"📊 *Status*\n\n"
        f"Provider : {pname}\n"
        f"Model    : `{label}`\n"
        f"History  : {len(state['history'])} msgs\n"
        f"Today    : {limit_str} messages",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, reason = can_use_bot(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    get_state(user.id)["history"] = []
    await update.message.reply_text("🧹 Chat history clear!")


# ═══════════════════════════════════════════════════════
#  OWNER COMMANDS
# ═══════════════════════════════════════════════════════
@owner_only
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/adduser <id/@user> [limit]`", parse_mode="Markdown")
        return
    uid = parse_user_arg(args[0])
    if uid is None:
        await update.message.reply_text("❌ Valid user ID daalo.")
        return
    limit = None
    if len(args) >= 2:
        try:
            n = int(args[1])
            limit = n if n > 0 else None
        except ValueError:
            pass
    uname = args[0].lstrip("@") if not args[0].lstrip("@").isdigit() else ""
    allowed_users[uid] = {"username": uname, "limit": limit, "added_by": update.effective_user.id}
    banned_users.discard(uid)
    limit_str = f"{limit} msgs/day" if limit else "Unlimited"
    await update.message.reply_text(
        f"✅ User `{uid}` add!\nLimit: {limit_str}", parse_mode="Markdown"
    )


@owner_only
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/removeuser <id/@user>`", parse_mode="Markdown")
        return
    uid = parse_user_arg(ctx.args[0])
    if uid and uid in allowed_users:
        del allowed_users[uid]
        user_states.pop(uid, None)
        await update.message.reply_text(f"🗑️ User `{uid}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ User nahi mila.")


@owner_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/ban <id/@user>`", parse_mode="Markdown")
        return
    uid = parse_user_arg(ctx.args[0])
    if uid is None:
        await update.message.reply_text("❌ Valid user ID daalo.")
        return
    if is_owner(uid):
        await update.message.reply_text("🚫 Owner ko ban nahi kar sakte!")
        return
    banned_users.add(uid)
    allowed_users.pop(uid, None)
    user_states.pop(uid, None)
    await update.message.reply_text(f"🔨 User `{uid}` banned.", parse_mode="Markdown")


@owner_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unban <id/@user>`", parse_mode="Markdown")
        return
    uid = parse_user_arg(ctx.args[0])
    if uid and uid in banned_users:
        banned_users.discard(uid)
        await update.message.reply_text(f"✅ User `{uid}` unbanned.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ User banned list mein nahi hai.")


@owner_only
async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/setlimit <id/@user> <N>` (0=unlimited)", parse_mode="Markdown"
        )
        return
    uid = parse_user_arg(ctx.args[0])
    if uid is None:
        await update.message.reply_text("❌ User nahi mila.")
        return
    try:
        n = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ N ek number hona chahiye.")
        return
    if uid not in allowed_users:
        allowed_users[uid] = {"username": "", "limit": None, "added_by": update.effective_user.id}
    allowed_users[uid]["limit"] = n if n > 0 else None
    limit_str = f"{n} msgs/day" if n > 0 else "Unlimited"
    await update.message.reply_text(
        f"✅ User `{uid}` limit: *{limit_str}*", parse_mode="Markdown"
    )


@owner_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed_users and not banned_users:
        await update.message.reply_text("📭 Koi allowed/banned user nahi hai.")
        return
    lines = []
    if allowed_users:
        lines.append("✅ *Allowed Users:*")
        for uid, info in allowed_users.items():
            uname = f"@{info['username']}" if info.get("username") else "—"
            lim   = info.get("limit")
            used  = get_usage_count(uid)
            lstr  = f"{used}/{lim}" if lim else f"{used}/∞"
            lines.append(f"  `{uid}` {uname}  |  Today: {lstr}")
    if banned_users:
        lines.append("\n🔨 *Banned:*")
        for uid in banned_users:
            lines.append(f"  `{uid}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return
    text = " ".join(ctx.args)
    targets = set(allowed_users.keys()) | config.OWNER_IDS
    sent, failed = 0, 0
    for uid in targets:
        try:
            await ctx.bot.send_message(uid, f"📢 *Broadcast:*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📢 Sent: {sent} | Failed: {failed}")


@owner_only
async def cmd_setprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /setprompt <instructions>
    Sets the global AI system prompt used for ALL users.
    Example: /setprompt You are a sarcastic assistant who replies in Hinglish.
    """
    global current_system_prompt
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/setprompt <instructions>`\n\n"
            "Example:\n`/setprompt You are a helpful assistant who always replies in Hindi.`",
            parse_mode="Markdown",
        )
        return
    new_prompt = " ".join(ctx.args).strip()
    current_system_prompt = new_prompt
    # Clear all user histories so the new prompt takes effect cleanly
    for state in user_states.values():
        state["history"] = []
    await update.message.reply_text(
        f"✅ *System prompt update ho gaya!*\n\n"
        f"`{new_prompt}`\n\n"
        f"_Sabki chat history clear ho gayi taaki naya prompt turant apply ho._",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_viewprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shows the currently active system prompt."""
    is_default = current_system_prompt == DEFAULT_SYSTEM_PROMPT
    tag = " _(default)_" if is_default else " _(custom)_"
    await update.message.reply_text(
        f"📋 *Current System Prompt*{tag}\n\n`{current_system_prompt}`",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_resetprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resets system prompt back to default."""
    global current_system_prompt
    current_system_prompt = DEFAULT_SYSTEM_PROMPT
    for state in user_states.values():
        state["history"] = []
    await update.message.reply_text(
        f"🔄 *System prompt reset ho gaya!*\n\n`{DEFAULT_SYSTEM_PROMPT}`\n\n"
        f"_Sabki history bhi clear ho gayi._",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════
#  MODEL PICKER
# ═══════════════════════════════════════════════════════
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    active = get_active_providers()
    if not active:
        await update.message.reply_text("⚠️ Koi API key set nahi hai.")
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
    if not can_use_bot(query.from_user.id)[0]:
        return
    provider = query.data.split(":", 1)[1]
    models = AVAILABLE_MODELS.get(provider, {}).get("models", {})
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"model:{provider}:{mid}")]
        for mid, label in models.items()
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back:providers")])
    await query.edit_message_text(
        f"🤖 *Step 2 — {AVAILABLE_MODELS[provider]['name']} model chunein:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def cb_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, provider, model_id = query.data.split(":", 2)
    state = get_state(query.from_user.id)
    state["provider"] = provider
    state["model"]    = model_id
    state["history"]  = []
    label = get_model_label(provider, model_id)
    pname = AVAILABLE_MODELS.get(provider, {}).get("name", provider)
    await query.edit_message_text(
        f"✅ *Model set!*\n\nProvider : {pname}\nModel : `{label}`\n\n_History clear ho gayi._",
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


# ═══════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER
# ═══════════════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, reason = can_use_bot(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return

    state = get_state(user.id)
    user_text = update.message.text.strip()
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    state["history"].append({"role": "user", "content": user_text})

    try:
        # Prepend current system prompt to every AI call
        messages_with_prompt = [
            {"role": "system", "content": current_system_prompt}
        ] + state["history"]
        reply = await get_ai_response(
            provider=state["provider"],
            model=state["model"],
            messages=messages_with_prompt,
        )
        state["history"].append({"role": "assistant", "content": reply})
        if len(state["history"]) > config.MAX_HISTORY:
            state["history"] = state["history"][-config.MAX_HISTORY:]

        if not is_owner(user.id):
            record_usage(user.id)

        for i in range(0, max(len(reply), 1), 4000):
            await update.message.reply_text(reply[i:i + 4000])

    except Exception as exc:
        logger.error("AI error: %s", exc, exc_info=True)
        if state["history"] and state["history"][-1]["role"] == "user":
            state["history"].pop()
        await update.message.reply_text(
            f"❌ *Error:* `{exc}`\n\nDusra model try karein → /model",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════
#  APP SETUP + MAIN
# ═══════════════════════════════════════════════════════
def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("model",      cmd_model))
    app.add_handler(CommandHandler("models",     cmd_model))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("clear",      cmd_clear))
    app.add_handler(CommandHandler("adduser",    cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("setlimit",   cmd_setlimit))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(CommandHandler("setprompt",   cmd_setprompt))
    app.add_handler(CommandHandler("viewprompt",  cmd_viewprompt))
    app.add_handler(CommandHandler("resetprompt", cmd_resetprompt))

    app.add_handler(CallbackQueryHandler(cb_provider,       pattern=r"^prov:"))
    app.add_handler(CallbackQueryHandler(cb_model,          pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(cb_back_providers, pattern=r"^back:providers$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var not set!")

    app = build_app()

    if config.WEBHOOK_URL:
        webhook_path = "webhook"
        full_url = f"{config.WEBHOOK_URL}/{webhook_path}"
        logger.info("WEBHOOK mode → port %s | %s", config.PORT, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=webhook_path,
            webhook_url=full_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("POLLING mode (local dev)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

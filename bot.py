"""
Telegram AI Bot — v4 Final
• 8 AI Providers: OpenAI, Anthropic, Grok, Gemini, Groq, SambaNova, OpenRouter, NVIDIA
• Tinder-style character selector with relationship levels
• /menu — beautiful inline keyboard hub
• /profile — user profile system
• /stats  — owner analytics panel
• MongoDB persistence (optional)
• Owner access control, daily limits, /setprompt, /broadcast
• Render: RENDER_EXTERNAL_URL auto-detected → webhook | Local: polling
"""

import logging
from datetime import date

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import config
import database as db
from ai_client import AVAILABLE_MODELS, get_ai_response, get_active_providers, get_model_label
from characters import CHARACTERS, get_character, get_char_pic, build_card_text

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful, friendly, and concise AI assistant."
current_system_prompt: str = DEFAULT_SYSTEM_PROMPT

# ─── per-user AI state (in-memory, fast) ────────────────────────
user_states: dict = {}

def get_state(uid: int) -> dict:
    if uid not in user_states:
        user_states[uid] = {
            "provider":   config.DEFAULT_PROVIDER,
            "model":      config.DEFAULT_MODEL,
            "history":    [],
            "mode":       "ai",        # "ai" | "character"
            "character":  None,
            "browse_idx": 0,
        }
    return user_states[uid]


# ═══════════════════════════════════════════════════════
#  ACCESS HELPERS
# ═══════════════════════════════════════════════════════
def is_owner(uid: int, username: str = None) -> bool:
    if uid in config.OWNER_IDS:
        return True
    if username and username.lower().lstrip("@") in config.OWNER_USERNAMES:
        return True
    return False

def can_use_bot(uid: int) -> tuple:
    if is_owner(uid):
        return True, ""
    allowed = db.get_allowed_users()
    banned  = db.get_banned_users()
    if uid in banned:
        return False, "🚫 Aap ban hain. Owner se contact karein."
    if uid not in allowed:
        return False, "🔒 Yeh bot private hai. Owner se access maangein."
    limit = allowed[uid].get("limit")
    if limit and db.get_usage_today(uid) >= limit:
        return False, f"⏳ Aaj ka limit ({limit} msgs) khatam. Kal try karein!"
    return True, ""

def parse_uid(text: str):
    text = text.strip().lstrip("@")
    try:
        return int(text)
    except ValueError:
        for uid, info in db.get_allowed_users().items():
            if info.get("username", "").lower() == text.lower():
                return uid
    return None

def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("⛔ Sirf owners ke liye.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ═══════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    crown   = " 👑" if is_owner(user.id) else ""
    active  = get_active_providers()
    plist   = " · ".join(AVAILABLE_MODELS[p]["name"] for p in active) or "⚠️ Koi API key set nahi"

    caption = (
        f"🤖 *AI Bot mein Swagat Hai!*{crown}\n\n"
        f"*Active Providers:*\n{plist}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💞 /meet — AI Companions se milo\n"
        f"📋 /menu — Main Menu\n"
        f"🔌 /model — AI Model chunein\n"
        f"📊 /status — Status dekho\n"
        f"👤 /profile — Apna profile set karo\n"
        f"🆘 /help — Help\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Koi bhi message bhejein — AI ready hai!_ 💬"
    )

    # Single message: media + caption together
    try:
        if config.WELCOME_VIDEO:
            await update.message.reply_video(
                video=config.WELCOME_VIDEO,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if config.WELCOME_PIC:
            await update.message.reply_photo(
                photo=config.WELCOME_PIC,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    except Exception as e:
        logger.warning("Media send failed: %s", e)

    # Fallback: text only
    await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════
#  /menu  — Beautiful inline hub
# ═══════════════════════════════════════════════════════
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    state = get_state(user.id)
    mode  = state["mode"]

    if mode == "character":
        char = get_character(state["character"])
        mode_line = f"💞 Chatting with *{char['name']}*" if char else "💞 Character mode"
    else:
        lbl       = get_model_label(state["provider"], state["model"])
        mode_line = f"🤖 AI mode — `{lbl}`"

    kb = [
        [
            InlineKeyboardButton("💞 Meet Characters", callback_data="menu:meet"),
            InlineKeyboardButton("🔌 Change Model",    callback_data="menu:model"),
        ],
        [
            InlineKeyboardButton("👤 My Profile",    callback_data="menu:profile"),
            InlineKeyboardButton("📊 My Status",     callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton("🧹 Clear History", callback_data="menu:clear"),
            InlineKeyboardButton("👋 Leave Chat",    callback_data="menu:leave"),
        ],
    ]
    if is_owner(user.id):
        kb.append([
            InlineKeyboardButton("📈 Bot Stats",      callback_data="menu:stats"),
            InlineKeyboardButton("👥 Manage Users",   callback_data="menu:users"),
        ])

    await update.message.reply_text(
        f"📋 *Main Menu*\n\n{mode_line}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    action  = query.data.split(":", 1)[1]

    if action == "meet":
        await query.edit_message_text("💞 Opening character browser...", parse_mode=ParseMode.MARKDOWN)
        await _send_card_new(query.message, uid, 0)
    elif action == "model":
        await query.edit_message_text("🔌 *Model Picker*\n\nProvider chunein:", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=_provider_keyboard())
    elif action == "profile":
        await _show_profile_menu(query.message, uid, edit=False)
    elif action == "status":
        await query.edit_message_text(_status_text(uid), parse_mode=ParseMode.MARKDOWN)
    elif action == "clear":
        get_state(uid)["history"] = []
        await query.edit_message_text("🧹 History clear!", parse_mode=ParseMode.MARKDOWN)
    elif action == "leave":
        state = get_state(uid)
        if state["mode"] == "character":
            char  = get_character(state["character"])
            name  = char["name"] if char else "Character"
            state.update({"mode": "ai", "character": None, "history": []})
            await query.edit_message_text(f"👋 *{name}* se bye-bye!\n\nAb AI mode mein ho.",
                                          parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text("ℹ️ Abhi character mode mein nahi ho.", parse_mode=ParseMode.MARKDOWN)
    elif action == "stats":
        if is_owner(uid):
            await query.edit_message_text(_stats_text(), parse_mode=ParseMode.MARKDOWN)
    elif action == "users":
        if is_owner(uid):
            await query.edit_message_text(_users_text(), parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════
#  /help
# ═══════════════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    base = (
        "🆘 *Help Menu*\n\n"
        "📋 /menu     — Main menu (sab kuch yahan)\n"
        "🤖 /start    — Welcome message\n"
        "💞 /meet     — AI Companions browser\n"
        "👋 /leave    — Character chat se bahar\n"
        "🔌 /model    — AI Model change karo\n"
        "📊 /status   — Current status\n"
        "👤 /profile  — Apna profile\n"
        "🧹 /clear    — Chat history saaf karo\n"
    )
    owner_section = (
        "\n👑 *Owner Commands:*\n"
        "/adduser `<id> [limit]` — Allow user\n"
        "/removeuser `<id>` — Remove user\n"
        "/ban · /unban `<id>` — Ban control\n"
        "/setlimit `<id> <N>` — Daily limit\n"
        "/users — Users list\n"
        "/stats — Bot analytics\n"
        "/broadcast `<msg>` — Sabko message\n"
        "/setprompt · /viewprompt · /resetprompt\n"
    ) if is_owner(user.id) else ""

    await update.message.reply_text(base + owner_section, parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════
#  STATUS HELPER
# ═══════════════════════════════════════════════════════
def _status_text(uid: int) -> str:
    state = get_state(uid)
    used  = db.get_usage_today(uid)
    total = db.get_total_messages(uid)

    if state["mode"] == "character":
        char    = get_character(state["character"])
        cname   = char["name"] if char else "Unknown"
        pts     = db.get_relationship(uid, state["character"])
        label   = db.get_relationship_label(pts)
        mode_str = f"💞 Character: *{cname}*\nRelationship: {label} ({pts} pts)"
    else:
        lbl      = get_model_label(state["provider"], state["model"])
        pname    = AVAILABLE_MODELS.get(state["provider"], {}).get("name", state["provider"])
        mode_str = f"🤖 AI Mode\nProvider: {pname}\nModel: `{lbl}`"

    if is_owner(uid):
        limit_str = "Unlimited 👑"
    else:
        lim = db.get_allowed_users().get(uid, {}).get("limit")
        limit_str = f"{used}/{lim}" if lim else f"{used}/Unlimited"

    return (
        f"📊 *Your Status*\n\n"
        f"{mode_str}\n\n"
        f"Today: {limit_str} messages\n"
        f"Total: {total} messages\n"
        f"History: {len(state['history'])} msgs"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    await update.message.reply_text(_status_text(user.id), parse_mode=ParseMode.MARKDOWN)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    get_state(user.id)["history"] = []
    await update.message.reply_text("🧹 History clear!")


# ═══════════════════════════════════════════════════════
#  /profile
# ═══════════════════════════════════════════════════════
async def _show_profile_menu(message, uid: int, edit=False):
    prof = db.get_profile(uid)
    text = (
        f"👤 *Your Profile*\n\n"
        f"Name : {prof.get('name', '—')}\n"
        f"Age  : {prof.get('age', '—')}\n"
        f"Bio  : {prof.get('bio', '—')}\n"
        f"Mood : {prof.get('mood', '—')}\n\n"
        f"_Use buttons to edit:_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set Name",  callback_data="profile:setname"),
         InlineKeyboardButton("✏️ Set Age",   callback_data="profile:setage")],
        [InlineKeyboardButton("✏️ Set Bio",   callback_data="profile:setbio"),
         InlineKeyboardButton("😊 Set Mood",  callback_data="profile:setmood")],
        [InlineKeyboardButton("❌ Close",     callback_data="profile:close")],
    ])
    if edit:
        try:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    # If args given: /profile name Alex
    if ctx.args and len(ctx.args) >= 2:
        field = ctx.args[0].lower()
        value = " ".join(ctx.args[1:])
        prof  = db.get_profile(user.id)
        if field in ("name", "age", "bio", "mood"):
            prof[field] = value
            await db.save_profile(user.id, prof)
            await update.message.reply_text(f"✅ {field.capitalize()} updated: *{value}*",
                                             parse_mode=ParseMode.MARKDOWN)
            return
    await _show_profile_menu(update.message, user.id)


async def cb_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_text("👤 Profile menu closed.")
        return

    field_map = {
        "setname": ("name", "apna naam"),
        "setage":  ("age",  "apni age"),
        "setbio":  ("bio",  "apni bio"),
        "setmood": ("mood", "apna mood (e.g. Happy 😊, Sad 😢)"),
    }
    if action in field_map:
        field, prompt_text = field_map[action]
        ctx.user_data["profile_edit_field"] = field
        ctx.user_data["profile_edit_uid"]   = uid
        await query.edit_message_text(
            f"✏️ *{field.capitalize()} set karo*\n\n{prompt_text} likhein:",
            parse_mode=ParseMode.MARKDOWN,
        )


async def handle_profile_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if we handled a profile edit, False otherwise."""
    field = ctx.user_data.get("profile_edit_field")
    if not field:
        return False
    uid   = update.effective_user.id
    value = update.message.text.strip()
    prof  = db.get_profile(uid)
    prof[field] = value
    await db.save_profile(uid, prof)
    ctx.user_data.pop("profile_edit_field", None)
    ctx.user_data.pop("profile_edit_uid", None)
    await update.message.reply_text(
        f"✅ *{field.capitalize()}* set ho gaya: *{value}*\n\n/profile se dekho!",
        parse_mode=ParseMode.MARKDOWN,
    )
    return True


# ═══════════════════════════════════════════════════════
#  TINDER-STYLE CHARACTER BROWSER
# ═══════════════════════════════════════════════════════
def _browse_keyboard(idx: int, char_id: str) -> InlineKeyboardMarkup:
    total = len(CHARACTERS)
    nav   = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"browse:prev:{idx}"))
    nav.append(InlineKeyboardButton(f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"browse:next:{idx}"))

    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton(f"💬 Chat with {CHARACTERS[idx]['name']}", callback_data=f"choose:{char_id}")],
        [InlineKeyboardButton("❌ Close", callback_data="browse:cancel")],
    ])


async def _send_card_new(message, uid: int, idx: int):
    char = CHARACTERS[idx]
    pic  = get_char_pic(char)
    text = build_card_text(char, idx, len(CHARACTERS))
    kb   = _browse_keyboard(idx, char["id"])
    get_state(uid)["browse_idx"] = idx

    if pic:
        await message.reply_photo(photo=pic, caption=text,
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_meet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    await _send_card_new(update.message, user.id, 0)


async def cb_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]

    if action == "cancel":
        await query.edit_message_text("👋 Browse band kiya. /meet se dobara shuru karo.")
        return

    idx = int(parts[2])
    new_idx = (idx + 1) % len(CHARACTERS) if action == "next" else max(0, idx - 1)

    char = CHARACTERS[new_idx]
    pic  = get_char_pic(char)
    text = build_card_text(char, new_idx, len(CHARACTERS))
    kb   = _browse_keyboard(new_idx, char["id"])
    get_state(uid)["browse_idx"] = new_idx

    try:
        if pic:
            await query.edit_message_media(
                media=InputMediaPhoto(media=pic, caption=text, parse_mode=ParseMode.MARKDOWN),
                reply_markup=kb,
            )
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except BadRequest:
        try:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer("💞 Connected!")
    uid     = query.from_user.id
    char_id = query.data.split(":", 1)[1]
    char    = get_character(char_id)
    if not char:
        await query.edit_message_text("❌ Character nahi mila.")
        return

    state = get_state(uid)
    state.update({"mode": "character", "character": char_id, "history": []})

    pts   = db.get_relationship(uid, char_id)
    rlbl  = db.get_relationship_label(pts)
    prof  = db.get_profile(uid)
    uname = prof.get("name") or query.from_user.first_name

    greeting = (
        f"💞 *{char['name']}* se connected!\n\n"
        f"_{char['intro']}_\n\n"
        f"Relationship: {rlbl}\n\n"
        f"_/leave — wapas jaao | /menu — options_"
    )

    try:
        if get_char_pic(char):
            await query.edit_message_caption(caption=greeting, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(greeting, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await ctx.bot.send_message(uid, greeting, parse_mode=ParseMode.MARKDOWN)


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    state = get_state(user.id)
    if state.get("mode") == "character":
        char = get_character(state["character"])
        name = char["name"] if char else "Character"
        state.update({"mode": "ai", "character": None, "history": []})
        await update.message.reply_text(
            f"👋 *{name}* se bye!\n\nAb normal AI mode mein ho. /meet se dobara milo!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("ℹ️ Abhi character mode mein nahi ho.")


# ═══════════════════════════════════════════════════════
#  MODEL PICKER
# ═══════════════════════════════════════════════════════
def _provider_keyboard() -> InlineKeyboardMarkup:
    active = get_active_providers()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(AVAILABLE_MODELS[p]["name"], callback_data=f"prov:{p}")]
        for p in active
    ])


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    if not get_active_providers():
        await update.message.reply_text("⚠️ Koi API key set nahi.")
        return
    await update.message.reply_text(
        "🔌 *Provider chunein:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_provider_keyboard(),
    )


async def cb_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    provider = query.data.split(":", 1)[1]
    models   = AVAILABLE_MODELS.get(provider, {}).get("models", {})
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"model:{provider}:{mid}")]
         for mid, label in models.items()] +
        [[InlineKeyboardButton("⬅️ Back", callback_data="back:providers")]]
    )
    await query.edit_message_text(
        f"🤖 *{AVAILABLE_MODELS[provider]['name']}* — Model chunein:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cb_model_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query              = update.callback_query
    await query.answer()
    _, provider, mid   = query.data.split(":", 2)
    state              = get_state(query.from_user.id)
    state.update({"provider": provider, "model": mid, "history": [],
                  "mode": "ai", "character": None})
    label = get_model_label(provider, mid)
    pname = AVAILABLE_MODELS.get(provider, {}).get("name", provider)
    await query.edit_message_text(
        f"✅ *Model set!*\n\nProvider : {pname}\nModel : `{label}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_back_providers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔌 *Provider chunein:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_provider_keyboard(),
    )


# ═══════════════════════════════════════════════════════
#  OWNER COMMANDS
# ═══════════════════════════════════════════════════════
def _stats_text() -> str:
    s = db.get_global_stats()
    return (
        f"📈 *Bot Stats*\n\n"
        f"👥 Total Users    : {s['total_users']}\n"
        f"🔨 Banned         : {s['banned']}\n"
        f"💬 Msgs Today     : {s['msgs_today']}\n"
        f"📨 Total Msgs     : {s['total_msgs']}\n"
        f"🗄️ MongoDB        : {'✅ Connected' if s['mongo_enabled'] else '⚠️ In-memory only'}\n"
    )

def _users_text() -> str:
    allowed = db.get_allowed_users()
    banned  = db.get_banned_users()
    if not allowed and not banned:
        return "📭 Koi user nahi hai abhi."
    lines = []
    if allowed:
        lines.append("✅ *Allowed:*")
        for uid, info in list(allowed.items())[:20]:
            uname = f"@{info['username']}" if info.get("username") else "—"
            lim   = info.get("limit")
            used  = db.get_usage_today(uid)
            lines.append(f"  `{uid}` {uname} | {used}/{lim or '∞'}")
    if banned:
        lines.append("\n🔨 *Banned:*")
        for uid in list(banned)[:10]:
            lines.append(f"  `{uid}`")
    return "\n".join(lines)


@owner_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_stats_text(), parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/adduser <id/@user> [limit]`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    if uid is None:
        await update.message.reply_text("❌ Valid ID daalo.")
        return
    limit = None
    if len(ctx.args) >= 2:
        try:
            n = int(ctx.args[1]); limit = n if n > 0 else None
        except ValueError:
            pass
    uname = ctx.args[0].lstrip("@") if not ctx.args[0].lstrip("@").isdigit() else ""
    await db.add_allowed_user(uid, uname, limit, update.effective_user.id)
    await update.message.reply_text(
        f"✅ `{uid}` added! Limit: {f'{limit} msgs/day' if limit else 'Unlimited'}",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/removeuser <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    if uid and uid in db.get_allowed_users():
        await db.remove_allowed_user(uid)
        user_states.pop(uid, None)
        await update.message.reply_text(f"🗑️ `{uid}` removed.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ User nahi mila.")


@owner_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/ban <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    if uid is None:
        await update.message.reply_text("❌ Valid ID daalo.")
        return
    if is_owner(uid):
        await update.message.reply_text("🚫 Owner ko ban nahi kar sakte!")
        return
    await db.ban_user(uid)
    user_states.pop(uid, None)
    await update.message.reply_text(f"🔨 `{uid}` banned.", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unban <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    if uid and uid in db.get_banned_users():
        await db.unban_user(uid)
        await update.message.reply_text(f"✅ `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Ban list mein nahi hai.")


@owner_only
async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/setlimit <id> <N>` (0=unlimited)", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    if uid is None:
        await update.message.reply_text("❌ User nahi mila.")
        return
    try:
        n = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Number daalo.")
        return
    await db.set_user_limit(uid, n if n > 0 else None)
    await update.message.reply_text(
        f"✅ `{uid}` limit: *{f'{n} msgs/day' if n > 0 else 'Unlimited'}*",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_users_text(), parse_mode=ParseMode.MARKDOWN)


@owner_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast <msg>`", parse_mode=ParseMode.MARKDOWN)
        return
    text    = " ".join(ctx.args)
    targets = set(db.get_allowed_users().keys()) | config.OWNER_IDS
    sent = failed = 0
    for uid in targets:
        try:
            await ctx.bot.send_message(uid, f"📢 *Broadcast:*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📢 Sent: {sent} | Failed: {failed}")


@owner_only
async def cmd_setprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global current_system_prompt
    if not ctx.args:
        await update.message.reply_text("Usage: `/setprompt <text>`", parse_mode=ParseMode.MARKDOWN)
        return
    current_system_prompt = " ".join(ctx.args)
    for s in user_states.values():
        s["history"] = []
    await update.message.reply_text(
        f"✅ *Prompt updated!*\n\n`{current_system_prompt}`\n\n_Sabki history clear._",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def cmd_viewprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tag = " _(default)_" if current_system_prompt == DEFAULT_SYSTEM_PROMPT else " _(custom)_"
    await update.message.reply_text(
        f"📋 *System Prompt*{tag}\n\n`{current_system_prompt}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def cmd_resetprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global current_system_prompt
    current_system_prompt = DEFAULT_SYSTEM_PROMPT
    for s in user_states.values():
        s["history"] = []
    await update.message.reply_text("🔄 Prompt reset to default.", parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER
# ═══════════════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    # Profile edit in progress?
    if await handle_profile_edit(update, ctx):
        return

    state     = get_state(user.id)
    user_text = update.message.text.strip()
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Build system prompt
    if state["mode"] == "character":
        char       = get_character(state["character"])
        sys_prompt = char["prompt"] if char else current_system_prompt
        # Inject user profile into character context
        prof = db.get_profile(user.id)
        if prof.get("name"):
            sys_prompt += f"\n\nUser's name is {prof['name']}."
        if prof.get("bio"):
            sys_prompt += f" They describe themselves as: {prof['bio']}."
    else:
        sys_prompt = current_system_prompt

    state["history"].append({"role": "user", "content": user_text})
    msgs = [{"role": "system", "content": sys_prompt}] + state["history"]

    try:
        reply = await get_ai_response(
            provider=state["provider"],
            model=state["model"],
            messages=msgs,
        )
        state["history"].append({"role": "assistant", "content": reply})
        if len(state["history"]) > config.MAX_HISTORY:
            state["history"] = state["history"][-config.MAX_HISTORY:]

        # Usage tracking
        if not is_owner(user.id):
            db.record_usage(user.id)
        else:
            db.record_usage(user.id)  # track owners too for stats

        # Relationship point for character chats
        if state["mode"] == "character" and state["character"]:
            await db.add_relationship_point(user.id, state["character"])

        await db.persist_usage(user.id)

        # Send reply (split if >4000 chars)
        for i in range(0, max(len(reply), 1), 4000):
            await update.message.reply_text(reply[i:i + 4000])

    except Exception as exc:
        logger.error("AI error [%s/%s]: %s", state["provider"], state["model"], exc, exc_info=True)
        if state["history"] and state["history"][-1]["role"] == "user":
            state["history"].pop()
        await update.message.reply_text(
            f"❌ *Error:* `{exc}`\n\nDusra model try karein → /model",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════
#  APP SETUP + MAIN
# ═══════════════════════════════════════════════════════
def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Standard
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("menu",        cmd_menu))
    app.add_handler(CommandHandler("model",       cmd_model))
    app.add_handler(CommandHandler("models",      cmd_model))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(CommandHandler("meet",        cmd_meet))
    app.add_handler(CommandHandler("leave",       cmd_leave))
    app.add_handler(CommandHandler("profile",     cmd_profile))

    # Owner
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("adduser",     cmd_adduser))
    app.add_handler(CommandHandler("removeuser",  cmd_removeuser))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("setlimit",    cmd_setlimit))
    app.add_handler(CommandHandler("users",       cmd_users))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(CommandHandler("setprompt",   cmd_setprompt))
    app.add_handler(CommandHandler("viewprompt",  cmd_viewprompt))
    app.add_handler(CommandHandler("resetprompt", cmd_resetprompt))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_menu,           pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(cb_browse,         pattern=r"^browse:"))
    app.add_handler(CallbackQueryHandler(cb_choose,         pattern=r"^choose:"))
    app.add_handler(CallbackQueryHandler(cb_profile,        pattern=r"^profile:"))
    app.add_handler(CallbackQueryHandler(cb_provider,       pattern=r"^prov:"))
    app.add_handler(CallbackQueryHandler(cb_model_select,   pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(cb_back_providers, pattern=r"^back:providers$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


async def post_init(app: Application):
    """Called after app is initialized — init DB."""
    await db.init_db()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set!")

    app = build_app()
    app.post_init = post_init

    if config.WEBHOOK_URL:
        webhook_path = "webhook"
        full_url     = f"{config.WEBHOOK_URL}/{webhook_path}"
        logger.info("WEBHOOK → port %s | %s", config.PORT, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=webhook_path,
            webhook_url=full_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("POLLING mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
Telegram AI Bot — Mega Edition
• 8 AI Providers with ChatGPT-like streaming
• Tinder character browser + mood + relationship + gifts
• Games: Truth/Dare, 20Q, Story, Horoscope, Spin
• Leaderboard + Badges
• User activity log, /finduser, auto-ban spam
• Scheduled broadcasts
• Mini Web Dashboard (FastAPI)
• Owner panel: stats, users, prompts, schedules
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Update, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import config
import database as db
import games as gm
from ai_client import (
    AVAILABLE_MODELS, get_active_providers,
    get_model_label, stream_ai_response,
)
from characters import CHARACTERS, build_card_text, get_char_pic, get_character

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful, friendly, and concise AI assistant."
current_system_prompt: str = DEFAULT_SYSTEM_PROMPT

# Per-user AI state
user_states: dict = {}

def get_state(uid: int) -> dict:
    if uid not in user_states:
        user_states[uid] = {
            "provider":   config.DEFAULT_PROVIDER,
            "model":      config.DEFAULT_MODEL,
            "history":    [],
            "mode":       "ai",        # "ai" | "character" | "game"
            "character":  None,
            "game":       None,        # active game type
            "browse_idx": 0,
        }
    return user_states[uid]


# ════════════════════════════════════════════════════════════
#  ACCESS HELPERS
# ════════════════════════════════════════════════════════════
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


# ════════════════════════════════════════════════════════════
#  STREAMING SEND — ChatGPT-like typing effect
# ════════════════════════════════════════════════════════════
async def send_streaming(update: Update, provider: str, model: str, messages: list) -> str:
    placeholder = await update.message.reply_text("💭 _Thinking..._", parse_mode=ParseMode.MARKDOWN)
    full_text   = ""
    last_edit   = time.monotonic()
    INTERVAL    = 0.9  # edit at most every 0.9s (Telegram rate limit safe)

    try:
        async for chunk in stream_ai_response(provider, model, messages):
            full_text += chunk
            now = time.monotonic()
            if now - last_edit >= INTERVAL and full_text.strip():
                try:
                    await placeholder.edit_text(full_text.rstrip() + " ▌")
                    last_edit = now
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception:
                    pass

        # Final — remove cursor, handle long responses
        if not full_text.strip():
            await placeholder.edit_text("❌ Empty response. Dusra model try karo → /model")
            return ""

        if len(full_text) > 4000:
            await placeholder.delete()
            for i in range(0, len(full_text), 4000):
                await update.message.reply_text(full_text[i:i + 4000])
        else:
            await placeholder.edit_text(full_text)

    except Exception as exc:
        logger.error("Streaming error [%s/%s]: %s", provider, model, exc)
        try:
            await placeholder.edit_text(
                f"❌ *Error:*\n`{exc}`\n\nDusra model try karein → /model",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        raise

    return full_text


# ════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    db.update_activity(user.id, getattr(user, "username", "") or "", user.first_name)
    crown   = " 👑" if is_owner(user.id) else ""
    active  = get_active_providers()
    plist   = " · ".join(AVAILABLE_MODELS[p]["name"] for p in active) or "⚠️ Koi key set nahi"

    caption = (
        f"🤖 *AI Bot mein Swagat!*{crown}\n\n"
        f"*Providers:* {plist}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 /menu  —  Main hub\n"
        f"💞 /meet  —  AI Companions\n"
        f"🎮 /games —  Fun games\n"
        f"🔌 /model —  AI model\n"
        f"👤 /profile — Tera profile\n"
        f"🏆 /top   —  Leaderboard\n"
        f"🆘 /help  —  Help\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_Koi bhi message bhejo!_ 💬"
    )

    sent = False
    if config.WELCOME_VIDEO:
        try:
            await update.message.reply_video(video=config.WELCOME_VIDEO,
                                              caption=caption, parse_mode=ParseMode.MARKDOWN)
            sent = True
        except Exception as e:
            logger.warning("Welcome video failed: %s", e)
    if not sent and config.WELCOME_PIC:
        try:
            await update.message.reply_photo(photo=config.WELCOME_PIC,
                                             caption=caption, parse_mode=ParseMode.MARKDOWN)
            sent = True
        except Exception as e:
            logger.warning("Welcome pic failed: %s", e)
    if not sent:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)

    # Award first_chat badge
    new_b = await db.check_and_award_badges(user.id)
    for bk in new_b:
        b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
        await update.message.reply_text(
            f"🎉 *New Badge Unlocked!*\n\n{b[0]} *{b[1]}*\n_{b[2]}_",
            parse_mode=ParseMode.MARKDOWN,
        )


# ════════════════════════════════════════════════════════════
#  /menu
# ════════════════════════════════════════════════════════════
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    state = get_state(user.id)
    if state["mode"] == "character":
        char = get_character(state["character"])
        mode_line = f"💞 Chatting with *{char['name']}* — /leave"
    elif state["mode"] == "game":
        mode_line = f"🎮 In a game — /endgame"
    else:
        lbl = get_model_label(state["provider"], state["model"])
        mode_line = f"🤖 AI Mode — `{lbl}`"

    app_url = f"{config.WEBHOOK_URL}/app" if config.WEBHOOK_URL else None
    kb = [
        [
            InlineKeyboardButton(
                "🚀 Open Mini App",
                web_app=WebAppInfo(url=app_url) if app_url else None,
                callback_data=None if app_url else "menu:noapp",
            )
        ] if app_url else [],
        [InlineKeyboardButton("💞 Meet Characters", callback_data="menu:meet"),
         InlineKeyboardButton("🎮 Games",           callback_data="menu:games")],
        [InlineKeyboardButton("🔌 Change Model",    callback_data="menu:model"),
         InlineKeyboardButton("👤 Profile",         callback_data="menu:profile")],
        [InlineKeyboardButton("🏆 Leaderboard",     callback_data="menu:top"),
         InlineKeyboardButton("🎖️ My Badges",       callback_data="menu:badges")],
        [InlineKeyboardButton("📊 Status",          callback_data="menu:status"),
         InlineKeyboardButton("🧹 Clear History",   callback_data="menu:clear")],
    ]
    kb = [row for row in kb if row]  # remove empty rows
    if is_owner(user.id):
        kb.append([
            InlineKeyboardButton("📈 Stats",        callback_data="menu:stats"),
            InlineKeyboardButton("🌐 Dashboard",    url=f"{config.WEBHOOK_URL}/dashboard" if config.WEBHOOK_URL else "https://t.me"),
        ])

    await update.message.reply_text(
        f"📋 *Main Menu*\n\n{mode_line}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action == "meet":
        await query.edit_message_text("💞 Loading companions...", parse_mode=ParseMode.MARKDOWN)
        await _send_card(query.message, uid, 0)
    elif action == "games":
        await query.edit_message_text(
            "🎮 *Games Menu* — Kaunsa game khelna hai?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=gm.games_menu_keyboard(),
        )
    elif action == "model":
        await query.edit_message_text(
            "🔌 *Provider chunein:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_provider_keyboard(),
        )
    elif action == "profile":
        await _show_profile(query.message, uid, edit=True)
    elif action == "top":
        await query.edit_message_text(_leaderboard_text(), parse_mode=ParseMode.MARKDOWN)
    elif action == "badges":
        await query.edit_message_text(_badges_text(uid), parse_mode=ParseMode.MARKDOWN)
    elif action == "status":
        await query.edit_message_text(_status_text(uid), parse_mode=ParseMode.MARKDOWN)
    elif action == "clear":
        get_state(uid)["history"] = []
        await query.edit_message_text("🧹 History clear!")
    elif action == "stats" and is_owner(uid):
        await query.edit_message_text(_stats_text(), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════════════
#  /help
# ════════════════════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    base = (
        "🆘 *Help*\n\n"
        "📋 /menu     — Main hub\n"
        "🤖 /start    — Welcome\n"
        "💞 /meet     — AI Companions\n"
        "🎮 /games    — Games menu\n"
        "👋 /leave    — Leave character/game\n"
        "🔌 /model    — Change AI model\n"
        "📊 /status   — Current status\n"
        "👤 /profile  — Your profile\n"
        "🏆 /top      — Leaderboard\n"
        "🎖️ /badges   — Your badges\n"
        "🧹 /clear    — Clear history\n"
    )
    owner_sec = (
        "\n👑 *Owner:*\n"
        "/adduser /removeuser /ban /unban /setlimit\n"
        "/users /stats /broadcast /finduser\n"
        "/schedule /unschedule /schedules\n"
        "/setprompt /viewprompt /resetprompt\n"
    ) if is_owner(user.id) else ""
    await update.message.reply_text(base + owner_sec, parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════════════
#  STATUS / LEADERBOARD / BADGES
# ════════════════════════════════════════════════════════════
def _status_text(uid: int) -> str:
    state = get_state(uid)
    used  = db.get_usage_today(uid)
    total = db.get_total_messages(uid)
    bdgs  = db.get_badges(uid)

    if state["mode"] == "character":
        char  = get_character(state["character"])
        cname = char["name"] if char else "?"
        pts   = db.get_relationship(uid, state["character"])
        rlbl  = db.get_relationship_label(pts)
        mood  = db.get_char_mood(state["character"])
        mode_line = f"💞 *{cname}* | {rlbl} | Mood: {mood}"
    else:
        lbl  = get_model_label(state["provider"], state["model"])
        pname = AVAILABLE_MODELS.get(state["provider"], {}).get("name", state["provider"])
        mode_line = f"🤖 {pname} — `{lbl}`"

    lim = db.get_allowed_users().get(uid, {}).get("limit") if not is_owner(uid) else None
    limit_str = f"{used}/{lim}" if lim else ("Unlimited 👑" if is_owner(uid) else f"{used}/∞")

    return (
        f"📊 *Status*\n\n"
        f"{mode_line}\n\n"
        f"Today  : {limit_str} msgs\n"
        f"Total  : {total} msgs\n"
        f"Badges : {len(bdgs)} 🎖️\n"
        f"History: {len(state['history'])} msgs"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    await update.message.reply_text(_status_text(user.id), parse_mode=ParseMode.MARKDOWN)

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    get_state(user.id)["history"] = []
    await update.message.reply_text("🧹 History clear!")

def _leaderboard_text() -> str:
    board = db.get_leaderboard(10)
    if not board:
        return "📭 Abhi koi data nahi."
    lines = ["🏆 *Leaderboard — Top Chatters*\n"]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    for i, (uid, total) in enumerate(board):
        prof  = db.get_profile(uid)
        info  = db.get_allowed_users().get(uid, {})
        name  = prof.get("name") or info.get("username") or f"User {uid}"
        today = db.get_usage_today(uid)
        lines.append(f"{medals[i]} *{name}* — {total} msgs (today: {today})")
    return "\n".join(lines)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    await update.message.reply_text(_leaderboard_text(), parse_mode=ParseMode.MARKDOWN)

def _badges_text(uid: int) -> str:
    owned = db.get_badges(uid)
    if not owned:
        return "🎖️ *Badges*\n\nAbhi koi badge nahi! Chatting karo aur unlock karo! 💬"
    lines = ["🎖️ *Your Badges*\n"]
    for key, (emoji, name, desc) in db.BADGE_DEFINITIONS.items():
        if key in owned:
            lines.append(f"{emoji} *{name}* — _{desc}_")
    total = len(db.BADGE_DEFINITIONS)
    lines.append(f"\n_{len(owned)}/{total} unlocked_")
    return "\n".join(lines)

async def cmd_badges(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    await update.message.reply_text(_badges_text(user.id), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════════════
#  /profile
# ════════════════════════════════════════════════════════════
async def _show_profile(message, uid: int, edit: bool = False):
    prof = db.get_profile(uid)
    text = (
        f"👤 *Your Profile*\n\n"
        f"Name   : {prof.get('name', '—')}\n"
        f"Age    : {prof.get('age', '—')}\n"
        f"Bio    : {prof.get('bio', '—')}\n"
        f"Mood   : {prof.get('mood', '—')}\n"
        f"Zodiac : {prof.get('zodiac', '—')}\n"
        f"Lang   : {prof.get('lang', 'Hinglish')}\n\n"
        f"_Edit: /profile name Alex_\n"
        f"_Fields: name, age, bio, mood, zodiac, lang_"
    )
    if edit:
        try:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            return
        except Exception:
            pass
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    if ctx.args and len(ctx.args) >= 2:
        field = ctx.args[0].lower()
        value = " ".join(ctx.args[1:])
        valid = {"name", "age", "bio", "mood", "zodiac", "lang"}
        if field in valid:
            prof = db.get_profile(user.id)
            prof[field] = value
            await db.save_profile(user.id, prof)
            await update.message.reply_text(f"✅ *{field.capitalize()}* set: *{value}*",
                                             parse_mode=ParseMode.MARKDOWN)
            return
    await _show_profile(update.message, user.id)


# ════════════════════════════════════════════════════════════
#  TINDER CHARACTER BROWSER
# ════════════════════════════════════════════════════════════
def _browse_kb(idx: int, char_id: str) -> InlineKeyboardMarkup:
    total = len(CHARACTERS)
    nav   = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"browse:prev:{idx}"))
    nav.append(InlineKeyboardButton(f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"browse:next:{idx}"))

    char = CHARACTERS[idx]
    mood = db.get_char_mood(char_id)
    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton(f"Mood: {mood}", callback_data="noop")],
        [InlineKeyboardButton(f"💬 Chat with {char['name']}", callback_data=f"choose:{char_id}")],
        [InlineKeyboardButton("❌ Close", callback_data="browse:cancel")],
    ])

async def _send_card(message, uid: int, idx: int):
    char = CHARACTERS[idx]
    pic  = get_char_pic(char)
    text = build_card_text(char, idx, len(CHARACTERS))
    kb   = _browse_kb(idx, char["id"])
    get_state(uid)["browse_idx"] = idx
    if pic:
        await message.reply_photo(photo=pic, caption=text,
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_meet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    await _send_card(update.message, user.id, 0)

async def cb_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]

    if action == "cancel":
        await query.edit_message_text("👋 /meet se dobara.")
        return

    idx = int(parts[2])
    new_idx = (idx + 1) % len(CHARACTERS) if action == "next" else max(0, idx - 1)
    char = CHARACTERS[new_idx]
    pic  = get_char_pic(char)
    text = build_card_text(char, new_idx, len(CHARACTERS))
    kb   = _browse_kb(new_idx, char["id"])
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
    state.update({"mode": "character", "character": char_id, "history": [], "game": None})

    pts  = db.get_relationship(uid, char_id)
    rlbl = db.get_relationship_label(pts)
    mood = db.get_char_mood(char_id)
    prof = db.get_profile(uid)
    uname = prof.get("name") or query.from_user.first_name

    # Award badge
    await db.award_badge(uid, "char_met")

    greeting = (
        f"💞 *{char['name']}* se connected!\n\n"
        f"_{char['intro']}_\n\n"
        f"Mood today: {mood}\n"
        f"Relationship: {rlbl} ({pts} pts)\n\n"
        f"_/leave — wapas | /games — games | /gift — gift bhejo_"
    )
    try:
        if get_char_pic(char):
            await query.edit_message_caption(caption=greeting, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(greeting, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await ctx.bot.send_message(uid, greeting, parse_mode=ParseMode.MARKDOWN)

async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = get_state(uid)
    if state["mode"] in ("character", "game"):
        char  = get_character(state["character"]) if state["character"] else None
        name  = char["name"] if char else "Game"
        state.update({"mode": "ai", "character": None, "history": [], "game": None})
        await update.message.reply_text(
            f"👋 *{name}* se bye!\n\nAb AI mode mein. /meet se dobara milo!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("ℹ️ Abhi character mode nahi.")


# ════════════════════════════════════════════════════════════
#  GIFT SYSTEM
# ════════════════════════════════════════════════════════════
GIFTS = {
    "rose":     ("🌹", "Rose",          5,  "Tumhara yeh gesture bahut sweet tha!"),
    "coffee":   ("☕", "Coffee",         3,  "Aww coffee bheja! Bilkul tumhari tarah warm!"),
    "star":     ("⭐", "Star",           8,  "OMG tumne mujhe star diya! Main blush kar rahi hoon!"),
    "heart":    ("❤️", "Heart",         10, "Yeh toh bahut special hai... dil se shukriya!"),
    "diamond":  ("💎", "Diamond",       20, "Diamond?! Tum serious ho?! Main... I'm speechless!"),
    "cake":     ("🎂", "Cake",           7,  "Cake! Main khush ho gayi! Let's celebrate together!"),
}

def gift_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(GIFTS.items())
    for i in range(0, len(items), 3):
        rows.append([
            InlineKeyboardButton(f"{v[0]} {v[1]} (+{v[2]}pts)", callback_data=f"gift:{k}")
            for k, v in items[i:i+3]
        ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="gift:cancel")])
    return InlineKeyboardMarkup(rows)

async def cmd_gift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = get_state(uid)
    if state["mode"] != "character":
        await update.message.reply_text("💝 Gift dene ke liye pehle kisi character se /meet karo!")
        return
    char = get_character(state["character"])
    await update.message.reply_text(
        f"🎁 *{char['name']} ko kya bhejein?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=gift_keyboard(),
    )

async def cb_gift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    gkey   = query.data.split(":", 1)[1]

    if gkey == "cancel":
        await query.edit_message_text("💝 Gift cancel kar diya.")
        return

    state = get_state(uid)
    if state["mode"] != "character":
        await query.edit_message_text("❌ Pehle kisi character se chat shuru karo.")
        return

    gift = GIFTS.get(gkey)
    if not gift:
        return

    char = get_character(state["character"])
    emoji, name, pts, reaction = gift

    await db.add_relationship_point(uid, state["character"], pts)
    new_pts = db.get_relationship(uid, state["character"])
    rlbl    = db.get_relationship_label(new_pts)

    await query.edit_message_text(
        f"{emoji} *Tumne {char['name']} ko {name} bheja!*\n\n"
        f"_{char['name']}: {reaction}_\n\n"
        f"Relationship: {rlbl} (+{pts} pts → {new_pts} total)",
        parse_mode=ParseMode.MARKDOWN,
    )
    # Check new badges
    new_b = await db.check_and_award_badges(uid)
    for bk in new_b:
        b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
        await ctx.bot.send_message(
            uid,
            f"🎉 *Badge Unlocked!*\n\n{b[0]} *{b[1]}*\n_{b[2]}_",
            parse_mode=ParseMode.MARKDOWN,
        )


# ════════════════════════════════════════════════════════════
#  GAMES
# ════════════════════════════════════════════════════════════
async def cmd_games(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
        return
    await update.message.reply_text(
        "🎮 *Games Menu*\n\nKaunsa game khelna hai?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=gm.games_menu_keyboard(),
    )

async def cmd_endgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    if state["mode"] == "game":
        state.update({"mode": "ai", "game": None, "history": []})
        await update.message.reply_text("🎮 Game khatam. /games se dobara khelo!")
    else:
        await update.message.reply_text("ℹ️ Koi game active nahi.")

async def cb_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]

    if action == "close":
        await query.edit_message_text("🎮 Games menu closed. /games se dobara!")
        return

    state     = get_state(uid)
    prof      = db.get_profile(uid)
    uname     = prof.get("name") or query.from_user.first_name
    char      = get_character(state.get("character")) if state.get("character") else None
    char_name = char["name"] if char else "Aria"
    char_mood = db.get_char_mood(state.get("character") or "aria")

    if action == "start":
        game = parts[2]

        if game == "tod":
            state.update({"mode": "game", "game": "tod", "history": []})
            # Inject AI system prompt — AI will generate all questions itself
            sys_p = gm.truth_or_dare_system(char_name, uname, char_mood)
            state["history"] = [{"role": "system", "content": sys_p}]
            await query.edit_message_text(
                f"🎮 *Truth or Dare with {char_name}!*\n\n"
                f"_{char_name}: Okay {uname}! Main ready hoon 😏_\n\n"
                f"Kya chahiye tumhe?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=gm.tod_keyboard(),
            )

        elif game == "20q":
            state.update({"mode": "game", "game": "20q", "history": []})
            sys_p = gm.twenty_questions_system(char_name, uname)
            state["history"] = [{"role": "system", "content": sys_p}]
            # AI asks the first question
            msgs = state["history"] + [{"role": "user", "content": "Start the game! I'm thinking of something."}]
            await query.edit_message_text(f"🧩 *20 Questions!*\n\n_{char_name}: Ooh! Main guess karunga/karungi..._ \n\n_Sochna shuru karo..._ 🤔")
            await _stream_game_reply(query, state, msgs)

        elif game == "story":
            # Show genre picker first
            state.update({"mode": "game", "game": "story_pick"})
            await query.edit_message_text(
                "📖 *Story Mode!*\n\nKaunsa genre choose karte ho?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=gm.story_genre_keyboard(),
            )

        elif game == "horo":
            state.update({"mode": "game", "game": "horo"})
            await query.edit_message_text(
                "🔮 *Daily Horoscope*\n\nApna zodiac sign chunein:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=gm.horoscope_keyboard(),
            )

        elif game == "spin":
            state.update({"mode": "game", "game": "spin", "history": []})
            sys_p = gm.spin_system(char_name, uname, prof)
            state["history"] = [{"role": "system", "content": sys_p}]
            msgs = state["history"] + [{"role": "user", "content": "Spin the wheel! Give me my activity!"}]
            await query.edit_message_text(f"🎰 *Wheel Spin!*\n\n_{char_name}: Okay spinning..._ 🎡")
            await _stream_game_reply(query, state, msgs)

        elif game == "riddle":
            state.update({"mode": "game", "game": "riddle", "history": []})
            sys_p = gm.riddle_system(char_name)
            state["history"] = [{"role": "system", "content": sys_p}]
            msgs = state["history"] + [{"role": "user", "content": "Start! Give me a riddle."}]
            await query.edit_message_text(f"🧠 *Riddle Time with {char_name}!*\n\n_Socho carefully..._ 🤔")
            await _stream_game_reply(query, state, msgs)

        elif game == "word":
            state.update({"mode": "game", "game": "word_pick"})
            await query.edit_message_text(
                "🔤 *Word Games!*\n\nKaunsa game khelna hai?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=gm.word_game_keyboard(),
            )

        # Award badge
        await db.award_badge(uid, "gamer")
        return

    # ── Story genre selected ─────────────────────────────────
    if action == "story":
        genre = parts[2]
        state.update({"mode": "game", "game": "story", "history": []})
        sys_p = gm.story_mode_system(char_name, uname, genre)
        state["history"] = [{"role": "system", "content": sys_p}]
        # AI writes the opening
        msgs = state["history"] + [{"role": "user", "content": f"Start our {genre} story! Write the opening scene."}]
        await query.edit_message_text(f"📖 *{genre.capitalize()} Story begins...*\n\n_{char_name} picks up the pen..._ ✍️")
        await _stream_game_reply(query, state, msgs)
        await db.award_badge(uid, "story_teller")
        return

    # ── Word game type selected ──────────────────────────────
    if action == "word":
        game_type = parts[2]
        state.update({"mode": "game", "game": f"word_{game_type}", "history": []})
        sys_p = gm.word_game_system(char_name, game_type)
        state["history"] = [{"role": "system", "content": sys_p}]
        game_names = {"antakshari": "Antakshari 🎵", "wordchain": "Word Chain 🔗"}
        name_display = game_names.get(game_type, game_type)
        msgs = state["history"] + [{"role": "user", "content": "Let's start! You go first."}]
        await query.edit_message_text(f"🔤 *{name_display} with {char_name}!*\n\n_{char_name}: Chalte hain!_ 🎯")
        await _stream_game_reply(query, state, msgs)
        return

    # ── Truth or Dare button presses ─────────────────────────
    if action == "tod":
        choice = parts[2]
        if choice == "end":
            state.update({"mode": "ai", "game": None, "history": []})
            await query.edit_message_text("🎮 Truth or Dare khatam! /games se dobara khelo.")
            return
        # User chose Truth/Dare/Random — AI generates the question
        choice_map = {"truth": "Give me a Truth question now.", "dare": "Give me a Dare challenge now.", "random": "Give me either Truth or Dare — your choice, surprise me!"}
        user_msg = choice_map.get(choice, "Give me a question.")
        state["history"].append({"role": "user", "content": user_msg})
        msgs = state["history"]
        await query.edit_message_text(f"_{char_name} is thinking..._ 💭")
        await _stream_game_reply(query, state, msgs, keyboard=gm.tod_keyboard())
        return

    # ── Horoscope sign selected ──────────────────────────────
    if action == "horo":
        sign  = parts[2]
        sys_p = gm.horoscope_system(char_name, sign, uname)
        msgs  = [
            {"role": "system", "content": sys_p},
            {"role": "user",   "content": f"Give me my complete {sign} horoscope for today!"},
        ]
        await query.edit_message_text(f"🔮 *{sign} Horoscope*\n\n_{char_name}: Taaron se pooch rahi/raha hoon..._ ✨")
        full = ""
        try:
            async for chunk in stream_ai_response(state["provider"], state["model"], msgs):
                full += chunk
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
            return
        state.update({"mode": "ai", "game": None})
        try:
            await query.edit_message_text(f"🔮 *{sign} — Aaj ka Horoscope*\n\n{full}", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await query.message.reply_text(f"🔮 *{sign} — Aaj ka Horoscope*\n\n{full}", parse_mode=ParseMode.MARKDOWN)


async def _stream_game_reply(query, state: dict, msgs: list, keyboard=None):
    """Stream AI reply for a game, edit the placeholder message."""
    import time
    full_text = ""
    last_edit = time.monotonic()
    try:
        async for chunk in stream_ai_response(state["provider"], state["model"], msgs):
            full_text += chunk
            now = time.monotonic()
            if now - last_edit >= 1.0 and full_text.strip():
                try:
                    await query.edit_message_text(full_text.rstrip() + " ▌", parse_mode=ParseMode.MARKDOWN)
                    last_edit = now
                except Exception:
                    pass
        # Final message
        if full_text.strip():
            state["history"].append({"role": "assistant", "content": full_text})
            try:
                if keyboard:
                    await query.edit_message_text(full_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                else:
                    await query.edit_message_text(full_text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
    except Exception as e:
        logger.error("Game stream error: %s", e)
        try:
            await query.edit_message_text(f"❌ Error: {e}\n\nDusra model try karo → /model")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  MODEL PICKER
# ════════════════════════════════════════════════════════════
def _provider_keyboard() -> InlineKeyboardMarkup:
    active = get_active_providers()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(AVAILABLE_MODELS[p]["name"], callback_data=f"prov:{p}")]
        for p in active
    ])

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_bot(user.id)[0]:
        await update.message.reply_text(can_use_bot(user.id)[1])
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
        f"🤖 *{AVAILABLE_MODELS[provider]['name']}* — model chunein:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )

async def cb_model_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query           = update.callback_query
    await query.answer()
    _, provider, mid = query.data.split(":", 2)
    state = get_state(query.from_user.id)
    state.update({"provider": provider, "model": mid, "history": [],
                  "mode": "ai", "character": None, "game": None})
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


# ════════════════════════════════════════════════════════════
#  OWNER COMMANDS
# ════════════════════════════════════════════════════════════
def _stats_text() -> str:
    s = db.get_global_stats()
    return (
        f"📈 *Bot Stats*\n\n"
        f"👥 Users     : {s['total_users']}\n"
        f"🔨 Banned    : {s['banned']}\n"
        f"💬 Today     : {s['msgs_today']}\n"
        f"🟢 Active    : {s['active_today']}\n"
        f"📨 Total     : {s['total_msgs']}\n"
        f"🗄️ MongoDB  : {'✅' if s['mongo_enabled'] else '⚠️ In-memory'}"
    )

async def cmd_miniapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send the Mini App button — /app command."""
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return
    app_url = f"{config.WEBHOOK_URL}/app" if config.WEBHOOK_URL else None
    if not app_url:
        await update.message.reply_text("⚠️ Mini App sirf Render deploy pe available hai.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Open AI Bot App", web_app=WebAppInfo(url=app_url))
    ]])
    await update.message.reply_text(
        "📱 *AI Bot Mini App*\n\n"
        "Profile, Leaderboard, Characters, Chat — sab ek jagah!\n\n"
        "_Button press karo:_ 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    await update.message.reply_text(_stats_text(), parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/adduser <id> [limit]`", parse_mode=ParseMode.MARKDOWN)
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
        f"✅ `{uid}` added! Limit: {f'{limit}/day' if limit else 'Unlimited'}",
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
        await update.message.reply_text("❌ Valid ID.")
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
        await update.message.reply_text("❌ Banned list mein nahi.")

@owner_only
async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/setlimit <id> <N>` (0=∞)", parse_mode=ParseMode.MARKDOWN)
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
        f"✅ `{uid}` limit: *{f'{n}/day' if n > 0 else 'Unlimited'}*",
        parse_mode=ParseMode.MARKDOWN,
    )

@owner_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    allowed = db.get_allowed_users()
    banned  = db.get_banned_users()
    if not allowed and not banned:
        await update.message.reply_text("📭 Koi user nahi.")
        return
    lines = []
    if allowed:
        lines.append("✅ *Allowed:*")
        for uid, info in list(allowed.items())[:20]:
            uname = f"@{info['username']}" if info.get("username") else "—"
            lim   = info.get("limit")
            used  = db.get_usage_today(uid)
            act   = db.get_activity(uid)
            last  = act.get("last_seen")
            last_str = last.strftime("%d/%m %H:%M") if last else "never"
            lines.append(f"  `{uid}` {uname} | {used}/{lim or '∞'} | last: {last_str}")
    if banned:
        lines.append("\n🔨 *Banned:*")
        for uid in list(banned)[:10]:
            lines.append(f"  `{uid}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_finduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/finduser <@username or name or id>`", parse_mode=ParseMode.MARKDOWN)
        return
    results = db.search_user(ctx.args[0])
    if not results:
        await update.message.reply_text("🔍 Koi user nahi mila.")
        return
    lines = ["🔍 *Search Results:*\n"]
    for uid, info in results:
        prof  = db.get_profile(uid)
        act   = db.get_activity(uid)
        last  = act.get("last_seen")
        last_str = last.strftime("%d/%m %H:%M") if last else "never"
        name  = prof.get("name", "—")
        uname = f"@{info.get('username', '—')}"
        total = db.get_total_messages(uid)
        today = db.get_usage_today(uid)
        lim   = info.get("limit")
        lines.append(
            f"`{uid}` {uname}\n"
            f"  Name: {name} | Msgs: {total} (today: {today}/{lim or '∞'})\n"
            f"  Last seen: {last_str}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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
async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /schedule HH:MM <message> — schedule daily broadcast"""
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/schedule HH:MM <message>`\nExample: `/schedule 09:00 Good morning! ☀️`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    time_str = ctx.args[0]
    try:
        hh, mm = time_str.split(":")
        int(hh); int(mm)
    except Exception:
        await update.message.reply_text("❌ Time format: HH:MM (e.g. 09:00)")
        return
    msg = " ".join(ctx.args[1:])
    sid = str(uuid.uuid4())[:8]
    sched = {"id": sid, "time": time_str, "message": msg, "active": True}
    await db.add_schedule(sched)
    # Register with APScheduler if running
    _register_schedule(sched, ctx.bot)
    await update.message.reply_text(
        f"⏰ *Scheduled!*\nID: `{sid}`\nTime: {time_str} daily\nMsg: {msg}",
        parse_mode=ParseMode.MARKDOWN,
    )

@owner_only
async def cmd_unschedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unschedule <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    sid = ctx.args[0]
    await db.remove_schedule(sid)
    try:
        _scheduler.remove_job(sid)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Schedule `{sid}` removed.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scheds = db.get_schedules()
    if not scheds:
        await update.message.reply_text("📭 Koi schedule nahi.")
        return
    lines = ["⏰ *Active Schedules:*\n"]
    for s in scheds:
        lines.append(f"`{s['id']}` — {s['time']} — {s['message'][:40]}...")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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


# ════════════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER
# ════════════════════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = can_use_bot(user.id)
    if not ok:
        await update.message.reply_text(msg)
        return

    # Anti-spam check
    if not is_owner(user.id) and db.check_spam(user.id):
        await db.auto_ban_user(user.id, reason="spam")
        user_states.pop(user.id, None)
        await update.message.reply_text(
            "🔨 *Auto-ban:* Bohot fast messages bhej rahe the.\n"
            "Owner se unban ke liye contact karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Update activity
    db.update_activity(user.id, getattr(user, "username", "") or "", user.first_name)

    state     = get_state(user.id)
    user_text = update.message.text.strip()
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Build system prompt
    if state["mode"] == "character":
        char       = get_character(state["character"])
        sys_prompt = char["prompt"] if char else current_system_prompt
        prof = db.get_profile(user.id)
        if prof.get("name"):
            sys_prompt += f"\n\nUser ka naam {prof['name']} hai."
        if prof.get("bio"):
            sys_prompt += f" Unke baare mein: {prof['bio']}."
        mood = db.get_char_mood(state["character"])
        sys_prompt += f"\n\nAaj tumhara mood {mood} hai — iske hisaab se reply karo."

    elif state["mode"] == "game":
        # Game conversation — use game-specific system prompt if present
        if state["history"] and state["history"][0].get("role") == "system":
            sys_prompt = state["history"][0]["content"]
            state["history"] = state["history"][1:]  # will be re-prepended
        else:
            sys_prompt = current_system_prompt
    else:
        sys_prompt = current_system_prompt

    state["history"].append({"role": "user", "content": user_text})
    msgs = [{"role": "system", "content": sys_prompt}] + state["history"]

    # Trim history
    if len(state["history"]) > config.MAX_HISTORY:
        state["history"] = state["history"][-config.MAX_HISTORY:]

    try:
        reply = await send_streaming(update, state["provider"], state["model"], msgs)

        if reply:
            state["history"].append({"role": "assistant", "content": reply})

            # Usage tracking
            db.record_usage(user.id)
            await db.persist_usage(user.id)

            # Relationship + badges for character mode
            if state["mode"] == "character" and state["character"]:
                await db.add_relationship_point(user.id, state["character"])

            new_b = await db.check_and_award_badges(user.id)
            for bk in new_b:
                b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
                await update.message.reply_text(
                    f"🎉 *Badge Unlocked!* {b[0]} *{b[1]}*\n_{b[2]}_",
                    parse_mode=ParseMode.MARKDOWN,
                )

    except Exception:
        # Error already shown by send_streaming
        if state["history"] and state["history"][-1]["role"] == "user":
            state["history"].pop()


# ════════════════════════════════════════════════════════════
#  SCHEDULER
# ════════════════════════════════════════════════════════════
_scheduler    = None
_bot_instance = None

def _register_schedule(sched: dict, bot):
    if _scheduler is None:
        return
    async def _send():
        targets = set(db.get_allowed_users().keys()) | config.OWNER_IDS
        for uid in targets:
            try:
                await bot.send_message(uid, f"⏰ *Scheduled Message:*\n\n{sched['message']}",
                                       parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
    try:
        hh, mm = sched["time"].split(":")
        _scheduler.add_job(
            lambda s=sched, b=bot: asyncio.create_task(_send()),
            trigger="cron",
            hour=int(hh),
            minute=int(mm),
            id=sched["id"],
            replace_existing=True,
        )
    except Exception as e:
        logger.warning("Schedule register failed: %s", e)

def start_scheduler(bot):
    global _scheduler, _bot_instance
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        _scheduler    = AsyncIOScheduler()
        _bot_instance = bot
        for sched in db.get_schedules():
            _register_schedule(sched, bot)
        _scheduler.start()
        logger.info("✅ Scheduler started")
    except Exception as e:
        logger.warning("Scheduler failed to start: %s", e)


# ════════════════════════════════════════════════════════════
#  WEB DASHBOARD  (FastAPI)
# ════════════════════════════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Bot Dashboard</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#e2e8f0;min-height:100vh}
  .header{background:linear-gradient(135deg,#6366f1,#8b5cf6,#ec4899);padding:24px 32px;display:flex;align-items:center;gap:16px}
  .header h1{font-size:1.6rem;font-weight:700}
  .header span{font-size:2rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;padding:28px 32px}
  .card{background:#1e1e2e;border-radius:16px;padding:24px;border:1px solid #2d2d44}
  .card .num{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#6366f1,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .card .label{color:#94a3b8;font-size:.85rem;margin-top:6px}
  .section{padding:0 32px 28px}
  .section h2{font-size:1.1rem;color:#a78bfa;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .table{width:100%;border-collapse:collapse}
  .table th{background:#2d2d44;padding:10px 14px;text-align:left;font-size:.8rem;color:#94a3b8;text-transform:uppercase}
  .table td{padding:10px 14px;border-bottom:1px solid #2d2d44;font-size:.9rem}
  .table tr:hover td{background:#2d2d44}
  .badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.75rem;font-weight:600}
  .badge.green{background:#064e3b;color:#34d399}
  .badge.red{background:#450a0a;color:#f87171}
  .badge.blue{background:#1e3a5f;color:#60a5fa}
  .status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
  .online{background:#34d399}.offline{background:#6b7280}
  footer{text-align:center;padding:20px;color:#4a5568;font-size:.8rem}
</style>
</head>
<body>
<div class="header">
  <span>🤖</span>
  <div><h1>AI Bot Dashboard</h1><div style="font-size:.85rem;opacity:.8">Live Stats</div></div>
  <div style="margin-left:auto;font-size:.85rem;opacity:.7" id="upd"></div>
</div>

<div class="grid" id="stats-grid">
  <div class="card"><div class="num" id="s-users">—</div><div class="label">👥 Total Users</div></div>
  <div class="card"><div class="num" id="s-today">—</div><div class="label">💬 Msgs Today</div></div>
  <div class="card"><div class="num" id="s-total">—</div><div class="label">📨 All-time Msgs</div></div>
  <div class="card"><div class="num" id="s-active">—</div><div class="label">🟢 Active Today</div></div>
  <div class="card"><div class="num" id="s-banned">—</div><div class="label">🔨 Banned</div></div>
  <div class="card"><div class="num" id="s-mongo">—</div><div class="label">🗄️ MongoDB</div></div>
</div>

<div class="section">
  <h2>🏆 Leaderboard</h2>
  <table class="table">
    <thead><tr><th>#</th><th>User</th><th>Total Msgs</th><th>Today</th></tr></thead>
    <tbody id="lb-body"></tbody>
  </table>
</div>

<div class="section">
  <h2>📋 Recent Activity</h2>
  <table class="table">
    <thead><tr><th>User ID</th><th>Name</th><th>Last Seen</th><th>Status</th></tr></thead>
    <tbody id="act-body"></tbody>
  </table>
</div>

<footer>➵⋆🪐ᴛᴇᴄʜɴɪᴄᴀʟ_sᴇʀᴇɴᴀ𓂃 · AI Bot Dashboard</footer>

<script>
const medals=['🥇','🥈','🥉'];
async function load(){
  try{
    const r=await fetch('/api/stats');
    const d=await r.json();
    document.getElementById('s-users').textContent=d.stats.total_users;
    document.getElementById('s-today').textContent=d.stats.msgs_today;
    document.getElementById('s-total').textContent=d.stats.total_msgs;
    document.getElementById('s-active').textContent=d.stats.active_today;
    document.getElementById('s-banned').textContent=d.stats.banned;
    document.getElementById('s-mongo').textContent=d.stats.mongo_enabled?'✅':'⚠️';

    const lb=document.getElementById('lb-body');
    lb.innerHTML=d.leaderboard.map((u,i)=>
      `<tr><td>${medals[i]||'#'+(i+1)}</td><td>${u.name}</td><td>${u.total}</td><td>${u.today}</td></tr>`
    ).join('');

    const now=Date.now();
    const act=document.getElementById('act-body');
    act.innerHTML=d.activity.map(u=>{
      const diff=Math.round((now-u.ts*1000)/60000);
      const online=diff<10;
      const when=diff<1?'Just now':diff<60?diff+'m ago':Math.round(diff/60)+'h ago';
      return `<tr>
        <td><code>${u.uid}</code></td>
        <td>${u.name||'—'}</td>
        <td>${when}</td>
        <td><span class="status-dot ${online?'online':'offline'}"></span>${online?'Online':'Offline'}</td>
      </tr>`;
    }).join('');

    document.getElementById('upd').textContent='Updated: '+new Date().toLocaleTimeString();
  }catch(e){console.error(e)}
}
load();setInterval(load,15000);
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════
#  PTB APPLICATION
# ════════════════════════════════════════════════════════════
def build_ptb_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

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
    app.add_handler(CommandHandler("games",       cmd_games))
    app.add_handler(CommandHandler("endgame",     cmd_endgame))
    app.add_handler(CommandHandler("top",         cmd_top))
    app.add_handler(CommandHandler("badges",      cmd_badges))
    app.add_handler(CommandHandler("gift",        cmd_gift))

    app.add_handler(CommandHandler("app",         cmd_miniapp))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("adduser",     cmd_adduser))
    app.add_handler(CommandHandler("removeuser",  cmd_removeuser))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("setlimit",    cmd_setlimit))
    app.add_handler(CommandHandler("users",       cmd_users))
    app.add_handler(CommandHandler("finduser",    cmd_finduser))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(CommandHandler("schedule",    cmd_schedule))
    app.add_handler(CommandHandler("unschedule",  cmd_unschedule))
    app.add_handler(CommandHandler("schedules",   cmd_schedules))
    app.add_handler(CommandHandler("setprompt",   cmd_setprompt))
    app.add_handler(CommandHandler("viewprompt",  cmd_viewprompt))
    app.add_handler(CommandHandler("resetprompt", cmd_resetprompt))

    app.add_handler(CallbackQueryHandler(cb_menu,           pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(cb_browse,         pattern=r"^browse:"))
    app.add_handler(CallbackQueryHandler(cb_choose,         pattern=r"^choose:"))
    app.add_handler(CallbackQueryHandler(cb_gift,           pattern=r"^gift:"))
    app.add_handler(CallbackQueryHandler(cb_game,           pattern=r"^game:"))
    app.add_handler(CallbackQueryHandler(cb_provider,       pattern=r"^prov:"))
    app.add_handler(CallbackQueryHandler(cb_model_select,   pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(cb_back_providers, pattern=r"^back:providers$"))
    app.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


# ════════════════════════════════════════════════════════════
#  FASTAPI APP + LIFESPAN
# ════════════════════════════════════════════════════════════
_ptb_app: Application = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ptb_app
    _ptb_app = build_ptb_app()
    await _ptb_app.initialize()
    await db.init_db()
    if config.WEBHOOK_URL:
        await _ptb_app.bot.set_webhook(
            f"{config.WEBHOOK_URL}/webhook",
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Webhook set: %s/webhook", config.WEBHOOK_URL)
    await _ptb_app.start()
    start_scheduler(_ptb_app.bot)
    yield
    logger.info("Shutting down...")
    await _ptb_app.stop()
    await _ptb_app.shutdown()
    if _scheduler:
        _scheduler.shutdown()

web_app = FastAPI(lifespan=lifespan)

@web_app.get("/")
async def health():
    return {"ok": True, "status": "running"}

@web_app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        body   = await request.body()
        update = Update.de_json(json.loads(body), _ptb_app.bot)
        await _ptb_app.process_update(update)
    except Exception as e:
        logger.error("Webhook error: %s", e)
    return Response(status_code=200)

@web_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

@web_app.get("/api/stats")
async def api_stats():
    stats = db.get_global_stats()
    board = db.get_leaderboard(10)
    lb_data = []
    for uid, total in board:
        prof  = db.get_profile(uid)
        info  = db.get_allowed_users().get(uid, {})
        name  = prof.get("name") or info.get("username") or f"User {uid}"
        lb_data.append({"uid": uid, "name": name, "total": total,
                        "today": db.get_usage_today(uid)})
    activity = []
    for uid, act in list(db.get_all_activity().items())[:20]:
        last = act.get("last_seen")
        activity.append({
            "uid":  uid,
            "name": act.get("first_name") or act.get("username") or str(uid),
            "ts":   int(last.timestamp()) if last else 0,
        })
    activity.sort(key=lambda x: x["ts"], reverse=True)
    return JSONResponse({"stats": stats, "leaderboard": lb_data, "activity": activity[:15]})


# ════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════
async def _polling_mode():
    """Local development: polling without FastAPI."""
    ptb = build_ptb_app()
    await db.init_db()
    async with ptb:
        await ptb.start()
        await ptb.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Polling mode — Ctrl+C to stop")
        await asyncio.Event().wait()

def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set!")
    if config.WEBHOOK_URL:
        # Production: FastAPI + uvicorn
        logger.info("Starting uvicorn on port %s", config.PORT)
        uvicorn.run(web_app, host="0.0.0.0", port=config.PORT, log_level="info")
    else:
        # Local dev: PTB polling
        asyncio.run(_polling_mode())



# ════════════════════════════════════════════════════════════
#  MINI APP API ENDPOINTS
# ════════════════════════════════════════════════════════════

# Mini App HTML — embedded directly (no file path issues)
_MINI_APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>AI Bot Mini App</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--tg-theme-bg-color, #0f0f1a);
    color: var(--tg-theme-text-color, #e2e8f0);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── Header ─────────────────────────────────── */
  .header {
    background: linear-gradient(135deg, #6366f1, #8b5cf6, #ec4899);
    padding: 20px 20px 30px;
    text-align: center;
    position: relative;
  }
  .header h1 { font-size: 1.4rem; font-weight: 800; }
  .header p  { font-size: .8rem; opacity: .85; margin-top: 4px; }
  .user-chip {
    display: inline-flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,.15);
    border-radius: 99px; padding: 6px 14px; margin-top: 10px;
    font-size: .82rem;
  }
  .avatar {
    width: 28px; height: 28px; border-radius: 50%;
    background: linear-gradient(135deg,#f59e0b,#ec4899);
    display: flex; align-items: center; justify-content: center;
    font-size: .75rem; font-weight: 700; color: #fff;
  }

  /* ── Nav Tabs ────────────────────────────────── */
  .tabs {
    display: flex; background: #1e1e2e;
    border-bottom: 1px solid #2d2d44;
    overflow-x: auto; -webkit-overflow-scrolling: touch;
  }
  .tab {
    flex: 1; min-width: 70px; padding: 12px 8px;
    text-align: center; cursor: pointer; font-size: .7rem;
    color: #64748b; border-bottom: 2px solid transparent;
    transition: all .2s; white-space: nowrap;
  }
  .tab.active { color: #a78bfa; border-bottom-color: #a78bfa; }
  .tab .icon { font-size: 1.3rem; display: block; margin-bottom: 3px; }

  /* ── Pages ───────────────────────────────────── */
  .page { display: none; padding: 20px; }
  .page.active { display: block; }

  /* ── Cards ───────────────────────────────────── */
  .card {
    background: #1e1e2e; border-radius: 16px;
    border: 1px solid #2d2d44; padding: 18px; margin-bottom: 14px;
  }
  .card-title {
    font-size: .9rem; font-weight: 700; margin-bottom: 4px;
    display: flex; align-items: center; gap: 8px;
  }
  .card-sub { font-size: .78rem; color: #94a3b8; }

  /* ── Stat Grid ───────────────────────────────── */
  .stat-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    margin-bottom: 16px;
  }
  .stat-card {
    background: #1e1e2e; border-radius: 14px;
    border: 1px solid #2d2d44; padding: 16px; text-align: center;
  }
  .stat-num {
    font-size: 1.8rem; font-weight: 800;
    background: linear-gradient(135deg,#6366f1,#ec4899);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .stat-label { font-size: .72rem; color: #94a3b8; margin-top: 4px; }

  /* ── Progress Bar ────────────────────────────── */
  .progress-wrap { margin: 6px 0 10px; }
  .progress-label { font-size: .73rem; color: #94a3b8; margin-bottom: 4px;
    display: flex; justify-content: space-between; }
  .progress-bar {
    height: 8px; background: #2d2d44; border-radius: 99px; overflow: hidden;
  }
  .progress-fill {
    height: 100%; border-radius: 99px;
    background: linear-gradient(90deg,#6366f1,#ec4899);
    transition: width .6s ease;
  }

  /* ── Badges Grid ─────────────────────────────── */
  .badges-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
  }
  .badge-item {
    background: #1e1e2e; border-radius: 12px;
    border: 1px solid #2d2d44; padding: 12px 8px; text-align: center;
  }
  .badge-item.owned { border-color: #6366f1; background: #1a1a35; }
  .badge-item.locked { opacity: .4; }
  .badge-emoji { font-size: 1.8rem; }
  .badge-name { font-size: .65rem; color: #94a3b8; margin-top: 4px; }

  /* ── Character Cards ─────────────────────────── */
  .char-card {
    background: #1e1e2e; border-radius: 16px;
    border: 1px solid #2d2d44; overflow: hidden; margin-bottom: 14px;
  }
  .char-banner {
    height: 80px;
    background: linear-gradient(135deg,#6366f1,#ec4899);
    display: flex; align-items: center; justify-content: center;
    font-size: 2.5rem;
  }
  .char-body { padding: 14px; }
  .char-name { font-size: 1rem; font-weight: 700; }
  .char-tag  { font-size: .73rem; color: #a78bfa; margin-top: 2px; }
  .char-rel  { margin-top: 10px; }

  /* ── Chat ────────────────────────────────────── */
  .chat-wrap {
    height: calc(100vh - 280px); overflow-y: auto;
    display: flex; flex-direction: column; gap: 10px;
    padding-bottom: 10px;
  }
  .msg {
    max-width: 80%; padding: 10px 14px;
    border-radius: 16px; font-size: .88rem; line-height: 1.45;
  }
  .msg.user {
    background: linear-gradient(135deg,#6366f1,#8b5cf6);
    color: #fff; align-self: flex-end; border-bottom-right-radius: 4px;
  }
  .msg.bot {
    background: #1e1e2e; border: 1px solid #2d2d44;
    align-self: flex-start; border-bottom-left-radius: 4px;
  }
  .msg.typing { color: #94a3b8; }
  .chat-input-row {
    display: flex; gap: 10px; margin-top: 12px;
    position: sticky; bottom: 0;
  }
  .chat-input {
    flex: 1; background: #1e1e2e; border: 1px solid #2d2d44;
    border-radius: 12px; padding: 10px 14px; color: inherit;
    font-size: .88rem; outline: none;
  }
  .chat-input:focus { border-color: #6366f1; }
  .send-btn {
    background: linear-gradient(135deg,#6366f1,#ec4899);
    border: none; border-radius: 12px; padding: 10px 16px;
    color: #fff; cursor: pointer; font-size: 1.1rem;
  }

  /* ── Leaderboard ─────────────────────────────── */
  .lb-row {
    display: flex; align-items: center; gap: 12px;
    background: #1e1e2e; border-radius: 12px;
    border: 1px solid #2d2d44; padding: 12px 14px; margin-bottom: 8px;
  }
  .lb-rank { font-size: 1.3rem; width: 30px; text-align: center; }
  .lb-name { flex: 1; font-size: .88rem; font-weight: 600; }
  .lb-score { font-size: .8rem; color: #a78bfa; }

  /* ── Buttons ─────────────────────────────────── */
  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 18px; border-radius: 12px;
    border: none; cursor: pointer; font-size: .85rem; font-weight: 600;
    transition: opacity .2s;
  }
  .btn:active { opacity: .7; }
  .btn-primary { background: linear-gradient(135deg,#6366f1,#8b5cf6); color: #fff; }
  .btn-outline { background: transparent; border: 1px solid #6366f1; color: #a78bfa; }
  .btn-block { width: 100%; justify-content: center; margin-bottom: 10px; }

  /* ── Section heading ─────────────────────────── */
  .sec-title {
    font-size: .8rem; color: #a78bfa; font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em;
    margin: 18px 0 10px;
  }

  /* ── Spinner ─────────────────────────────────── */
  .spinner {
    width: 20px; height: 20px; border-radius: 50%;
    border: 2px solid #2d2d44; border-top-color: #6366f1;
    animation: spin .7s linear infinite; margin: 20px auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Toast ───────────────────────────────────── */
  #toast {
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
    background: #6366f1; color: #fff; padding: 8px 18px;
    border-radius: 99px; font-size: .8rem; opacity: 0;
    transition: opacity .3s; pointer-events: none; z-index: 999;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>

<div class="header">
  <h1>🤖 AI Bot</h1>
  <p>➵⋆🪐ᴛᴇᴄʜɴɪᴄᴀʟ_sᴇʀᴇɴᴀ𓂃</p>
  <div class="user-chip">
    <div class="avatar" id="avatarLetter">?</div>
    <span id="userName">Loading...</span>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('home')">
    <span class="icon">🏠</span>Home
  </div>
  <div class="tab" onclick="switchTab('chat')">
    <span class="icon">💬</span>Chat
  </div>
  <div class="tab" onclick="switchTab('characters')">
    <span class="icon">💞</span>Characters
  </div>
  <div class="tab" onclick="switchTab('profile')">
    <span class="icon">👤</span>Profile
  </div>
  <div class="tab" onclick="switchTab('leaderboard')">
    <span class="icon">🏆</span>Top
  </div>
  <div class="tab" onclick="switchTab('badges')">
    <span class="icon">🎖️</span>Badges
  </div>
</div>

<!-- ════════════════════ HOME ════════════════════ -->
<div class="page active" id="page-home">
  <div class="sec-title">📊 Your Stats</div>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-num" id="h-msgs-today">—</div>
      <div class="stat-label">💬 Today</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="h-msgs-total">—</div>
      <div class="stat-label">📨 Total Msgs</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="h-badges">—</div>
      <div class="stat-label">🎖️ Badges</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="h-rel">—</div>
      <div class="stat-label">💞 Best Rel</div>
    </div>
  </div>

  <div class="sec-title">⚡ Quick Actions</div>
  <button class="btn btn-primary btn-block" onclick="switchTab('chat')">
    💬 Start Chatting
  </button>
  <button class="btn btn-outline btn-block" onclick="switchTab('characters')">
    💞 Browse Characters
  </button>
  <button class="btn btn-outline btn-block" onclick="openInBot('/games')">
    🎮 Play Games in Bot
  </button>

  <div class="sec-title">🏆 Today's Top</div>
  <div id="home-lb"></div>
</div>

<!-- ════════════════════ CHAT ════════════════════ -->
<div class="page" id="page-chat">
  <div class="card" style="margin-bottom:14px">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div>
        <div class="card-title" id="chat-char-name">🤖 AI Assistant</div>
        <div class="card-sub" id="chat-char-mood">Ready to chat!</div>
      </div>
      <button class="btn btn-outline" style="padding:6px 12px;font-size:.75rem"
              onclick="switchTab('characters')">Change</button>
    </div>
  </div>

  <div class="chat-wrap" id="chat-messages"></div>

  <div class="chat-input-row">
    <input class="chat-input" id="chat-input"
           placeholder="Message likhein..." autocomplete="off"
           onkeydown="if(event.key==='Enter')sendChat()">
    <button class="send-btn" onclick="sendChat()">➤</button>
  </div>
</div>

<!-- ════════════════════ CHARACTERS ════════════════════ -->
<div class="page" id="page-characters">
  <div class="sec-title">💞 AI Companions</div>
  <div id="chars-list"></div>
</div>

<!-- ════════════════════ PROFILE ════════════════════ -->
<div class="page" id="page-profile">
  <div class="card">
    <div class="card-title">👤 Your Profile</div>
    <div style="margin-top:14px;display:flex;flex-direction:column;gap:10px" id="profile-fields">
      <div class="spinner"></div>
    </div>
  </div>

  <div class="sec-title">🌟 Zodiac & Language</div>
  <div class="card">
    <div style="margin-bottom:10px">
      <label style="font-size:.78rem;color:#94a3b8">Zodiac Sign</label>
      <select id="p-zodiac" class="chat-input" style="margin-top:6px;width:100%">
        <option value="">Select...</option>
        <option>Aries</option><option>Taurus</option><option>Gemini</option>
        <option>Cancer</option><option>Leo</option><option>Virgo</option>
        <option>Libra</option><option>Scorpio</option><option>Sagittarius</option>
        <option>Capricorn</option><option>Aquarius</option><option>Pisces</option>
      </select>
    </div>
    <div>
      <label style="font-size:.78rem;color:#94a3b8">Chat Language</label>
      <select id="p-lang" class="chat-input" style="margin-top:6px;width:100%">
        <option value="Hinglish">Hinglish (Default)</option>
        <option value="Hindi">Hindi</option>
        <option value="English">English</option>
      </select>
    </div>
  </div>
  <button class="btn btn-primary btn-block" onclick="saveProfile()">💾 Save Profile</button>
</div>

<!-- ════════════════════ LEADERBOARD ════════════════════ -->
<div class="page" id="page-leaderboard">
  <div class="sec-title">🏆 Top Chatters</div>
  <div id="lb-list"><div class="spinner"></div></div>
</div>

<!-- ════════════════════ BADGES ════════════════════ -->
<div class="page" id="page-badges">
  <div class="sec-title">🎖️ Your Badges</div>
  <div id="badges-progress" style="margin-bottom:16px"></div>
  <div class="badges-grid" id="badges-grid"></div>
</div>

<div id="toast"></div>

<script>
// ── Telegram WebApp init ─────────────────────────────────
const tg       = window.Telegram.WebApp;
const tgUser   = tg.initDataUnsafe?.user || {};
const BOT_URL  = "https://t.me/YourBotUsername"; // replaced by server
const API_BASE = ""; // same origin

tg.expand();
tg.ready();

// Apply Telegram theme
if (tg.colorScheme === 'dark') {
  document.body.style.background = tg.themeParams.bg_color || '#0f0f1a';
}

// Show username
document.getElementById('userName').textContent =
  tgUser.first_name || tgUser.username || 'User';
const letter = (tgUser.first_name || 'U')[0].toUpperCase();
document.getElementById('avatarLetter').textContent = letter;

// ── State ────────────────────────────────────────────────
let currentChar   = null;
let chatHistory   = [];
let userProfile   = {};
let allBadgeDefs  = [];

// ── Tab Switcher ─────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const tabs = ['home','chat','characters','profile','leaderboard','badges'];
    t.classList.toggle('active', tabs[i] === name);
  });
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');

  if (name === 'home')        loadHome();
  if (name === 'characters')  loadCharacters();
  if (name === 'profile')     loadProfile();
  if (name === 'leaderboard') loadLeaderboard();
  if (name === 'badges')      loadBadges();
  if (name === 'chat')        scrollChat();
}

// ── API Helpers ──────────────────────────────────────────
async function apiGet(path) {
  const uid = tgUser.id || 0;
  const r   = await fetch(`${API_BASE}${path}?uid=${uid}`);
  return r.json();
}

async function apiPost(path, body) {
  const uid = tgUser.id || 0;
  const r   = await fetch(`${API_BASE}${path}?uid=${uid}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  return r.json();
}

// ── Toast ────────────────────────────────────────────────
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Open command in bot ──────────────────────────────────
function openInBot(cmd) {
  tg.close();
  // Telegram will re-open the bot with the deep-link
}

// ── HOME ─────────────────────────────────────────────────
async function loadHome() {
  try {
    const d = await apiGet('/api/me');
    document.getElementById('h-msgs-today').textContent  = d.today    ?? '—';
    document.getElementById('h-msgs-total').textContent  = d.total    ?? '—';
    document.getElementById('h-badges').textContent      = d.badges   ?? '—';
    document.getElementById('h-rel').textContent         = d.best_rel ?? '—';

    const lb   = await apiGet('/api/leaderboard');
    const medals = ['🥇','🥈','🥉'];
    document.getElementById('home-lb').innerHTML =
      (lb.data || []).slice(0,3).map((u,i) =>
        `<div class="lb-row">
          <div class="lb-rank">${medals[i]}</div>
          <div class="lb-name">${esc(u.name)}</div>
          <div class="lb-score">${u.total} msgs</div>
        </div>`
      ).join('');
  } catch(e) { console.error(e); }
}

// ── CHARACTERS ───────────────────────────────────────────
async function loadCharacters() {
  try {
    const d = await apiGet('/api/characters');
    document.getElementById('chars-list').innerHTML =
      (d.characters || []).map(c => {
        const pts   = c.relationship || 0;
        const pct   = Math.min(100, Math.round(pts));
        const rlbl  = relLabel(pts);
        const emojis = ['🌸','😏','📚','💻','🎨'];
        const idx   = d.characters.indexOf(c);
        return `<div class="char-card">
          <div class="char-banner">${emojis[idx % emojis.length]}</div>
          <div class="char-body">
            <div class="char-name">${esc(c.name)}, ${c.age}</div>
            <div class="char-tag">${esc(c.tagline)}</div>
            <div class="char-rel">
              <div class="progress-wrap">
                <div class="progress-label">
                  <span>${rlbl}</span><span>${pts} pts</span>
                </div>
                <div class="progress-bar">
                  <div class="progress-fill" style="width:${pct}%"></div>
                </div>
              </div>
            </div>
            <button class="btn btn-primary btn-block" style="margin-top:8px"
                    onclick="selectChar(${JSON.stringify(c).replace(/"/g,'&quot;')})">
              💬 Chat with ${esc(c.name)}
            </button>
          </div>
        </div>`;
      }).join('');
  } catch(e) {
    document.getElementById('chars-list').innerHTML = '<div class="card card-sub">Could not load characters.</div>';
  }
}

function relLabel(pts) {
  if(pts<5)   return '👋 Strangers';
  if(pts<15)  return '🙂 Acquaintances';
  if(pts<30)  return '😊 Friends';
  if(pts<60)  return '🥰 Close Friends';
  if(pts<100) return '💕 Best Friends';
  return '💞 Soulmates';
}

function selectChar(c) {
  currentChar = c;
  chatHistory = [];
  document.getElementById('chat-char-name').textContent = `${c.name}, ${c.age}`;
  document.getElementById('chat-char-mood').textContent  = c.mood || 'Ready to chat!';
  document.getElementById('chat-messages').innerHTML     = '';
  addMsg('bot', `💞 Hey! I'm ${c.name}. ${c.intro}`);
  switchTab('chat');
}

// ── CHAT ──────────────────────────────────────────────────
function addMsg(role, text) {
  const wrap = document.getElementById('chat-messages');
  const div  = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  wrap.appendChild(div);
  scrollChat();
}

function scrollChat() {
  const w = document.getElementById('chat-messages');
  if(w) setTimeout(() => w.scrollTop = w.scrollHeight, 50);
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const text  = input.value.trim();
  if(!text) return;
  input.value = '';

  addMsg('user', text);
  chatHistory.push({role:'user', content: text});

  // Show typing
  const wrap   = document.getElementById('chat-messages');
  const typing = document.createElement('div');
  typing.className = 'msg bot typing';
  typing.id        = 'typing-indicator';
  typing.textContent = '● ● ●';
  wrap.appendChild(typing);
  scrollChat();

  try {
    const d = await apiPost('/api/chat', {
      message:   text,
      history:   chatHistory.slice(-10),
      character: currentChar?.id || null,
    });
    typing.remove();
    const reply = d.reply || '❌ No response';
    addMsg('bot', reply);
    chatHistory.push({role:'assistant', content: reply});
  } catch(e) {
    typing.remove();
    addMsg('bot', '❌ Error. Please try again.');
  }
}

// ── PROFILE ──────────────────────────────────────────────
async function loadProfile() {
  try {
    const d = await apiGet('/api/me');
    userProfile = d.profile || {};
    const fields = ['name','age','bio','mood'];
    const labels = {name:'👤 Name', age:'🎂 Age', bio:'📝 Bio', mood:'😊 Mood'};
    document.getElementById('profile-fields').innerHTML = fields.map(f =>
      `<div>
        <label style="font-size:.78rem;color:#94a3b8">${labels[f]}</label>
        <input class="chat-input" id="p-${f}" style="width:100%;margin-top:5px"
               value="${esc(userProfile[f]||'')}" placeholder="${labels[f]}...">
      </div>`
    ).join('');

    if(userProfile.zodiac) document.getElementById('p-zodiac').value = userProfile.zodiac;
    if(userProfile.lang)   document.getElementById('p-lang').value   = userProfile.lang;
  } catch(e) {}
}

async function saveProfile() {
  const data = {
    name:   document.getElementById('p-name')?.value.trim()   || '',
    age:    document.getElementById('p-age')?.value.trim()    || '',
    bio:    document.getElementById('p-bio')?.value.trim()    || '',
    mood:   document.getElementById('p-mood')?.value.trim()   || '',
    zodiac: document.getElementById('p-zodiac')?.value        || '',
    lang:   document.getElementById('p-lang')?.value          || 'Hinglish',
  };
  try {
    await apiPost('/api/profile', data);
    toast('✅ Profile saved!');
    tg.HapticFeedback.notificationOccurred('success');
  } catch(e) { toast('❌ Save failed'); }
}

// ── LEADERBOARD ──────────────────────────────────────────
async function loadLeaderboard() {
  try {
    const d      = await apiGet('/api/leaderboard');
    const medals = ['🥇','🥈','🥉'];
    document.getElementById('lb-list').innerHTML =
      (d.data || []).map((u,i) =>
        `<div class="lb-row">
          <div class="lb-rank">${medals[i]||'#'+(i+1)}</div>
          <div class="lb-name">
            <div>${esc(u.name)}</div>
            <div style="font-size:.7rem;color:#64748b">Today: ${u.today}</div>
          </div>
          <div class="lb-score">${u.total}<br><span style="font-size:.7rem;color:#64748b">msgs</span></div>
        </div>`
      ).join('') || '<div class="card-sub" style="padding:20px;text-align:center">No data yet.</div>';
  } catch(e) {}
}

// ── BADGES ───────────────────────────────────────────────
async function loadBadges() {
  try {
    const d       = await apiGet('/api/badges');
    const owned   = new Set(d.owned || []);
    const all     = d.all   || [];
    const pct     = all.length ? Math.round(owned.size/all.length*100) : 0;

    document.getElementById('badges-progress').innerHTML = `
      <div class="progress-wrap">
        <div class="progress-label">
          <span>Progress</span><span>${owned.size}/${all.length} badges</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" style="width:${pct}%"></div>
        </div>
      </div>`;

    document.getElementById('badges-grid').innerHTML = all.map(b =>
      `<div class="badge-item ${owned.has(b.key)?'owned':'locked'}">
        <div class="badge-emoji">${b.emoji}</div>
        <div class="badge-name">${esc(b.name)}</div>
        ${owned.has(b.key) ? '' : '<div style="font-size:.6rem;color:#4a5568">🔒</div>'}
      </div>`
    ).join('');
  } catch(e) {}
}

// ── Helpers ───────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init ─────────────────────────────────────────────────
tg.MainButton.setText('Close').show().onClick(() => tg.close());
loadHome();
</script>
</body>
</html>
"""

@web_app.get("/app", response_class=HTMLResponse)
async def mini_app():
    """Serves the Telegram Mini App — HTML embedded in code, no file needed."""
    return HTMLResponse(_MINI_APP_HTML)


@web_app.get("/api/me")
async def api_me(uid: int = 0):
    """User's personal stats for Mini App."""
    if not uid:
        return JSONResponse({"error": "no uid"}, status_code=400)

    prof   = db.get_profile(uid)
    bdgs   = db.get_badges(uid)
    rels   = db._relationships.get(uid, {})
    best_r = max(rels.values()) if rels else 0

    return JSONResponse({
        "today":    db.get_usage_today(uid),
        "total":    db.get_total_messages(uid),
        "badges":   len(bdgs),
        "best_rel": best_r,
        "profile":  prof,
    })


@web_app.get("/api/leaderboard")
async def api_leaderboard(uid: int = 0):
    board = db.get_leaderboard(10)
    data  = []
    for u_id, total in board:
        p    = db.get_profile(u_id)
        info = db.get_allowed_users().get(u_id, {})
        name = p.get("name") or info.get("username") or f"User {u_id}"
        data.append({"uid": u_id, "name": name, "total": total,
                     "today": db.get_usage_today(u_id)})
    return JSONResponse({"data": data})


@web_app.get("/api/characters")
async def api_characters(uid: int = 0):
    from characters import CHARACTERS, get_char_pic
    chars_data = []
    for c in CHARACTERS:
        pts = db.get_relationship(uid, c["id"]) if uid else 0
        chars_data.append({
            "id":           c["id"],
            "name":         c["name"],
            "age":          c["age"],
            "tagline":      c["tagline"],
            "intro":        c["intro"],
            "hobbies":      c["hobbies"],
            "mood":         db.get_char_mood(c["id"]),
            "relationship": pts,
            "pic":          get_char_pic(c),
        })
    return JSONResponse({"characters": chars_data})


@web_app.get("/api/badges")
async def api_badges(uid: int = 0):
    owned = list(db.get_badges(uid)) if uid else []
    all_b = [
        {"key": k, "emoji": v[0], "name": v[1], "desc": v[2]}
        for k, v in db.BADGE_DEFINITIONS.items()
    ]
    return JSONResponse({"owned": owned, "all": all_b})


@web_app.post("/api/profile")
async def api_save_profile(request: Request, uid: int = 0):
    if not uid:
        return JSONResponse({"error": "no uid"}, status_code=400)
    try:
        data = await request.json()
        prof = db.get_profile(uid)
        for field in ("name", "age", "bio", "mood", "zodiac", "lang"):
            if field in data:
                prof[field] = data[field]
        await db.save_profile(uid, prof)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@web_app.post("/api/chat")
async def api_chat(request: Request, uid: int = 0):
    """Mini App in-app chat endpoint — streams full reply."""
    if not uid:
        return JSONResponse({"error": "no uid"}, status_code=400)

    ok, reason = can_use_bot(uid)
    if not ok:
        return JSONResponse({"reply": reason})

    try:
        body      = await request.json()
        message   = body.get("message", "")
        history   = body.get("history", [])
        char_id   = body.get("character")

        state = get_state(uid)

        # Build system prompt
        if char_id:
            from characters import get_character
            char       = get_character(char_id)
            sys_prompt = char["prompt"] if char else current_system_prompt
            mood       = db.get_char_mood(char_id)
            sys_prompt += f"\n\nAaj tumhara mood {mood} hai."
            prof = db.get_profile(uid)
            if prof.get("name"):
                sys_prompt += f"\n\nUser ka naam {prof['name']} hai."
            # Update state
            if state["character"] != char_id:
                state.update({"mode": "character", "character": char_id, "history": []})
        else:
            sys_prompt = current_system_prompt

        msgs = [{"role": "system", "content": sys_prompt}] + history + \
               [{"role": "user", "content": message}]

        # Collect full response (mini app doesn't stream)
        full = ""
        async for chunk in stream_ai_response(state["provider"], state["model"], msgs):
            full += chunk

        # Track usage + relationship
        db.record_usage(uid)
        await db.persist_usage(uid)
        if char_id:
            await db.add_relationship_point(uid, char_id)
        new_b = await db.check_and_award_badges(uid)

        return JSONResponse({
            "reply":      full,
            "new_badges": new_b,
        })

    except Exception as e:
        logger.error("Mini app chat error: %s", e)
        return JSONResponse({"reply": f"❌ Error: {e}"}, status_code=500)


if __name__ == "__main__":
    main()

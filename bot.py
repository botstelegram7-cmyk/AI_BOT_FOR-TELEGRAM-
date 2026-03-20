"""
Telegram AI Bot — Clean Edition
• Open to all users — 10 free msgs/day
• Force subscribe channel (optional)
• Tinder character browser + relationship system
• AI-powered personalized greetings (morning/noon/night)
• Owner: /lock /unlock /restart /broadcast /setprompt etc.
• Mini Web App at /app
• MongoDB persistence
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
    Bot, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup,
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
from love_mode_prompt import get_love_mode_prompt, is_love_mode_on, is_love_mode_off, LOVE_MODE_OFF_RESPONSE
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
bot_locked: bool = False

user_states: dict = {}

def get_state(uid: int) -> dict:
    if uid not in user_states:
        user_states[uid] = {
            "provider":   config.DEFAULT_PROVIDER,
            "model":      config.DEFAULT_MODEL,
            "history":    [],
            "mode":       "ai",
            "character":  None,
            "browse_idx": 0,
            "love_mode":  False,
            "user_gender": "unknown",
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

async def check_force_sub(bot: Bot, uid: int) -> bool:
    """Returns True if user is subscribed or force sub is disabled."""
    if not config.FORCE_SUB_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(f"@{config.FORCE_SUB_CHANNEL}", uid)
        return member.status not in (ChatMember.BANNED, ChatMember.LEFT)
    except Exception:
        return True  # if check fails, allow

async def can_use_bot(uid: int, bot: Bot = None) -> tuple:
    if is_owner(uid):
        return True, ""
    if bot_locked:
        return False, "🔒 Bot abhi locked hai. Baad mein try karo."
    if db.is_banned(uid):
        return False, "🚫 Aap ban hain. Owner se contact karein."
    if bot and config.FORCE_SUB_CHANNEL:
        subbed = await check_force_sub(bot, uid)
        if not subbed:
            return False, "FORCE_SUB"
    used = db.get_usage_today(uid)
    if used >= config.FREE_DAILY_LIMIT:
        return False, f"⏳ Aaj ka limit ({config.FREE_DAILY_LIMIT} msgs) khatam ho gaya!\nKal dobara aana 🌅"
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
#  FORCE SUB KEYBOARD
# ════════════════════════════════════════════════════════════
def force_sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=config.FORCE_SUB_URL)],
        [InlineKeyboardButton("✅ Joined! Check Again", callback_data="check_sub")],
    ])


# ════════════════════════════════════════════════════════════
#  STREAMING SEND
# ════════════════════════════════════════════════════════════
async def send_streaming(update: Update, provider: str, model: str, messages: list) -> str:
    placeholder = await update.message.reply_text("💭", parse_mode=ParseMode.MARKDOWN)
    full_text   = ""
    last_edit   = time.monotonic()

    try:
        async for chunk in stream_ai_response(provider, model, messages):
            full_text += chunk
            now = time.monotonic()
            if now - last_edit >= 0.9 and full_text.strip():
                try:
                    await placeholder.edit_text(full_text.rstrip() + " ▌")
                    last_edit = now
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception:
                    pass

        if not full_text.strip():
            await placeholder.edit_text("❌ Empty response. Dusra model try karo → /model")
            return ""

        if len(full_text) > 4000:
            await placeholder.delete()
            for i in range(0, len(full_text), 4000):
                await update.message.reply_text(full_text[i:i + 4000])
        else:
            try:
                await placeholder.edit_text(full_text)
            except Exception:
                pass

    except Exception as exc:
        logger.error("Stream error [%s/%s]: %s", provider, model, exc)
        try:
            await placeholder.edit_text(
                f"❌ *Error:*\n`{exc}`\n\nDusra model → /model",
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
    user = update.effective_user
    db.update_activity(user.id, getattr(user, "username", "") or "", user.first_name)

    # Force sub check
    if config.FORCE_SUB_CHANNEL and not is_owner(user.id):
        subbed = await check_force_sub(ctx.bot, user.id)
        if not subbed:
            await update.message.reply_text(
                "📢 *Bot use karne ke liye pehle channel join karo!*\n\n"
                "Join karne ke baad ✅ button press karo.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=force_sub_keyboard(),
            )
            return

    crown  = " 👑" if is_owner(user.id) else ""
    used   = db.get_usage_today(user.id)
    remain = "∞" if is_owner(user.id) else str(max(0, config.FREE_DAILY_LIMIT - used))

    caption = (
        f"✨ *Namaste, {user.first_name}!*{crown}\n\n"
        f"Main ek AI Bot hoon jo kai powerful AI models se connected hoon!\n\n"
        f"🎯 *Aaj ke liye:* {remain} messages bache hain\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    _kb_rows = [
        [InlineKeyboardButton("💬 AI Se Baat Karo", callback_data="start:chat"),
         InlineKeyboardButton("💞 Companions",      callback_data="start:meet")],
        [InlineKeyboardButton("🔌 Model Chunein",   callback_data="start:model"),
         InlineKeyboardButton("👤 Profile",         callback_data="start:profile")],
        [InlineKeyboardButton("📊 Meri Stats",      callback_data="start:status"),
         InlineKeyboardButton("🏆 Leaderboard",     callback_data="start:top")],
    ]
    if config.WEBHOOK_URL:
        _kb_rows.append([
            InlineKeyboardButton("📱 Mini App", web_app=WebAppInfo(url=f"{config.WEBHOOK_URL}/app"))
        ])
    _kb_rows.append([
        InlineKeyboardButton(config.OWNER_CONTACTS[0][0], url=config.OWNER_CONTACTS[0][1]),
        InlineKeyboardButton(config.OWNER_CONTACTS[1][0], url=config.OWNER_CONTACTS[1][1]),
    ])
    kb = InlineKeyboardMarkup(_kb_rows)

    sent = False
    if config.WELCOME_VIDEO:
        try:
            await update.message.reply_video(video=config.WELCOME_VIDEO,
                                              caption=caption, parse_mode=ParseMode.MARKDOWN,
                                              reply_markup=kb)
            sent = True
        except Exception:
            pass
    if not sent and config.WELCOME_PIC:
        try:
            await update.message.reply_photo(photo=config.WELCOME_PIC,
                                              caption=caption, parse_mode=ParseMode.MARKDOWN,
                                              reply_markup=kb)
            sent = True
        except Exception:
            pass
    if not sent:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN,
                                         reply_markup=kb)

    new_b = await db.check_and_award_badges(user.id)
    for bk in new_b:
        b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
        await update.message.reply_text(
            f"🎉 *Badge Unlocked!* {b[0]} *{b[1]}*\n_{b[2]}_",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cb_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action == "chat":
        await query.edit_message_text(
            "💬 *Bas kuch bhi likhdo — main sun raha/rahi hoon!*\n\n"
            "_/model se AI change karo | /meet se companions_",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "meet":
        await query.edit_message_text("💞 Loading...")
        await _send_card(query.message, uid, 0)
    elif action == "model":
        await query.edit_message_text("🔌 *Provider chunein:*",
                                       parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=_provider_keyboard())
    elif action == "profile":
        await _show_profile(query.message, uid, edit=True)
    elif action == "status":
        await query.edit_message_text(_status_text(uid), parse_mode=ParseMode.MARKDOWN)
    elif action == "top":
        await query.edit_message_text(_leaderboard_text(), parse_mode=ParseMode.MARKDOWN)


async def cb_check_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    subbed = await check_force_sub(ctx.bot, uid)
    if subbed:
        await query.answer("✅ Verified!", show_alert=False)
        await query.edit_message_text(
            "✅ *Shukriya join karne ke liye!*\n\n/start karo aur enjoy karo! 🎉",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.answer("❌ Abhi join nahi kiya!", show_alert=True)


# ════════════════════════════════════════════════════════════
#  /help
# ════════════════════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton("💞 AI Companions", callback_data="start:meet"),
         InlineKeyboardButton("🔌 Model Change",  callback_data="start:model")],
        [InlineKeyboardButton("👤 Profile",        callback_data="start:profile"),
         InlineKeyboardButton("📊 Status",         callback_data="start:status")],
        [InlineKeyboardButton(config.OWNER_CONTACTS[0][0], url=config.OWNER_CONTACTS[0][1]),
         InlineKeyboardButton(config.OWNER_CONTACTS[1][0], url=config.OWNER_CONTACTS[1][1])],
    ])
    owner_sec = (
        "\n\n👑 *Owner Commands:*\n"
        "`/lock` `/unlock` `/restart`\n"
        "`/adduser` `/ban` `/unban` `/setlimit`\n"
        "`/users` `/stats` `/broadcast`\n"
        "`/finduser` `/setprompt` `/schedule`\n"
    ) if is_owner(user.id) else ""

    await update.message.reply_text(
        "🆘 *Help*\n\n"
        "Koi bhi message likhो — AI jawab dega!\n\n"
        "`/start`  — Home\n"
        "`/meet`   — 💞 AI Companions\n"
        "`/leave`  — Character se bahar\n"
        "`/model`  — AI model change\n"
        "`/status` — Apni stats\n"
        "`/profile`— Profile edit\n"
        "`/top`    — Leaderboard\n"
        "`/badges` — Tumhare badges\n"
        "`/clear`  — History saaf\n"
        + owner_sec,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


# ════════════════════════════════════════════════════════════
#  STATUS / TOP / BADGES / CLEAR / PROFILE
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
        mode_line = f"💞 *{cname}* | {rlbl} | {mood}"
    else:
        lbl  = get_model_label(state["provider"], state["model"])
        pname = AVAILABLE_MODELS.get(state["provider"], {}).get("name", state["provider"])
        mode_line = f"🤖 {pname} — `{lbl}`"

    remain = "∞ 👑" if is_owner(uid) else str(max(0, config.FREE_DAILY_LIMIT - used))
    return (
        f"📊 *Tumhari Stats*\n\n"
        f"{mode_line}\n\n"
        f"Aaj use: {used} | Bacha: {remain}\n"
        f"Total: {total} msgs | Badges: {len(bdgs)} 🎖️"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_status_text(update.effective_user.id),
                                     parse_mode=ParseMode.MARKDOWN)

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_user.id)["history"] = []
    await update.message.reply_text("🧹 History clear!")

def _leaderboard_text() -> str:
    board  = db.get_leaderboard(10)
    if not board:
        return "📭 Koi data nahi abhi."
    medals = ["🥇","🥈","🥉"] + ["🏅"] * 7
    lines  = ["🏆 *Top Chatters*\n"]
    for i, (uid, total) in enumerate(board):
        prof  = db.get_profile(uid)
        info  = db.get_allowed_users().get(uid, {})
        name  = prof.get("name") or info.get("username") or f"User {uid}"
        lines.append(f"{medals[i]} *{name}* — {total} msgs")
    return "\n".join(lines)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_leaderboard_text(), parse_mode=ParseMode.MARKDOWN)

def _badges_text(uid: int) -> str:
    owned = db.get_badges(uid)
    if not owned:
        return "🎖️ *Badges*\n\nAbhi koi badge nahi! Chatting karo aur unlock karo! 💬"
    lines = ["🎖️ *Tumhare Badges*\n"]
    for key, (emoji, name, desc) in db.BADGE_DEFINITIONS.items():
        if key in owned:
            lines.append(f"{emoji} *{name}* — _{desc}_")
    lines.append(f"\n_{len(owned)}/{len(db.BADGE_DEFINITIONS)} unlocked_")
    return "\n".join(lines)

async def cmd_badges(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_badges_text(update.effective_user.id),
                                     parse_mode=ParseMode.MARKDOWN)

async def _show_profile(message, uid: int, edit: bool = False):
    prof = db.get_profile(uid)
    text = (
        f"👤 *Tumhara Profile*\n\n"
        f"Name   : {prof.get('name', '—')}\n"
        f"Age    : {prof.get('age', '—')}\n"
        f"Bio    : {prof.get('bio', '—')}\n"
        f"Mood   : {prof.get('mood', '—')}\n"
        f"Zodiac : {prof.get('zodiac', '—')}\n"
        f"Lang   : {prof.get('lang', 'Hinglish')}\n\n"
        f"_Edit: /profile name Alex_\n"
        f"_Fields: name, age, bio, mood, zodiac, lang_"
    )
    fn = message.edit_text if edit else message.reply_text
    try:
        await fn(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ctx.args and len(ctx.args) >= 2:
        field = ctx.args[0].lower()
        value = " ".join(ctx.args[1:])
        if field in {"name", "age", "bio", "mood", "zodiac", "lang"}:
            prof = db.get_profile(user.id)
            prof[field] = value
            await db.save_profile(user.id, prof)
            await update.message.reply_text(
                f"✅ *{field.capitalize()}* set: *{value}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    await _show_profile(update.message, user.id)


# ════════════════════════════════════════════════════════════
#  CHARACTER BROWSER
# ════════════════════════════════════════════════════════════
def _browse_kb(idx: int, char_id: str) -> InlineKeyboardMarkup:
    total = len(CHARACTERS)
    nav   = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"browse:prev:{idx}"))
    nav.append(InlineKeyboardButton(f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"browse:next:{idx}"))
    mood = db.get_char_mood(char_id)
    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton(f"Mood: {mood}", callback_data="noop")],
        [InlineKeyboardButton(f"💬 Chat with {CHARACTERS[idx]['name']}",
                              callback_data=f"choose:{char_id}")],
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
    await _send_card(update.message, update.effective_user.id, 0)

async def cb_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    idx    = int(parts[2])
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
    state.update({"mode": "character", "character": char_id, "history": []})
    pts   = db.get_relationship(uid, char_id)
    rlbl  = db.get_relationship_label(pts)
    mood  = db.get_char_mood(char_id)
    await db.award_badge(uid, "char_met")
    greeting = (
        f"💞 *{char['name']}* se connected!\n\n"
        f"_{char['intro']}_\n\n"
        f"Mood: {mood} | {rlbl}\n\n"
        f"_/leave — wapas jaao_"
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
    if state.get("mode") == "character":
        char = get_character(state["character"])
        name = char["name"] if char else "Character"
        state.update({"mode": "ai", "character": None, "history": []})
        await update.message.reply_text(
            f"👋 *{name}* se bye!\n\nAb AI mode. /meet se dobara milo!",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("ℹ️ Abhi character mode nahi.")


# ════════════════════════════════════════════════════════════
#  GIFT SYSTEM
# ════════════════════════════════════════════════════════════
GIFTS = {
    "rose":    ("🌹", "Rose",    5,  "Tumhara yeh gesture bahut sweet tha!"),
    "coffee":  ("☕", "Coffee",  3,  "Coffee! Bilkul tumhari tarah warm!"),
    "star":    ("⭐", "Star",    8,  "OMG star diya! Main blush kar rahi hoon!"),
    "heart":   ("❤️", "Heart",  10, "Yeh bahut special hai... dil se shukriya!"),
    "diamond": ("💎", "Diamond",20, "Diamond?! Main speechless hoon!"),
    "cake":    ("🎂", "Cake",    7,  "Cake! Let's celebrate together!"),
}

async def cmd_gift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = get_state(uid)
    if state["mode"] != "character":
        await update.message.reply_text("💝 Pehle /meet se kisi se connect karo!")
        return
    char = get_character(state["character"])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{v[0]} {v[1]} (+{v[2]})", callback_data=f"gift:{k}")
         for k, v in list(GIFTS.items())[i:i+3]]
        for i in range(0, len(GIFTS), 3)
    ] + [[InlineKeyboardButton("❌ Cancel", callback_data="gift:cancel")]])
    await update.message.reply_text(
        f"🎁 *{char['name']} ko kya bhejein?*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )

async def cb_gift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    gkey  = query.data.split(":", 1)[1]
    if gkey == "cancel":
        await query.edit_message_text("💝 Cancel.")
        return
    state = get_state(uid)
    if state["mode"] != "character":
        return
    gift  = GIFTS.get(gkey)
    if not gift:
        return
    char  = get_character(state["character"])
    emoji, name, pts, reaction = gift
    await db.add_relationship_point(uid, state["character"], pts)
    new_pts = db.get_relationship(uid, state["character"])
    rlbl    = db.get_relationship_label(new_pts)
    await query.edit_message_text(
        f"{emoji} *{char['name']} ko {name} bheja!*\n\n"
        f"_{char['name']}: {reaction}_\n\n"
        f"{rlbl} (+{pts} pts → {new_pts} total)",
        parse_mode=ParseMode.MARKDOWN,
    )
    new_b = await db.check_and_award_badges(uid)
    for bk in new_b:
        b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
        await ctx.bot.send_message(uid, f"🎉 *Badge!* {b[0]} *{b[1]}*\n_{b[2]}_",
                                    parse_mode=ParseMode.MARKDOWN)


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
    if not get_active_providers():
        await update.message.reply_text("⚠️ Koi API key set nahi.")
        return
    await update.message.reply_text("🔌 *Provider chunein:*",
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=_provider_keyboard())

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
        f"🤖 *{AVAILABLE_MODELS[provider]['name']}* — model:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )

async def cb_model_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query            = update.callback_query
    await query.answer()
    _, provider, mid = query.data.split(":", 2)
    state = get_state(query.from_user.id)
    state.update({"provider": provider, "model": mid, "history": [],
                  "mode": "ai", "character": None})
    label = get_model_label(provider, mid)
    pname = AVAILABLE_MODELS.get(provider, {}).get("name", provider)
    await query.edit_message_text(
        f"✅ *Set!*\n\n{pname} — `{label}`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_back_providers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔌 *Provider chunein:*",
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=_provider_keyboard())


# ════════════════════════════════════════════════════════════
#  OWNER COMMANDS
# ════════════════════════════════════════════════════════════
@owner_only
async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_locked
    bot_locked = True
    await update.message.reply_text("🔒 Bot locked. Sirf owners use kar sakte hain.")

@owner_only
async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_locked
    bot_locked = False
    await update.message.reply_text("🔓 Bot unlocked. Sabke liye open hai!")

@owner_only
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting... (Render will auto-restart)")
    import sys, os
    os.execv(sys.executable, [sys.executable] + sys.argv)

@owner_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = db.get_global_stats()
    await update.message.reply_text(
        f"📈 *Bot Stats*\n\n"
        f"👥 Users      : {s['total_users']}\n"
        f"🔨 Banned     : {s['banned']}\n"
        f"💬 Today      : {s['msgs_today']}\n"
        f"🟢 Active     : {s['active_today']}\n"
        f"📨 Total      : {s['total_msgs']}\n"
        f"🔒 Locked     : {'Yes' if bot_locked else 'No'}\n"
        f"🗄️ MongoDB    : {'✅' if s['mongo_enabled'] else '⚠️ In-memory'}",
        parse_mode=ParseMode.MARKDOWN,
    )

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
    await update.message.reply_text(f"✅ `{uid}` added!", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    uid = parse_uid(ctx.args[0])
    if uid:
        await db.remove_allowed_user(uid)
        user_states.pop(uid, None)
        await update.message.reply_text(f"🗑️ `{uid}` removed.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    uid = parse_uid(ctx.args[0])
    if uid and is_owner(uid):
        await update.message.reply_text("🚫 Owner ko ban nahi!")
        return
    if uid:
        await db.ban_user(uid)
        user_states.pop(uid, None)
        await update.message.reply_text(f"🔨 `{uid}` banned.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    uid = parse_uid(ctx.args[0])
    if uid:
        await db.unban_user(uid)
        await update.message.reply_text(f"✅ `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/setlimit <id> <N>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = parse_uid(ctx.args[0])
    try:
        n = int(ctx.args[1])
    except ValueError:
        return
    if uid:
        await db.set_user_limit(uid, n if n > 0 else None)
        await update.message.reply_text(f"✅ `{uid}` limit: {n or '∞'}", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    allowed = db.get_allowed_users()
    banned  = db.get_banned_users()
    lines   = []
    if allowed:
        lines.append("✅ *Allowed:*")
        for uid, info in list(allowed.items())[:15]:
            uname = f"@{info.get('username','')}" if info.get('username') else "—"
            lines.append(f"  `{uid}` {uname} | {db.get_usage_today(uid)}/{info.get('limit','∞')}")
    if banned:
        lines.append("\n🔨 *Banned:*")
        for uid in list(banned)[:10]:
            lines.append(f"  `{uid}`")
    await update.message.reply_text(
        "\n".join(lines) if lines else "📭 Koi user nahi.",
        parse_mode=ParseMode.MARKDOWN,
    )

@owner_only
async def cmd_finduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/finduser <query>`", parse_mode=ParseMode.MARKDOWN)
        return
    results = db.search_user(ctx.args[0])
    if not results:
        await update.message.reply_text("🔍 Nahi mila.")
        return
    lines = ["🔍 *Results:*\n"]
    for uid, info in results:
        prof = db.get_profile(uid)
        act  = db.get_activity(uid)
        last = act.get("last_seen")
        last_str = last.strftime("%d/%m %H:%M") if last else "never"
        lines.append(
            f"`{uid}` @{info.get('username','—')}\n"
            f"  Name: {prof.get('name','—')} | Total: {db.get_total_messages(uid)}\n"
            f"  Last: {last_str}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    text    = " ".join(ctx.args)
    targets = set(db.get_allowed_users().keys()) | config.OWNER_IDS | set(db.get_all_activity().keys())
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
        return
    current_system_prompt = " ".join(ctx.args)
    for s in user_states.values():
        s["history"] = []
    await update.message.reply_text(f"✅ Prompt updated!\n\n`{current_system_prompt}`",
                                     parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_viewprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📋 *Prompt:*\n\n`{current_system_prompt}`",
                                     parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_resetprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global current_system_prompt
    current_system_prompt = DEFAULT_SYSTEM_PROMPT
    for s in user_states.values():
        s["history"] = []
    await update.message.reply_text("🔄 Prompt reset.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/schedule HH:MM <message>`", parse_mode=ParseMode.MARKDOWN)
        return
    time_str = ctx.args[0]
    try:
        hh, mm = time_str.split(":")
        int(hh); int(mm)
    except Exception:
        await update.message.reply_text("❌ Format: HH:MM")
        return
    msg = " ".join(ctx.args[1:])
    sid = str(uuid.uuid4())[:8]
    sched = {"id": sid, "time": time_str, "message": msg}
    await db.add_schedule(sched)
    _register_schedule(sched, ctx.bot)
    await update.message.reply_text(
        f"⏰ Scheduled! ID: `{sid}` | Time: {time_str}", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_unschedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    sid = ctx.args[0]
    await db.remove_schedule(sid)
    try:
        _scheduler.remove_job(sid)
    except Exception:
        pass
    await update.message.reply_text(f"✅ `{sid}` removed.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    scheds = db.get_schedules()
    if not scheds:
        await update.message.reply_text("📭 Koi schedule nahi.")
        return
    lines = ["⏰ *Schedules:*\n"]
    for s in scheds:
        lines.append(f"`{s['id']}` — {s['time']} — {s['message'][:40]}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════════════
#  AI GREETINGS SCHEDULER
# ════════════════════════════════════════════════════════════
async def send_ai_greeting(bot: Bot, time_slot: str):
    """AI generates personalized greeting for each active user."""
    SLOT_CONTEXT = {
        "morning": (
            "It's morning (7-9 AM). Send a warm, energetic morning greeting. "
            "Motivate them for the day. Mention something positive about mornings. "
            "Keep it under 4 lines. Use emojis. Hinglish."
        ),
        "noon": (
            "It's noon (12-2 PM). Send a midday check-in message. "
            "Ask how their day is going. Give a quick motivational boost. "
            "Remind them to take a break, drink water. Under 4 lines. Hinglish."
        ),
        "night": (
            "It's night (9-11 PM). Send a calming, reflective goodnight message. "
            "Help them relax and unwind. Gently remind them to put down reels/social media. "
            "Encourage rest over overthinking. Under 4 lines. Hinglish."
        ),
    }
    context = SLOT_CONTEXT.get(time_slot, "")
    if not context:
        return

    # Get all active users
    activity   = db.get_all_activity()
    active_ids = list(activity.keys())

    state_ref  = user_states.get(list(config.OWNER_IDS)[0], {})
    provider   = state_ref.get("provider", config.DEFAULT_PROVIDER)
    model      = state_ref.get("model",    config.DEFAULT_MODEL)

    for uid in active_ids:
        try:
            prof     = db.get_profile(uid)
            name     = prof.get("name") or activity[uid].get("first_name") or "yaar"
            zodiac   = prof.get("zodiac", "")
            mood_str = f"Their recent mood: {prof.get('mood')}." if prof.get("mood") else ""

            sys_p = (
                f"You are a warm, caring AI companion sending a personal greeting to {name}. "
                f"{f'Their zodiac: {zodiac}.' if zodiac else ''} {mood_str} "
                f"{context} "
                f"Address them by name. Make it feel personal, not generic. Never say you are an AI."
            )
            msgs  = [
                {"role": "system", "content": sys_p},
                {"role": "user",   "content": "Send the greeting now."},
            ]
            full = ""
            async for chunk in stream_ai_response(provider, model, msgs):
                full += chunk

            if full.strip():
                await bot.send_message(uid, full, parse_mode=ParseMode.MARKDOWN)
                await asyncio.sleep(0.3)  # rate limit safe

        except Exception as e:
            logger.warning("Greeting failed for %s: %s", uid, e)


# ════════════════════════════════════════════════════════════
#  SCHEDULER
# ════════════════════════════════════════════════════════════
_scheduler    = None

def _register_schedule(sched: dict, bot):
    if _scheduler is None:
        return
    async def _send(s=sched, b=bot):
        targets = set(db.get_allowed_users().keys()) | config.OWNER_IDS
        for target_uid in targets:
            try:
                await b.send_message(target_uid, f"⏰ {s['message']}", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
    try:
        hh, mm = sched["time"].split(":")
        loop = asyncio.get_event_loop()
        def _sync_send(s=sched, b=bot, l=loop):
            asyncio.ensure_future(_send(s, b), loop=l)
        _scheduler.add_job(
            _sync_send,
            trigger="cron", hour=int(hh), minute=int(mm),
            id=sched["id"], replace_existing=True,
        )
    except Exception as e:
        logger.warning("Schedule register failed: %s", e)

async def start_scheduler(bot):
    """Called from within running asyncio event loop (lifespan)."""
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        loop = asyncio.get_event_loop()
        _scheduler = AsyncIOScheduler(event_loop=loop)

        def _make_job(slot):
            def job():
                asyncio.ensure_future(send_ai_greeting(bot, slot), loop=loop)
            return job

        _scheduler.add_job(_make_job("morning"), trigger="cron", hour=7,  minute=0,
                           id="greeting_morning", replace_existing=True)
        _scheduler.add_job(_make_job("noon"),    trigger="cron", hour=13, minute=0,
                           id="greeting_noon",    replace_existing=True)
        _scheduler.add_job(_make_job("night"),   trigger="cron", hour=21, minute=0,
                           id="greeting_night",   replace_existing=True)

        for sched in db.get_schedules():
            _register_schedule(sched, bot)

        _scheduler.start()
        logger.info("✅ Scheduler started (greetings: 7am, 1pm, 9pm UTC)")
    except Exception as e:
        logger.warning("Scheduler failed: %s", e)


# ════════════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER
# ════════════════════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    ok, msg = await can_use_bot(user.id, ctx.bot)

    if not ok:
        if msg == "FORCE_SUB":
            await update.message.reply_text(
                "📢 *Pehle channel join karo!*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=force_sub_keyboard(),
            )
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # Anti-spam
    if not is_owner(user.id) and db.check_spam(user.id):
        await db.auto_ban_user(user.id, reason="spam")
        user_states.pop(user.id, None)
        await update.message.reply_text("🔨 Spam detected. Auto-banned.")
        return

    db.update_activity(user.id, getattr(user, "username", "") or "", user.first_name)

    state     = get_state(user.id)
    user_text = update.message.text.strip()
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # ── Love Mode toggle ──────────────────────────────────
    if is_love_mode_on(user_text):
        state["love_mode"] = True
        state["history"]   = []
        lm_text = (
            "love mode...\n\n"
            f"hey {user.first_name} :)\n\n"
            "I'm here now.\n\n"
            "_Bata do: girlfriend chahiye ya boyfriend? So I know how to be here for you._"
        )
        await update.message.reply_text(lm_text, parse_mode=ParseMode.MARKDOWN)
        return

    if is_love_mode_off(user_text):
        state["love_mode"] = False
        state["history"]   = []
        await update.message.reply_text(LOVE_MODE_OFF_RESPONSE)
        return

    # Gender detection from user message
    if state.get("love_mode") and state.get("user_gender") == "unknown":
        t = user_text.lower()
        if any(w in t for w in ["girlfriend", "girl", "female", "ladki", "ladkiya", "she/her"]):
            state["user_gender"] = "female"
        elif any(w in t for w in ["boyfriend", "boy", "male", "ladka", "he/him"]):
            state["user_gender"] = "male"

    # Build system prompt
    if state.get("love_mode"):
        char       = get_character(state["character"]) if state.get("character") else None
        char_name  = char["name"] if char else "Aria"
        prof       = db.get_profile(user.id)
        uname      = prof.get("name") or user.first_name
        rel_pts    = db.get_relationship(user.id, state.get("character") or "aria")
        mood       = db.get_char_mood(state.get("character") or "aria")
        sys_prompt = get_love_mode_prompt(
            char_name       = char_name,
            user_name       = uname,
            user_gender     = state.get("user_gender", "unknown"),
            char_mood       = mood,
            relationship_level = rel_pts,
        )
    elif state["mode"] == "character":
        char       = get_character(state["character"])
        sys_prompt = char["prompt"] if char else current_system_prompt
        prof = db.get_profile(user.id)
        if prof.get("name"):
            sys_prompt += f"\n\nUser ka naam {prof['name']} hai."
        mood = db.get_char_mood(state["character"])
        sys_prompt += f"\n\nAaj tumhara mood {mood} hai."
    else:
        sys_prompt = current_system_prompt

    state["history"].append({"role": "user", "content": user_text})
    if len(state["history"]) > config.MAX_HISTORY:
        state["history"] = state["history"][-config.MAX_HISTORY:]
    msgs = [{"role": "system", "content": sys_prompt}] + state["history"]

    try:
        reply = await send_streaming(update, state["provider"], state["model"], msgs)
        if reply:
            state["history"].append({"role": "assistant", "content": reply})
            db.record_usage(user.id)
            await db.persist_usage(user.id)
            if state["mode"] == "character" and state["character"]:
                await db.add_relationship_point(user.id, state["character"])
            new_b = await db.check_and_award_badges(user.id)
            for bk in new_b:
                b = db.BADGE_DEFINITIONS.get(bk, ("🏅", bk, ""))
                await update.message.reply_text(
                    f"🎉 *Badge!* {b[0]} *{b[1]}*\n_{b[2]}_",
                    parse_mode=ParseMode.MARKDOWN,
                )
    except Exception:
        if state["history"] and state["history"][-1]["role"] == "user":
            state["history"].pop()


# ════════════════════════════════════════════════════════════
#  PTB APP
# ════════════════════════════════════════════════════════════
def build_ptb_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("model",       cmd_model))
    app.add_handler(CommandHandler("models",      cmd_model))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(CommandHandler("meet",        cmd_meet))
    app.add_handler(CommandHandler("leave",       cmd_leave))
    app.add_handler(CommandHandler("profile",     cmd_profile))
    app.add_handler(CommandHandler("top",         cmd_top))
    app.add_handler(CommandHandler("badges",      cmd_badges))
    app.add_handler(CommandHandler("gift",        cmd_gift))
    app.add_handler(CommandHandler("app",         cmd_miniapp))

    app.add_handler(CommandHandler("lock",        cmd_lock))
    app.add_handler(CommandHandler("unlock",      cmd_unlock))
    app.add_handler(CommandHandler("restart",     cmd_restart))
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

    app.add_handler(CallbackQueryHandler(cb_start,          pattern=r"^start:"))
    app.add_handler(CallbackQueryHandler(cb_check_sub,      pattern=r"^check_sub$"))
    app.add_handler(CallbackQueryHandler(cb_browse,         pattern=r"^browse:"))
    app.add_handler(CallbackQueryHandler(cb_choose,         pattern=r"^choose:"))
    app.add_handler(CallbackQueryHandler(cb_gift,           pattern=r"^gift:"))
    app.add_handler(CallbackQueryHandler(cb_provider,       pattern=r"^prov:"))
    app.add_handler(CallbackQueryHandler(cb_model_select,   pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(cb_back_providers, pattern=r"^back:providers$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(),
                                          pattern=r"^noop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


# ════════════════════════════════════════════════════════════
#  MINI APP COMMAND
# ════════════════════════════════════════════════════════════
async def cmd_miniapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    app_url = f"{config.WEBHOOK_URL}/app" if config.WEBHOOK_URL else None
    if not app_url:
        await update.message.reply_text("⚠️ Mini App sirf Render pe available hai.")
        return
    await update.message.reply_text(
        "📱 *AI Bot Mini App*\n\n_Button press karo:_ 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Open Mini App", web_app=WebAppInfo(url=app_url))
        ]]),
    )



# ════════════════════════════════════════════════════════════
#  FASTAPI + MINI APP HTML (embedded)
# ════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════
#  FASTAPI LIFESPAN
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
    await _ptb_app.start()
    await start_scheduler(_ptb_app.bot)
    yield
    await _ptb_app.stop()
    await _ptb_app.shutdown()
    if _scheduler:
        _scheduler.shutdown()

web_app = FastAPI(lifespan=lifespan)
web_app.router.lifespan_context = lifespan

_MINI_APP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>AI Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#f5f3ff;--card:#fff;--card2:#fdf8ff;--border:#ede8ff;
  --t1:#1c1040;--t2:#7c6faa;--t3:#b8a9e0;
  --p:#7c4dff;--p2:#e040fb;--p3:#00e5ff;
  --grad:linear-gradient(135deg,#7c4dff,#e040fb);
  --grad2:linear-gradient(135deg,#00e5ff,#7c4dff);
  --grad3:linear-gradient(135deg,#e040fb,#ff6d00);
  --sh:0 8px 32px rgba(124,77,255,.14);
  --sh2:0 2px 12px rgba(124,77,255,.10);
  --r:22px;--rs:14px;
}
.dk{
  --bg:#0e0b1f;--card:#16122e;--card2:#1d1840;--border:#2a2250;
  --t1:#f0eaff;--t2:#9d89cc;--t3:#4a3f70;
  --sh:0 8px 32px rgba(0,0,0,.4);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
body{font-family:'Plus Jakarta Sans',sans-serif;background:var(--bg);color:var(--t1);
     min-height:100vh;transition:background .25s,color .25s;overscroll-behavior:none;}

/* HERO HEADER */
.hero{
  background:var(--grad);
  padding:0 0 36px;
  position:relative;overflow:hidden;
}
.hero-blur{
  position:absolute;width:220px;height:220px;border-radius:50%;
  background:rgba(255,255,255,.08);top:-60px;right:-60px;pointer-events:none;
}
.hero-blur2{
  position:absolute;width:140px;height:140px;border-radius:50%;
  background:rgba(255,255,255,.06);bottom:-40px;left:20px;pointer-events:none;
}
.hero-top{
  display:flex;justify-content:space-between;align-items:center;
  padding:16px 18px 0;
}
.brand{
  font-family:'Syne',sans-serif;font-weight:800;font-size:1.45rem;
  color:#fff;letter-spacing:-.02em;
}
.brand span{opacity:.7;font-size:.9em;}
.theme-btn{
  background:rgba(255,255,255,.18);border:none;border-radius:99px;
  padding:7px 13px;color:#fff;cursor:pointer;font-size:.9rem;
  backdrop-filter:blur(8px);
}

/* USER PROFILE CARD in hero */
.profile-hero{
  display:flex;align-items:center;gap:14px;
  margin:18px 18px 0;
  background:rgba(255,255,255,.15);
  border-radius:var(--rs);padding:14px 16px;
  backdrop-filter:blur(12px);
}
.avatar-wrap{position:relative;flex-shrink:0;}
.avatar-img{
  width:54px;height:54px;border-radius:50%;
  border:3px solid rgba(255,255,255,.6);
  object-fit:cover;display:block;
  background:rgba(255,255,255,.2);
}
.avatar-fallback{
  width:54px;height:54px;border-radius:50%;
  border:3px solid rgba(255,255,255,.6);
  background:rgba(255,255,255,.25);
  display:flex;align-items:center;justify-content:center;
  font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:800;color:#fff;
}
.avatar-online{
  position:absolute;bottom:2px;right:2px;
  width:12px;height:12px;border-radius:50%;
  background:#00e676;border:2px solid rgba(255,255,255,.8);
}
.profile-info{flex:1;min-width:0;}
.profile-name{
  font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;
  color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.profile-sub{font-size:.72rem;color:rgba(255,255,255,.75);margin-top:2px;}
.profile-badge{
  background:rgba(255,255,255,.2);border-radius:99px;
  padding:3px 10px;font-size:.68rem;font-weight:700;color:#fff;
  white-space:nowrap;
}

/* STATS SCROLL */
.stats-row{
  display:flex;gap:10px;padding:18px 18px 0;
  overflow-x:auto;-webkit-overflow-scrolling:touch;
}
.stats-row::-webkit-scrollbar{display:none;}
.stat-card{
  flex-shrink:0;background:var(--card);border:1px solid var(--border);
  border-radius:var(--rs);padding:14px 18px;min-width:90px;text-align:center;
  box-shadow:var(--sh2);
}
.stat-n{
  font-family:'Syne',sans-serif;font-size:1.5rem;font-weight:800;
  background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.stat-l{font-size:.65rem;color:var(--t2);margin-top:2px;font-weight:600;}

/* TABS */
.tabs-wrap{
  display:flex;gap:8px;padding:16px 18px 0;
  overflow-x:auto;-webkit-overflow-scrolling:touch;
}
.tabs-wrap::-webkit-scrollbar{display:none;}
.tab{
  flex-shrink:0;padding:9px 18px;border-radius:99px;cursor:pointer;
  font-size:.78rem;font-weight:700;border:1.5px solid var(--border);
  background:var(--card);color:var(--t2);transition:all .2s;
  white-space:nowrap;
}
.tab.on{
  background:var(--grad);color:#fff;border-color:transparent;
  box-shadow:0 4px 14px rgba(124,77,255,.35);
}

/* PAGES */
.page{display:none;padding:16px 18px 100px;}
.page.on{display:block;}

/* SECTION LABEL */
.sl{
  font-size:.7rem;font-weight:800;color:var(--p);
  text-transform:uppercase;letter-spacing:.1em;margin:18px 0 10px;
}

/* CARD */
.card{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:18px;margin-bottom:12px;
  box-shadow:var(--sh2);
}

/* QUICK ACTION BUTTONS */
.qa-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:4px;}
.qa-btn{
  background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--rs);padding:16px 10px;text-align:center;
  cursor:pointer;transition:all .2s;box-shadow:var(--sh2);
}
.qa-btn:active{transform:scale(.96);}
.qa-btn .qi{font-size:1.6rem;display:block;margin-bottom:6px;}
.qa-btn .ql{font-size:.74rem;font-weight:700;color:var(--t1);}
.qa-btn.primary{
  background:var(--grad);border-color:transparent;
  box-shadow:0 6px 20px rgba(124,77,255,.35);
}
.qa-btn.primary .ql{color:#fff;}

/* CHARACTER CARD */
.char-card{
  background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--r);overflow:hidden;margin-bottom:14px;
  box-shadow:var(--sh);transition:transform .2s;
}
.char-card:active{transform:scale(.99);}
.char-banner{
  height:86px;position:relative;overflow:hidden;
  display:flex;align-items:center;justify-content:center;
}
.char-banner::before{content:'';position:absolute;inset:0;background:var(--grad2);}
.char-banner .ce{position:relative;z-index:1;font-size:2.8rem;}
.char-body{padding:16px;}
.char-name{font-family:'Syne',sans-serif;font-weight:800;font-size:1.05rem;}
.char-tag{font-size:.72rem;color:var(--t2);margin:3px 0 10px;font-weight:500;}

/* PROGRESS */
.prog-w{margin:8px 0;}
.prog-m{display:flex;justify-content:space-between;
        font-size:.68rem;color:var(--t2);font-weight:600;margin-bottom:5px;}
.prog-b{height:7px;background:var(--border);border-radius:99px;overflow:hidden;}
.prog-f{height:100%;border-radius:99px;background:var(--grad);transition:width .7s;}

/* BUTTON */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:13px 20px;border-radius:var(--rs);border:none;
  cursor:pointer;font-size:.86rem;font-weight:700;
  font-family:'Plus Jakarta Sans',sans-serif;
  transition:all .18s;-webkit-tap-highlight-color:transparent;
}
.btn:active{transform:scale(.96);}
.btn-p{background:var(--grad);color:#fff;box-shadow:0 4px 16px rgba(124,77,255,.4);}
.btn-o{background:transparent;border:1.5px solid var(--p);color:var(--p);}
.btn-bl{width:100%;margin-bottom:9px;}

/* CHAT */
.chat-hdr{
  display:flex;align-items:center;gap:12px;
  background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--r);padding:14px;margin-bottom:12px;
  box-shadow:var(--sh2);
}
.chat-av{
  width:44px;height:44px;border-radius:50%;
  background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-size:1.5rem;flex-shrink:0;
}
.chat-nm{font-weight:800;font-size:.95rem;}
.chat-md{font-size:.7rem;color:var(--t2);margin-top:2px;}
.msgs{
  height:calc(100vh - 350px);min-height:160px;
  overflow-y:auto;display:flex;flex-direction:column;gap:10px;
  padding:2px 0 8px;
}
.msgs::-webkit-scrollbar{width:3px;}
.msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px;}
.msg{max-width:82%;padding:10px 14px;border-radius:18px;font-size:.87rem;line-height:1.52;}
.msg.u{
  background:var(--grad);color:#fff;align-self:flex-end;
  border-bottom-right-radius:4px;
}
.msg.b{
  background:var(--card2);border:1.5px solid var(--border);
  align-self:flex-start;border-bottom-left-radius:4px;
}
.msg.ty{color:var(--t2);font-style:italic;}
.inp-row{display:flex;gap:8px;margin-top:10px;}
.cinput{
  flex:1;background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--rs);padding:12px 14px;color:var(--t1);
  font-size:.86rem;font-family:'Plus Jakarta Sans',sans-serif;
  outline:none;transition:border .2s;
}
.cinput:focus{border-color:var(--p);}
.sbtn{
  background:var(--grad);border:none;border-radius:var(--rs);
  padding:12px 17px;color:#fff;cursor:pointer;font-size:1.1rem;
  box-shadow:0 4px 14px rgba(124,77,255,.4);transition:transform .15s;
}
.sbtn:active{transform:scale(.9);}

/* LEADERBOARD */
.lb-row{
  display:flex;align-items:center;gap:12px;
  background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--rs);padding:13px 14px;margin-bottom:9px;
  box-shadow:var(--sh2);
}
.lb-rk{font-size:1.4rem;width:32px;text-align:center;flex-shrink:0;}
.lb-av{
  width:36px;height:36px;border-radius:50%;
  background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-weight:800;color:#fff;font-size:.85rem;flex-shrink:0;
}
.lb-inf{flex:1;min-width:0;}
.lb-nm{font-weight:800;font-size:.88rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lb-sb{font-size:.68rem;color:var(--t2);}
.lb-sc{
  font-family:'Syne',sans-serif;font-size:1.05rem;font-weight:800;
  background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;
}

/* BADGES */
.bdg-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}
.bdg{
  background:var(--card);border:1.5px solid var(--border);
  border-radius:var(--rs);padding:14px 8px;text-align:center;transition:all .2s;
}
.bdg.owned{
  border-color:var(--p);
  background:linear-gradient(135deg,rgba(124,77,255,.07),rgba(224,64,251,.05));
}
.bdg.locked{opacity:.35;}
.bdg .be{font-size:1.7rem;}
.bdg .bn{font-size:.62rem;color:var(--t2);margin-top:5px;font-weight:700;}

/* PROFILE INPUTS */
.field{margin-bottom:12px;}
.flbl{font-size:.72rem;font-weight:700;color:var(--t2);margin-bottom:5px;display:block;}
.finput{
  width:100%;background:var(--card2);border:1.5px solid var(--border);
  border-radius:var(--rs);padding:12px 14px;color:var(--t1);
  font-size:.86rem;font-family:'Plus Jakarta Sans',sans-serif;
  outline:none;transition:border .2s;
}
.finput:focus{border-color:var(--p);}
select.finput{cursor:pointer;}

/* PROFILE AVATAR in profile tab */
.prof-av-wrap{text-align:center;margin-bottom:20px;}
.prof-av-big{
  width:84px;height:84px;border-radius:50%;
  border:3px solid var(--p);margin:0 auto;display:block;
  object-fit:cover;background:var(--grad);
}
.prof-av-fb{
  width:84px;height:84px;border-radius:50%;
  border:3px solid var(--p);margin:0 auto;
  background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-family:'Syne',sans-serif;
  font-size:2rem;font-weight:800;color:#fff;
}
.prof-name-big{
  font-family:'Syne',sans-serif;font-weight:800;font-size:1.2rem;
  text-align:center;margin-top:10px;
}
.prof-sub{font-size:.75rem;color:var(--t2);text-align:center;margin-top:3px;}

/* SPINNER */
.spin{
  width:26px;height:26px;border-radius:50%;
  border:3px solid var(--border);border-top-color:var(--p);
  animation:sp .7s linear infinite;margin:28px auto;
}
@keyframes sp{to{transform:rotate(360deg);}}

/* TOAST */
#toast{
  position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
  background:var(--grad);color:#fff;padding:11px 24px;border-radius:99px;
  font-size:.8rem;font-weight:700;opacity:0;transition:opacity .28s;
  pointer-events:none;z-index:999;white-space:nowrap;
  box-shadow:0 8px 24px rgba(124,77,255,.45);
}
#toast.show{opacity:1;}
</style>
</head>
<body>

<!-- HEADER -->
<div class="hero">
  <div class="hero-blur"></div><div class="hero-blur2"></div>
  <div class="hero-top">
    <div class="brand">✦ AI<span>Bot</span></div>
    <button class="theme-btn" id="tBtn" onclick="toggleTheme()">🌙</button>
  </div>
  <!-- User Profile Row -->
  <div class="profile-hero">
    <div class="avatar-wrap">
      <img id="heroAv" class="avatar-img" style="display:none" alt="">
      <div id="heroFb" class="avatar-fallback">?</div>
      <div class="avatar-online"></div>
    </div>
    <div class="profile-info">
      <div class="profile-name" id="heroName">Loading...</div>
      <div class="profile-sub" id="heroSub">@username • AI Bot</div>
    </div>
    <div class="profile-badge" id="heroBadge">⚡ Active</div>
  </div>
</div>

<!-- STATS -->
<div class="stats-row">
  <div class="stat-card"><div class="stat-n" id="sToday">—</div><div class="stat-l">💬 Today</div></div>
  <div class="stat-card"><div class="stat-n" id="sTotal">—</div><div class="stat-l">📨 Total</div></div>
  <div class="stat-card"><div class="stat-n" id="sBdg">—</div><div class="stat-l">🎖️ Badges</div></div>
  <div class="stat-card"><div class="stat-n" id="sRel">—</div><div class="stat-l">💞 Rel</div></div>
</div>

<!-- TABS -->
<div class="tabs-wrap">
  <div class="tab on"  onclick="sw('home')">🏠 Home</div>
  <div class="tab"     onclick="sw('chat')">💬 Chat</div>
  <div class="tab"     onclick="sw('chars')">💞 Companions</div>
  <div class="tab"     onclick="sw('prof')">👤 Profile</div>
  <div class="tab"     onclick="sw('top')">🏆 Top</div>
  <div class="tab"     onclick="sw('bdgs')">🎖️ Badges</div>
</div>

<!-- HOME -->
<div class="page on" id="pg-home">
  <div class="sl">⚡ Quick Actions</div>
  <div class="qa-grid">
    <div class="qa-btn primary" onclick="sw('chat')">
      <span class="qi">💬</span><span class="ql">Chat with AI</span>
    </div>
    <div class="qa-btn" onclick="sw('chars')">
      <span class="qi">💞</span><span class="ql">Companions</span>
    </div>
    <div class="qa-btn" onclick="sw('prof')">
      <span class="qi">👤</span><span class="ql">My Profile</span>
    </div>
    <div class="qa-btn" onclick="sw('top')">
      <span class="qi">🏆</span><span class="ql">Leaderboard</span>
    </div>
  </div>
  <div class="sl">🏆 Top 3</div>
  <div id="homeLb"><div class="spin"></div></div>
</div>

<!-- CHAT -->
<div class="page" id="pg-chat">
  <div class="chat-hdr">
    <div class="chat-av" id="cAv">🤖</div>
    <div>
      <div class="chat-nm" id="cName">AI Assistant</div>
      <div class="chat-md" id="cMood">Ready to chat!</div>
    </div>
    <button class="btn btn-o" style="padding:7px 12px;font-size:.72rem;margin-left:auto"
            onclick="sw('chars')">Change</button>
  </div>
  <div class="msgs" id="msgs"></div>
  <div class="inp-row">
    <input class="cinput" id="cin" placeholder="Kuch bhi likho..."
           autocomplete="off"
           onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}">
    <button class="sbtn" onclick="sendChat()">➤</button>
  </div>
</div>

<!-- COMPANIONS -->
<div class="page" id="pg-chars">
  <div class="sl">💞 AI Companions</div>
  <div id="charList"><div class="spin"></div></div>
</div>

<!-- PROFILE -->
<div class="page" id="pg-prof">
  <div class="card">
    <div class="prof-av-wrap">
      <img id="profAv" class="prof-av-big" style="display:none" alt="">
      <div id="profFb" class="prof-av-fb">?</div>
      <div class="prof-name-big" id="profName">—</div>
      <div class="prof-sub" id="profSub">Telegram User</div>
    </div>
    <div id="profFields"><div class="spin"></div></div>
    <button class="btn btn-p btn-bl" onclick="saveProfile()" style="margin-top:14px">
      💾 Save Profile
    </button>
  </div>
</div>

<!-- LEADERBOARD -->
<div class="page" id="pg-top">
  <div class="sl">🏆 Leaderboard</div>
  <div id="lbList"><div class="spin"></div></div>
</div>

<!-- BADGES -->
<div class="page" id="pg-bdgs">
  <div class="sl">🎖️ Badges</div>
  <div id="bdgProg" style="margin-bottom:16px"></div>
  <div class="bdg-grid" id="bdgGrid"></div>
</div>

<div id="toast"></div>

<script>
const tg  = window.Telegram?.WebApp;
const tgU = tg?.initDataUnsafe?.user || {};
const API = '';

if(tg){tg.expand();tg.ready();}

// ── Theme ─────────────────────────────────────────────
let dk = localStorage.getItem('theme')==='dk';
function applyTheme(){document.body.classList.toggle('dk',dk);}
function toggleTheme(){dk=!dk;localStorage.setItem('theme',dk?'dk':'lt');applyTheme();document.getElementById('tBtn').textContent=dk?'☀️':'🌙';}
applyTheme();
if(dk)document.getElementById('tBtn').textContent='☀️';

// ── User profile photo ─────────────────────────────────
const uid   = tgU.id    || 0;
const uname = tgU.first_name || 'User';
const uuser = tgU.username  || '';

document.getElementById('heroName').textContent = uname;
document.getElementById('heroSub').textContent  = uuser ? `@${uuser} • AI Bot` : 'AI Bot User';
document.getElementById('heroFb').textContent   = uname[0].toUpperCase();
document.getElementById('profName').textContent = uname;
document.getElementById('profSub').textContent  = uuser ? `@${uuser}` : 'Telegram User';
document.getElementById('profFb').textContent   = uname[0].toUpperCase();

// Try to get Telegram profile photo via API
async function loadUserPhoto(){
  try{
    const r = await fetch(`${API}/api/me?uid=${uid}`);
    const d = await r.json();
    if(d.photo_url){
      // Show photo in hero
      const hImg = document.getElementById('heroAv');
      hImg.src = d.photo_url;
      hImg.onload = ()=>{hImg.style.display='block';document.getElementById('heroFb').style.display='none';};
      // Show photo in profile tab
      const pImg = document.getElementById('profAv');
      pImg.src = d.photo_url;
      pImg.onload = ()=>{pImg.style.display='block';document.getElementById('profFb').style.display='none';};
    }
    // Stats
    document.getElementById('sToday').textContent = d.today  ?? '—';
    document.getElementById('sTotal').textContent = d.total  ?? '—';
    document.getElementById('sBdg').textContent   = d.badges ?? '—';
    document.getElementById('sRel').textContent   = d.best_rel ?? '—';
    if(d.badges > 0) document.getElementById('heroBadge').textContent = `🎖️ ${d.badges} Badges`;
  }catch(e){}
}
loadUserPhoto();

// ── Helpers ───────────────────────────────────────────
async function GET(p){const r=await fetch(`${API}${p}?uid=${uid}`);return r.json();}
async function POST(p,b){const r=await fetch(`${API}${p}?uid=${uid}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2500);}
function relLabel(p){if(p<5)return'👋 Strangers';if(p<15)return'🙂 Acquaintances';if(p<30)return'😊 Friends';if(p<60)return'🥰 Close Friends';if(p<100)return'💕 Best Friends';return'💞 Soulmates';}

// ── Tabs ──────────────────────────────────────────────
const TABS=['home','chat','chars','prof','top','bdgs'];
function sw(n){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('on',TABS[i]===n));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));
  document.getElementById('pg-'+n).classList.add('on');
  ({chars:loadChars,prof:loadProf,top:loadTop,bdgs:loadBdgs,home:loadHome})[n]?.();
  if(n==='chat')scrollMsgs();
}

// ── Home ──────────────────────────────────────────────
async function loadHome(){
  try{
    const lb=await GET('/api/leaderboard');
    const medals=['🥇','🥈','🥉'];
    document.getElementById('homeLb').innerHTML=
      (lb.data||[]).slice(0,3).map((u,i)=>
        `<div class="lb-row">
          <div class="lb-rk">${medals[i]}</div>
          <div class="lb-av">${esc((u.name||'?')[0].toUpperCase())}</div>
          <div class="lb-inf"><div class="lb-nm">${esc(u.name)}</div><div class="lb-sb">Today: ${u.today}</div></div>
          <div class="lb-sc">${u.total}</div>
        </div>`
      ).join('')||'<div style="color:var(--t2);padding:12px">No data yet.</div>';
  }catch(e){}
}

// ── Chars ─────────────────────────────────────────────
const EMOJIS=['🌸','😏','📚','💻','🎨'];
async function loadChars(){
  try{
    const d=await GET('/api/characters');
    document.getElementById('charList').innerHTML=
      (d.characters||[]).map((c,i)=>{
        const pts=c.relationship||0,pct=Math.min(100,pts);
        return`<div class="char-card">
          <div class="char-banner"><span class="ce">${EMOJIS[i%5]}</span></div>
          <div class="char-body">
            <div class="char-name">${esc(c.name)}, ${c.age}</div>
            <div class="char-tag">${esc(c.tagline)}</div>
            <div class="prog-w">
              <div class="prog-m"><span>${relLabel(pts)}</span><span>${pts} pts</span></div>
              <div class="prog-b"><div class="prog-f" style="width:${pct}%"></div></div>
            </div>
            <div style="font-size:.72rem;color:var(--t2);margin-bottom:10px;font-weight:600">Mood: ${esc(c.mood||'')}</div>
            <button class="btn btn-p btn-bl" onclick='selChar(${JSON.stringify(c).replace(/"/g,"&quot;")})'>
              💬 Chat with ${esc(c.name)}
            </button>
          </div>
        </div>`;
      }).join('');
  }catch(e){document.getElementById('charList').innerHTML='<div class="card" style="color:var(--t2)">Could not load.</div>';}
}

// ── Chat ──────────────────────────────────────────────
let curChar=null,chatHist=[];
const emojiMap={'aria':'🌸','zara':'😏','riya':'📚','neo':'💻','luna':'🎨'};

function selChar(c){
  curChar=c;chatHist=[];
  document.getElementById('cAv').textContent=emojiMap[c.id]||'💞';
  document.getElementById('cName').textContent=`${c.name}, ${c.age}`;
  document.getElementById('cMood').textContent=`Mood: ${c.mood||'Ready!'}`;
  document.getElementById('msgs').innerHTML='';
  addMsg('b',`${emojiMap[c.id]||'💞'} Hey! I'm ${c.name}. ${c.intro}`);
  sw('chat');
}
function addMsg(r,t){
  const w=document.getElementById('msgs'),d=document.createElement('div');
  d.className=`msg ${r}`;d.textContent=t;w.appendChild(d);scrollMsgs();
}
function scrollMsgs(){const w=document.getElementById('msgs');if(w)setTimeout(()=>w.scrollTop=w.scrollHeight,60);}
async function sendChat(){
  const inp=document.getElementById('cin'),text=inp.value.trim();
  if(!text)return;inp.value='';
  addMsg('u',text);chatHist.push({role:'user',content:text});
  const w=document.getElementById('msgs');
  const t=document.createElement('div');
  t.className='msg b ty';t.id='typ';t.textContent='● ● ●';
  w.appendChild(t);scrollMsgs();
  try{
    const d=await POST('/api/chat',{message:text,history:chatHist.slice(-10),character:curChar?.id||null});
    document.getElementById('typ')?.remove();
    const r=d.reply||'❌ No response';
    addMsg('b',r);chatHist.push({role:'assistant',content:r});
    if(d.new_badges?.length)toast(`🎉 Badge: ${d.new_badges[0]}`);
  }catch(e){document.getElementById('typ')?.remove();addMsg('b','❌ Error.');}
}

// ── Profile ───────────────────────────────────────────
async function loadProf(){
  try{
    const d=await GET('/api/me');
    const p=d.profile||{};
    const fields=[{k:'name',l:'👤 Name',t:'text'},{k:'age',l:'🎂 Age',t:'number'},
                  {k:'bio',l:'📝 Bio',t:'text'},{k:'mood',l:'😊 Mood',t:'text'}];
    document.getElementById('profFields').innerHTML=
      fields.map(f=>`<div class="field">
        <label class="flbl">${f.l}</label>
        <input class="finput" id="p-${f.k}" type="${f.t}" value="${esc(p[f.k]||'')}" placeholder="${f.l}...">
      </div>`).join('')+
      `<div class="field"><label class="flbl">⭐ Zodiac</label>
      <select class="finput" id="p-zodiac">
        <option value="">Select...</option>
        ${['Aries','Taurus','Gemini','Cancer','Leo','Virgo','Libra','Scorpio','Sagittarius','Capricorn','Aquarius','Pisces'].map(s=>`<option${p.zodiac===s?' selected':''}>${s}</option>`).join('')}
      </select></div>
      <div class="field"><label class="flbl">🌐 Language</label>
      <select class="finput" id="p-lang">
        ${['Hinglish','Hindi','English'].map(l=>`<option${(p.lang||'Hinglish')===l?' selected':''}>${l}</option>`).join('')}
      </select></div>`;
  }catch(e){}
}
async function saveProfile(){
  const data={name:document.getElementById('p-name')?.value.trim()||'',
    age:document.getElementById('p-age')?.value.trim()||'',
    bio:document.getElementById('p-bio')?.value.trim()||'',
    mood:document.getElementById('p-mood')?.value.trim()||'',
    zodiac:document.getElementById('p-zodiac')?.value||'',
    lang:document.getElementById('p-lang')?.value||'Hinglish'};
  try{
    await POST('/api/profile',data);
    toast('✅ Profile saved!');
    if(tg?.HapticFeedback)tg.HapticFeedback.notificationOccurred('success');
    // Update hero name if changed
    if(data.name){document.getElementById('heroName').textContent=data.name;document.getElementById('profName').textContent=data.name;}
  }catch(e){toast('❌ Failed');}
}

// ── Leaderboard ───────────────────────────────────────
async function loadTop(){
  try{
    const d=await GET('/api/leaderboard');
    const medals=['🥇','🥈','🥉'];
    document.getElementById('lbList').innerHTML=
      (d.data||[]).map((u,i)=>
        `<div class="lb-row">
          <div class="lb-rk">${medals[i]||'#'+(i+1)}</div>
          <div class="lb-av">${esc((u.name||'?')[0].toUpperCase())}</div>
          <div class="lb-inf"><div class="lb-nm">${esc(u.name)}</div><div class="lb-sb">Today: ${u.today}</div></div>
          <div class="lb-sc">${u.total}</div>
        </div>`
      ).join('')||'<div style="color:var(--t2);padding:14px">No data yet.</div>';
  }catch(e){}
}

// ── Badges ────────────────────────────────────────────
async function loadBdgs(){
  try{
    const d=await GET('/api/badges');
    const owned=new Set(d.owned||[]),all=d.all||[];
    const pct=all.length?Math.round(owned.size/all.length*100):0;
    document.getElementById('bdgProg').innerHTML=
      `<div class="prog-w"><div class="prog-m"><span>Progress</span><span>${owned.size}/${all.length}</span></div>
      <div class="prog-b"><div class="prog-f" style="width:${pct}%"></div></div></div>`;
    document.getElementById('bdgGrid').innerHTML=
      all.map(b=>`<div class="bdg ${owned.has(b.key)?'owned':'locked'}">
        <div class="be">${b.emoji}</div>
        <div class="bn">${esc(b.name)}</div>
        ${owned.has(b.key)?'':'<div style="font-size:.58rem;color:var(--t3)">🔒</div>'}
      </div>`).join('');
  }catch(e){}
}

// Init
loadHome();
</script>
</body>
</html>"""


@web_app.get("/app", response_class=HTMLResponse)
async def mini_app_route():
    return HTMLResponse(_MINI_APP_HTML)


@web_app.get("/api/me")
async def api_me(uid: int = 0):
    if not uid:
        return JSONResponse({"error": "no uid"}, status_code=400)
    prof   = db.get_profile(uid)
    bdgs   = db.get_badges(uid)
    rels   = db._relationships.get(uid, {})
    best_r = max(rels.values()) if rels else 0

    # Try to get Telegram profile photo URL
    photo_url = None
    if _ptb_app:
        try:
            photos = await _ptb_app.bot.get_user_profile_photos(uid, limit=1)
            if photos.photos:
                file = await _ptb_app.bot.get_file(photos.photos[0][0].file_id)
                photo_url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file.file_path}"
        except Exception:
            pass

    return JSONResponse({"today": db.get_usage_today(uid), "total": db.get_total_messages(uid),
                         "badges": len(bdgs), "best_rel": best_r, "profile": prof,
                         "photo_url": photo_url})


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
    from characters import CHARACTERS as CHARS, get_char_pic
    result = []
    for c in CHARS:
        pts = db.get_relationship(uid, c["id"]) if uid else 0
        result.append({"id": c["id"], "name": c["name"], "age": c["age"],
                       "tagline": c["tagline"], "intro": c["intro"],
                       "mood": db.get_char_mood(c["id"]), "relationship": pts,
                       "pic": get_char_pic(c)})
    return JSONResponse({"characters": result})


@web_app.get("/api/badges")
async def api_badges(uid: int = 0):
    owned = list(db.get_badges(uid)) if uid else []
    all_b = [{"key": k, "emoji": v[0], "name": v[1], "desc": v[2]}
             for k, v in db.BADGE_DEFINITIONS.items()]
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
    if not uid:
        return JSONResponse({"error": "no uid"}, status_code=400)
    ok, reason = await can_use_bot(uid)
    if not ok:
        return JSONResponse({"reply": reason})
    try:
        body      = await request.json()
        message   = body.get("message", "")
        history   = body.get("history", [])
        char_id   = body.get("character")
        state     = get_state(uid)

        if char_id:
            from characters import get_character as gc
            char       = gc(char_id)
            sys_prompt = char["prompt"] if char else current_system_prompt
            sys_prompt += f"\n\nMood: {db.get_char_mood(char_id)}."
            prof = db.get_profile(uid)
            if prof.get("name"):
                sys_prompt += f" User ka naam {prof['name']} hai."
            if state["character"] != char_id:
                state.update({"mode": "character", "character": char_id, "history": []})
        else:
            sys_prompt = current_system_prompt

        msgs = [{"role": "system", "content": sys_prompt}] + history + \
               [{"role": "user", "content": message}]

        full = ""
        async for chunk in stream_ai_response(state["provider"], state["model"], msgs):
            full += chunk

        db.record_usage(uid)
        await db.persist_usage(uid)
        if char_id:
            await db.add_relationship_point(uid, char_id)
        new_b = await db.check_and_award_badges(uid)
        return JSONResponse({"reply": full, "new_badges": new_b})
    except Exception as e:
        logger.error("Mini app chat: %s", e)
        return JSONResponse({"reply": f"❌ Error: {e}"}, status_code=500)


@web_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    s = db.get_global_stats()
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
<title>Dashboard</title>
<style>body{{font-family:sans-serif;background:#0d0b1e;color:#e2e8f0;padding:30px}}
h1{{background:linear-gradient(135deg,#7c4dff,#ff4081);-webkit-background-clip:text;
-webkit-text-fill-color:transparent;font-size:2rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:20px 0}}
.card{{background:#1a1535;border-radius:16px;padding:20px;text-align:center;border:1px solid #2d2060}}
.num{{font-size:2rem;font-weight:800;background:linear-gradient(135deg,#7c4dff,#ff4081);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.lbl{{color:#9d89cc;font-size:.8rem;margin-top:4px}}</style></head>
<body><h1>🤖 AI Bot Dashboard</h1>
<div class="grid">
<div class="card"><div class="num">{s['total_users']}</div><div class="lbl">👥 Users</div></div>
<div class="card"><div class="num">{s['msgs_today']}</div><div class="lbl">💬 Today</div></div>
<div class="card"><div class="num">{s['total_msgs']}</div><div class="lbl">📨 Total</div></div>
<div class="card"><div class="num">{s['active_today']}</div><div class="lbl">🟢 Active</div></div>
<div class="card"><div class="num">{s['banned']}</div><div class="lbl">🔨 Banned</div></div>
</div>
<p style="color:#9d89cc">MongoDB: {'✅ Connected' if s['mongo_enabled'] else '⚠️ In-memory'}</p>
</body></html>""")


@web_app.get("/")
@web_app.head("/")
async def health():
    return {"ok": True, "status": "AI Bot running 🤖"}


@web_app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        body   = await request.body()
        update = Update.de_json(json.loads(body), _ptb_app.bot)
        await _ptb_app.process_update(update)
    except Exception as e:
        logger.error("Webhook error: %s", e)
    return Response(status_code=200)



async def _polling_mode():
    ptb = build_ptb_app()
    await db.init_db()
    async with ptb:
        await ptb.start()
        await ptb.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.Event().wait()

def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set!")
    if config.WEBHOOK_URL:
        uvicorn.run(web_app, host="0.0.0.0", port=config.PORT, log_level="info")
    else:
        asyncio.run(_polling_mode())

if __name__ == "__main__":
    main()

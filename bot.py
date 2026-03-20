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

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 AI Se Baat Karo", callback_data="start:chat"),
         InlineKeyboardButton("💞 Companions",      callback_data="start:meet")],
        [InlineKeyboardButton("🔌 Model Chunein",   callback_data="start:model"),
         InlineKeyboardButton("👤 Profile",         callback_data="start:profile")],
        [InlineKeyboardButton("📊 Meri Stats",      callback_data="start:status"),
         InlineKeyboardButton("🏆 Leaderboard",     callback_data="start:top")],
        [InlineKeyboardButton("📱 Mini App", web_app=WebAppInfo(url=f"{config.WEBHOOK_URL}/app"))
         ] if config.WEBHOOK_URL else [],
        [
            InlineKeyboardButton(config.OWNER_CONTACTS[0][0], url=config.OWNER_CONTACTS[0][1]),
            InlineKeyboardButton(config.OWNER_CONTACTS[1][0], url=config.OWNER_CONTACTS[1][1]),
        ],
    ])
    kb.inline_keyboard = [r for r in kb.inline_keyboard if r]

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

    # Build system prompt
    if state["mode"] == "character":
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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AI Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root {
  --bg: #f0f4ff; --surface: #ffffff; --surface2: #f8f0ff;
  --border: #e8e0ff; --text: #1a1040; --text2: #6b5b95;
  --accent: #7c4dff; --accent2: #ff4081; --accent3: #00bcd4;
  --grad: linear-gradient(135deg, #7c4dff, #ff4081);
  --grad2: linear-gradient(135deg, #00bcd4, #7c4dff);
  --shadow: 0 4px 24px rgba(124,77,255,.15);
  --radius: 20px; --radius-sm: 12px;
}
.dark {
  --bg: #0d0b1e; --surface: #1a1535; --surface2: #221b40;
  --border: #2d2060; --text: #f0e8ff; --text2: #9d89cc;
  --shadow: 0 4px 24px rgba(124,77,255,.3);
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { font-family:'Nunito',sans-serif; background:var(--bg); color:var(--text);
       min-height:100vh; transition:background .3s,color .3s; }

/* ── Brand ─────────────────────────────────────── */
.brand { font-family:'Fredoka One',cursive; }

/* ── Header ─────────────────────────────────────── */
.header {
  background: var(--grad);
  padding: 20px 20px 28px;
  position: relative; overflow: hidden;
}
.header::after {
  content:''; position:absolute; bottom:-20px; left:0; right:0;
  height:40px; background:var(--bg); border-radius:50% 50% 0 0;
  transition: background .3s;
}
.header-top { display:flex; justify-content:space-between; align-items:center; }
.header h1 { font-family:'Fredoka One',cursive; font-size:1.6rem; color:#fff; }
.theme-btn {
  background:rgba(255,255,255,.2); border:none; border-radius:99px;
  padding:6px 12px; color:#fff; cursor:pointer; font-size:1rem;
}
.user-pill {
  display:inline-flex; align-items:center; gap:8px; margin-top:12px;
  background:rgba(255,255,255,.2); border-radius:99px; padding:6px 14px;
}
.avatar {
  width:32px; height:32px; border-radius:50%;
  background:rgba(255,255,255,.3);
  display:flex; align-items:center; justify-content:center;
  font-weight:800; color:#fff; font-size:.85rem;
}
.user-pill span { color:#fff; font-size:.85rem; font-weight:700; }

/* ── Stats strip ─────────────────────────────────── */
.stats-strip {
  display:flex; gap:10px; padding:20px 16px 8px;
  overflow-x:auto; -webkit-overflow-scrolling:touch;
}
.stats-strip::-webkit-scrollbar { display:none; }
.stat-pill {
  flex-shrink:0; background:var(--surface); border:1px solid var(--border);
  border-radius:99px; padding:10px 18px; text-align:center;
  box-shadow: var(--shadow);
}
.stat-pill .num { font-family:'Fredoka One',cursive; font-size:1.3rem;
  background:var(--grad); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.stat-pill .lbl { font-size:.68rem; color:var(--text2); margin-top:1px; }

/* ── Tabs ────────────────────────────────────────── */
.tabs {
  display:flex; gap:6px; padding:4px 16px 12px;
  overflow-x:auto; -webkit-overflow-scrolling:touch;
}
.tabs::-webkit-scrollbar { display:none; }
.tab {
  flex-shrink:0; padding:8px 18px; border-radius:99px; cursor:pointer;
  font-size:.8rem; font-weight:700; border:2px solid var(--border);
  background:var(--surface); color:var(--text2); transition:all .2s;
}
.tab.active {
  background:var(--grad); color:#fff; border-color:transparent;
  box-shadow:0 4px 14px rgba(124,77,255,.35);
}

/* ── Pages ───────────────────────────────────────── */
.page { display:none; padding:0 16px 100px; }
.page.active { display:block; }

/* ── Section title ───────────────────────────────── */
.sec { font-size:.75rem; font-weight:800; color:var(--accent);
       text-transform:uppercase; letter-spacing:.08em; margin:16px 0 10px; }

/* ── Card ────────────────────────────────────────── */
.card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); padding:18px; margin-bottom:12px;
  box-shadow:var(--shadow);
}
.card-row { display:flex; align-items:center; gap:12px; }

/* ── Char card ───────────────────────────────────── */
.char-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); overflow:hidden; margin-bottom:14px;
  box-shadow:var(--shadow);
}
.char-banner {
  height:90px; display:flex; align-items:center; justify-content:center;
  font-size:3rem; position:relative; overflow:hidden;
}
.char-banner::before {
  content:''; position:absolute; inset:0;
  background:var(--grad2); opacity:.85;
}
.char-banner span { position:relative; z-index:1; }
.char-body { padding:16px; }
.char-name { font-family:'Fredoka One',cursive; font-size:1.1rem; }
.char-tag  { font-size:.75rem; color:var(--text2); margin:3px 0 10px; }

/* ── Progress bar ────────────────────────────────── */
.prog-wrap { margin:8px 0; }
.prog-meta { display:flex; justify-content:space-between;
             font-size:.7rem; color:var(--text2); margin-bottom:5px; }
.prog-bar  { height:8px; background:var(--border); border-radius:99px; overflow:hidden; }
.prog-fill { height:100%; border-radius:99px; background:var(--grad); transition:width .7s; }

/* ── Button ──────────────────────────────────────── */
.btn {
  display:inline-flex; align-items:center; justify-content:center; gap:8px;
  padding:12px 20px; border-radius:var(--radius-sm); border:none;
  cursor:pointer; font-size:.88rem; font-weight:700; font-family:'Nunito',sans-serif;
  transition:all .2s; -webkit-tap-highlight-color:transparent;
}
.btn:active { transform:scale(.96); }
.btn-primary { background:var(--grad); color:#fff;
               box-shadow:0 4px 14px rgba(124,77,255,.4); }
.btn-outline { background:transparent; border:2px solid var(--accent); color:var(--accent); }
.btn-block   { width:100%; margin-bottom:8px; }

/* ── Chat ────────────────────────────────────────── */
.chat-header {
  display:flex; align-items:center; gap:12px;
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); padding:14px; margin-bottom:12px;
}
.chat-avatar {
  width:44px; height:44px; border-radius:50%;
  background:var(--grad); display:flex; align-items:center;
  justify-content:center; font-size:1.4rem; flex-shrink:0;
}
.chat-name { font-weight:800; font-size:.95rem; }
.chat-mood { font-size:.72rem; color:var(--text2); margin-top:2px; }
.chat-msgs {
  height:calc(100vh - 340px); min-height:200px;
  overflow-y:auto; display:flex; flex-direction:column; gap:10px; padding:4px 0 10px;
}
.chat-msgs::-webkit-scrollbar { width:3px; }
.chat-msgs::-webkit-scrollbar-thumb { background:var(--border); border-radius:99px; }
.msg { max-width:82%; padding:10px 14px; border-radius:18px;
       font-size:.88rem; line-height:1.5; }
.msg.user { background:var(--grad); color:#fff; align-self:flex-end;
            border-bottom-right-radius:4px; }
.msg.bot  { background:var(--surface2); border:1px solid var(--border);
            align-self:flex-start; border-bottom-left-radius:4px; }
.msg.typing { color:var(--text2); font-style:italic; }
.chat-input-row { display:flex; gap:8px; margin-top:10px; }
.chat-input {
  flex:1; background:var(--surface); border:2px solid var(--border);
  border-radius:var(--radius-sm); padding:11px 14px; color:var(--text);
  font-size:.88rem; font-family:'Nunito',sans-serif; outline:none;
  transition:border .2s;
}
.chat-input:focus { border-color:var(--accent); }
.send-btn {
  background:var(--grad); border:none; border-radius:var(--radius-sm);
  padding:11px 16px; color:#fff; cursor:pointer; font-size:1.1rem;
  box-shadow:0 4px 12px rgba(124,77,255,.4); transition:transform .15s;
}
.send-btn:active { transform:scale(.92); }

/* ── Leaderboard ─────────────────────────────────── */
.lb-row {
  display:flex; align-items:center; gap:12px;
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-sm); padding:14px; margin-bottom:8px;
  box-shadow:var(--shadow);
}
.lb-rank { font-size:1.4rem; width:34px; text-align:center; flex-shrink:0; }
.lb-info { flex:1; }
.lb-name { font-weight:800; font-size:.9rem; }
.lb-sub  { font-size:.7rem; color:var(--text2); }
.lb-score { font-family:'Fredoka One',cursive; font-size:1.1rem;
            background:var(--grad); -webkit-background-clip:text;
            -webkit-text-fill-color:transparent; }

/* ── Badges ──────────────────────────────────────── */
.badges-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.badge-item {
  background:var(--surface); border:2px solid var(--border);
  border-radius:var(--radius-sm); padding:14px 8px; text-align:center;
  transition:all .2s;
}
.badge-item.owned { border-color:var(--accent);
                    background:linear-gradient(135deg,rgba(124,77,255,.08),rgba(255,64,129,.08)); }
.badge-item.locked { opacity:.35; }
.badge-emoji { font-size:1.8rem; }
.badge-name  { font-size:.65rem; color:var(--text2); margin-top:5px; font-weight:700; }

/* ── Profile inputs ──────────────────────────────── */
.field-wrap { margin-bottom:12px; }
.field-label { font-size:.75rem; color:var(--text2); font-weight:700;
               margin-bottom:5px; display:block; }
.field-input {
  width:100%; background:var(--surface2); border:2px solid var(--border);
  border-radius:var(--radius-sm); padding:11px 14px; color:var(--text);
  font-size:.88rem; font-family:'Nunito',sans-serif; outline:none;
  transition:border .2s;
}
.field-input:focus { border-color:var(--accent); }
select.field-input { cursor:pointer; }

/* ── Spinner ─────────────────────────────────────── */
.spinner {
  width:24px; height:24px; border-radius:50%;
  border:3px solid var(--border); border-top-color:var(--accent);
  animation:spin .7s linear infinite; margin:24px auto;
}
@keyframes spin { to { transform:rotate(360deg); } }

/* ── Toast ───────────────────────────────────────── */
#toast {
  position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
  background:var(--grad); color:#fff; padding:10px 22px; border-radius:99px;
  font-size:.82rem; font-weight:700; opacity:0; transition:opacity .3s;
  pointer-events:none; z-index:999; white-space:nowrap;
  box-shadow:0 6px 20px rgba(124,77,255,.4);
}
#toast.show { opacity:1; }
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <h1 class="brand">✨ AI Bot</h1>
    <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">🌙</button>
  </div>
  <div class="user-pill">
    <div class="avatar" id="av">?</div>
    <span id="uname">Loading...</span>
  </div>
  <div style="margin-top:8px;color:rgba(255,255,255,.8);font-size:.78rem" id="tagline">
    ➵⋆🪐 ᴛᴇᴄʜɴɪᴄᴀʟ_sᴇʀᴇɴᴀ 𓂃
  </div>
</div>

<div class="stats-strip" id="stats-strip">
  <div class="stat-pill"><div class="num" id="s-today">—</div><div class="lbl">💬 Today</div></div>
  <div class="stat-pill"><div class="num" id="s-total">—</div><div class="lbl">📨 Total</div></div>
  <div class="stat-pill"><div class="num" id="s-badges">—</div><div class="lbl">🎖️ Badges</div></div>
  <div class="stat-pill"><div class="num" id="s-rel">—</div><div class="lbl">💞 Best Rel</div></div>
</div>

<div class="tabs">
  <div class="tab active"  onclick="sw('home')">🏠 Home</div>
  <div class="tab"         onclick="sw('chat')">💬 Chat</div>
  <div class="tab"         onclick="sw('companions')">💞 Companions</div>
  <div class="tab"         onclick="sw('profile')">👤 Profile</div>
  <div class="tab"         onclick="sw('top')">🏆 Top</div>
  <div class="tab"         onclick="sw('badges')">🎖️ Badges</div>
</div>

<!-- HOME -->
<div class="page active" id="page-home">
  <div class="sec">⚡ Quick Actions</div>
  <button class="btn btn-primary btn-block" onclick="sw('chat')">💬 AI Se Baat Karo</button>
  <button class="btn btn-outline btn-block" onclick="sw('companions')">💞 Companions Browse Karo</button>

  <div class="sec">🏆 Top 3</div>
  <div id="home-lb"><div class="spinner"></div></div>
</div>

<!-- CHAT -->
<div class="page" id="page-chat">
  <div class="chat-header">
    <div class="chat-avatar" id="chat-av">🤖</div>
    <div>
      <div class="chat-name" id="chat-cname">AI Assistant</div>
      <div class="chat-mood" id="chat-cmood">Ready to chat!</div>
    </div>
    <button class="btn btn-outline" style="padding:7px 12px;font-size:.72rem;margin-left:auto"
            onclick="sw('companions')">Change</button>
  </div>
  <div class="chat-msgs" id="chat-msgs"></div>
  <div class="chat-input-row">
    <input class="chat-input" id="cin" placeholder="Kuch bhi likhо..."
           autocomplete="off" onkeydown="if(e=>e.key==='Enter',event)sendChat()">
    <button class="send-btn" onclick="sendChat()">➤</button>
  </div>
</div>

<!-- COMPANIONS -->
<div class="page" id="page-companions">
  <div class="sec">💞 AI Companions</div>
  <div id="chars-list"><div class="spinner"></div></div>
</div>

<!-- PROFILE -->
<div class="page" id="page-profile">
  <div class="card">
    <div class="sec" style="margin-top:0">👤 Edit Profile</div>
    <div id="prof-fields"><div class="spinner"></div></div>
    <button class="btn btn-primary btn-block" onclick="saveProfile()" style="margin-top:14px">
      💾 Save Profile
    </button>
  </div>
</div>

<!-- LEADERBOARD -->
<div class="page" id="page-top">
  <div class="sec">🏆 Leaderboard</div>
  <div id="lb-list"><div class="spinner"></div></div>
</div>

<!-- BADGES -->
<div class="page" id="page-badges">
  <div class="sec">🎖️ Tumhare Badges</div>
  <div id="badges-prog" style="margin-bottom:16px"></div>
  <div class="badges-grid" id="badges-grid"></div>
</div>

<div id="toast"></div>

<script>
const tg    = window.Telegram?.WebApp;
const tgU   = tg?.initDataUnsafe?.user || {};
const API   = '';
let currentChar = null, chatHistory = [], isDark = false;

if (tg) { tg.expand(); tg.ready(); }

// Theme
function toggleTheme() {
  isDark = !isDark;
  document.body.classList.toggle('dark', isDark);
  document.getElementById('themeBtn').textContent = isDark ? '☀️' : '🌙';
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
}
if (localStorage.getItem('theme') === 'dark') toggleTheme();

// User init
document.getElementById('uname').textContent = tgU.first_name || 'User';
document.getElementById('av').textContent     = (tgU.first_name || 'U')[0].toUpperCase();

// Tab switch
function sw(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    t.classList.toggle('active', ['home','chat','companions','profile','top','badges'][i] === name);
  });
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  const loaders = {companions: loadChars, profile: loadProfile,
                   top: loadTop, badges: loadBadges, home: loadHome};
  if (loaders[name]) loaders[name]();
  if (name === 'chat') scrollMsgs();
}

// API helpers
const uid = tgU.id || 0;
async function GET(path) {
  const r = await fetch(`${API}${path}?uid=${uid}`);
  return r.json();
}
async function POST(path, body) {
  const r = await fetch(`${API}${path}?uid=${uid}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  return r.json();
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function relLabel(pts) {
  if(pts<5)   return '👋 Strangers';
  if(pts<15)  return '🙂 Acquaintances';
  if(pts<30)  return '😊 Friends';
  if(pts<60)  return '🥰 Close Friends';
  if(pts<100) return '💕 Best Friends';
  return '💞 Soulmates';
}

// Load home
async function loadHome() {
  try {
    const d = await GET('/api/me');
    document.getElementById('s-today').textContent  = d.today   ?? '—';
    document.getElementById('s-total').textContent  = d.total   ?? '—';
    document.getElementById('s-badges').textContent = d.badges  ?? '—';
    document.getElementById('s-rel').textContent    = d.best_rel ?? '—';
    const lb = await GET('/api/leaderboard');
    const medals = ['🥇','🥈','🥉'];
    document.getElementById('home-lb').innerHTML =
      (lb.data || []).slice(0,3).map((u,i) =>
        `<div class="lb-row">
          <div class="lb-rank">${medals[i]}</div>
          <div class="lb-info"><div class="lb-name">${esc(u.name)}</div></div>
          <div class="lb-score">${u.total}</div>
        </div>`
      ).join('') || '<div style="color:var(--text2);padding:10px">No data yet.</div>';
  } catch(e){}
}

// Load characters
const EMOJIS = ['🌸','😏','📚','💻','🎨'];
async function loadChars() {
  try {
    const d = await GET('/api/characters');
    document.getElementById('chars-list').innerHTML =
      (d.characters || []).map((c,i) => {
        const pts = c.relationship || 0;
        const pct = Math.min(100, pts);
        return `<div class="char-card">
          <div class="char-banner"><span>${EMOJIS[i%EMOJIS.length]}</span></div>
          <div class="char-body">
            <div class="char-name">${esc(c.name)}, ${c.age}</div>
            <div class="char-tag">${esc(c.tagline)}</div>
            <div class="prog-wrap">
              <div class="prog-meta"><span>${relLabel(pts)}</span><span>${pts} pts</span></div>
              <div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>
            </div>
            <div style="font-size:.75rem;color:var(--text2);margin-bottom:10px">Mood: ${esc(c.mood||'')}</div>
            <button class="btn btn-primary btn-block"
                    onclick='selectChar(${JSON.stringify(c).replace(/\"/g,"&quot;")})'>
              💬 Chat with ${esc(c.name)}
            </button>
          </div>
        </div>`;
      }).join('');
  } catch(e) {
    document.getElementById('chars-list').innerHTML = '<div class="card">Could not load.</div>';
  }
}

function selectChar(c) {
  currentChar = c; chatHistory = [];
  document.getElementById('chat-av').textContent    = EMOJIS[['aria','zara','riya','neo','luna'].indexOf(c.id) % EMOJIS.length] || '💞';
  document.getElementById('chat-cname').textContent = `${c.name}, ${c.age}`;
  document.getElementById('chat-cmood').textContent = `Mood: ${c.mood || 'Ready!'}`;
  document.getElementById('chat-msgs').innerHTML    = '';
  addMsg('bot', `💞 Hey! I'm ${c.name}. ${c.intro}`);
  sw('chat');
}

// Chat
function addMsg(role, text) {
  const w = document.getElementById('chat-msgs');
  const d = document.createElement('div');
  d.className = `msg ${role}`;
  d.textContent = text;
  w.appendChild(d);
  scrollMsgs();
}
function scrollMsgs() {
  const w = document.getElementById('chat-msgs');
  if(w) setTimeout(() => w.scrollTop = w.scrollHeight, 60);
}
async function sendChat() {
  const inp  = document.getElementById('cin');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  addMsg('user', text);
  chatHistory.push({role:'user', content:text});

  const w = document.getElementById('chat-msgs');
  const t = document.createElement('div');
  t.className = 'msg bot typing'; t.id = 'typ'; t.textContent = '● ● ●';
  w.appendChild(t); scrollMsgs();

  try {
    const d = await POST('/api/chat', {
      message: text, history: chatHistory.slice(-10),
      character: currentChar?.id || null,
    });
    document.getElementById('typ')?.remove();
    const r = d.reply || '❌ No response';
    addMsg('bot', r);
    chatHistory.push({role:'assistant', content:r});
    if(d.new_badges?.length) toast(`🎉 Badge: ${d.new_badges[0]}`);
  } catch(e) {
    document.getElementById('typ')?.remove();
    addMsg('bot', '❌ Error. Please try again.');
  }
}

// Profile
async function loadProfile() {
  try {
    const d = await GET('/api/me');
    const p = d.profile || {};
    const fields = [
      {k:'name', l:'👤 Name', t:'text'},
      {k:'age',  l:'🎂 Age',  t:'number'},
      {k:'bio',  l:'📝 Bio',  t:'text'},
      {k:'mood', l:'😊 Mood', t:'text'},
    ];
    document.getElementById('prof-fields').innerHTML =
      fields.map(f =>
        `<div class="field-wrap">
          <label class="field-label">${f.l}</label>
          <input class="field-input" id="p-${f.k}" type="${f.t}"
                 value="${esc(p[f.k]||'')}" placeholder="${f.l}...">
        </div>`
      ).join('') +
      `<div class="field-wrap">
        <label class="field-label">⭐ Zodiac</label>
        <select class="field-input" id="p-zodiac">
          <option value="">Select...</option>
          ${['Aries','Taurus','Gemini','Cancer','Leo','Virgo','Libra',
             'Scorpio','Sagittarius','Capricorn','Aquarius','Pisces']
            .map(s=>`<option ${p.zodiac===s?'selected':''}>${s}</option>`).join('')}
        </select>
      </div>
      <div class="field-wrap">
        <label class="field-label">🌐 Language</label>
        <select class="field-input" id="p-lang">
          ${['Hinglish','Hindi','English'].map(l=>
            `<option ${(p.lang||'Hinglish')===l?'selected':''}>${l}</option>`).join('')}
        </select>
      </div>`;
  } catch(e){}
}

async function saveProfile() {
  const data = {
    name:   document.getElementById('p-name')?.value.trim()||'',
    age:    document.getElementById('p-age')?.value.trim()||'',
    bio:    document.getElementById('p-bio')?.value.trim()||'',
    mood:   document.getElementById('p-mood')?.value.trim()||'',
    zodiac: document.getElementById('p-zodiac')?.value||'',
    lang:   document.getElementById('p-lang')?.value||'Hinglish',
  };
  try {
    await POST('/api/profile', data);
    toast('✅ Profile saved!');
    if(tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch(e) { toast('❌ Save failed'); }
}

// Leaderboard
async function loadTop() {
  try {
    const d = await GET('/api/leaderboard');
    const medals = ['🥇','🥈','🥉'];
    document.getElementById('lb-list').innerHTML =
      (d.data||[]).map((u,i)=>
        `<div class="lb-row">
          <div class="lb-rank">${medals[i]||'#'+(i+1)}</div>
          <div class="lb-info">
            <div class="lb-name">${esc(u.name)}</div>
            <div class="lb-sub">Today: ${u.today} msgs</div>
          </div>
          <div class="lb-score">${u.total}</div>
        </div>`
      ).join('') || '<div style="color:var(--text2);padding:14px">No data yet.</div>';
  } catch(e){}
}

// Badges
async function loadBadges() {
  try {
    const d     = await GET('/api/badges');
    const owned = new Set(d.owned||[]);
    const all   = d.all||[];
    const pct   = all.length ? Math.round(owned.size/all.length*100) : 0;
    document.getElementById('badges-prog').innerHTML =
      `<div class="prog-wrap">
        <div class="prog-meta"><span>Progress</span><span>${owned.size}/${all.length}</span></div>
        <div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>
      </div>`;
    document.getElementById('badges-grid').innerHTML =
      all.map(b =>
        `<div class="badge-item ${owned.has(b.key)?'owned':'locked'}">
          <div class="badge-emoji">${b.emoji}</div>
          <div class="badge-name">${esc(b.name)}</div>
          ${owned.has(b.key)?'':'<div style="font-size:.6rem;color:var(--text2)">🔒</div>'}
        </div>`
      ).join('');
  } catch(e){}
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
    return JSONResponse({"today": db.get_usage_today(uid), "total": db.get_total_messages(uid),
                         "badges": len(bdgs), "best_rel": best_r, "profile": prof})


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

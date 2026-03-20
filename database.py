"""
database.py — MongoDB async (motor) + in-memory fallback.
Handles: users, bans, profiles, usage, activity log,
         leaderboard, badges, relationships, spam detection,
         scheduled broadcasts, character mood.
"""

import logging
from collections import deque
from datetime import date, datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── In-memory stores ─────────────────────────────────────────────
_allowed_users: dict  = {}   # {uid: {username, limit, added_by}}
_banned_users:  set   = set()
_profiles:      dict  = {}   # {uid: {name, age, bio, mood, zodiac, lang}}
_usage_today:   dict  = {}   # {uid: {date, count}}
_msg_total:     dict  = {}   # {uid: int}
_relationships: dict  = {}   # {uid: {char_id: int}}
_activity:      dict  = {}   # {uid: {last_seen: datetime, msg_count_today: int}}
_badges:        dict  = {}   # {uid: set of badge keys}
_schedules:     list  = []   # [{id, cron, message, active}]
_char_mood:     dict  = {}   # {char_id: {mood, updated}}

# Spam tracking: {uid: deque of timestamps}
_spam_track: dict = {}

_db            = None
_mongo_enabled = False


# ════════════════════════════════════════════════════════════════
#  INIT
# ════════════════════════════════════════════════════════════════
async def init_db():
    global _db, _mongo_enabled
    if not config.MONGODB_URI:
        logger.warning("MONGODB_URI not set — in-memory only")
        return
    try:
        import motor.motor_asyncio as motor
        client         = motor.AsyncIOMotorClient(config.MONGODB_URI, serverSelectionTimeoutMS=5000)
        _db            = client["telegram_ai_bot"]
        await client.admin.command("ping")
        _mongo_enabled = True
        logger.info("✅ MongoDB connected")
        await _load_from_mongo()
    except Exception as e:
        logger.error("MongoDB failed: %s", e)
        _mongo_enabled = False


async def _load_from_mongo():
    global _allowed_users, _banned_users, _profiles, _msg_total, _relationships, _badges, _schedules
    async for doc in _db.allowed_users.find():
        uid = doc["_id"]
        _allowed_users[uid] = {k: v for k, v in doc.items() if k != "_id"}
    async for doc in _db.banned_users.find():
        _banned_users.add(doc["_id"])
    async for doc in _db.profiles.find():
        uid = doc["_id"]
        _profiles[uid] = {k: v for k, v in doc.items() if k != "_id"}
    async for doc in _db.msg_totals.find():
        _msg_total[doc["_id"]] = doc.get("count", 0)
    async for doc in _db.relationships.find():
        _relationships[doc["_id"]] = doc.get("chars", {})
    async for doc in _db.badges.find():
        _badges[doc["_id"]] = set(doc.get("list", []))
    async for doc in _db.schedules.find():
        _schedules.append({k: v for k, v in doc.items() if k != "_id"})
    logger.info("Loaded: %d users, %d banned, %d profiles", len(_allowed_users), len(_banned_users), len(_profiles))


# ════════════════════════════════════════════════════════════════
#  ALLOWED / BANNED USERS
# ════════════════════════════════════════════════════════════════
def get_allowed_users() -> dict:
    return _allowed_users

def get_banned_users() -> set:
    return _banned_users

async def add_allowed_user(uid: int, username: str, limit: Optional[int], added_by: int):
    _allowed_users[uid] = {"username": username, "limit": limit, "added_by": added_by}
    _banned_users.discard(uid)
    if _mongo_enabled:
        await _db.allowed_users.update_one(
            {"_id": uid},
            {"$set": {"username": username, "limit": limit, "added_by": added_by}},
            upsert=True,
        )
        await _db.banned_users.delete_one({"_id": uid})

async def remove_allowed_user(uid: int):
    _allowed_users.pop(uid, None)
    if _mongo_enabled:
        await _db.allowed_users.delete_one({"_id": uid})

async def ban_user(uid: int):
    _banned_users.add(uid)
    _allowed_users.pop(uid, None)
    if _mongo_enabled:
        await _db.banned_users.update_one({"_id": uid}, {"$set": {"auto": False}}, upsert=True)
        await _db.allowed_users.delete_one({"_id": uid})

async def auto_ban_user(uid: int, reason: str = "spam"):
    _banned_users.add(uid)
    _allowed_users.pop(uid, None)
    logger.warning("Auto-banned uid=%s reason=%s", uid, reason)
    if _mongo_enabled:
        await _db.banned_users.update_one(
            {"_id": uid},
            {"$set": {"auto": True, "reason": reason, "at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        await _db.allowed_users.delete_one({"_id": uid})

async def unban_user(uid: int):
    _banned_users.discard(uid)
    if _mongo_enabled:
        await _db.banned_users.delete_one({"_id": uid})

async def set_user_limit(uid: int, limit: Optional[int]):
    if uid not in _allowed_users:
        _allowed_users[uid] = {"username": "", "limit": None, "added_by": 0}
    _allowed_users[uid]["limit"] = limit
    if _mongo_enabled:
        await _db.allowed_users.update_one({"_id": uid}, {"$set": {"limit": limit}}, upsert=True)


# ════════════════════════════════════════════════════════════════
#  SPAM DETECTION
# ════════════════════════════════════════════════════════════════
def check_spam(uid: int) -> bool:
    """Returns True if user is spamming (should be auto-banned)."""
    now = datetime.now(timezone.utc).timestamp()
    if uid not in _spam_track:
        _spam_track[uid] = deque()
    dq = _spam_track[uid]
    dq.append(now)
    # Remove timestamps outside window
    while dq and now - dq[0] > config.SPAM_WINDOW_SECS:
        dq.popleft()
    return len(dq) >= config.SPAM_MSG_COUNT


# ════════════════════════════════════════════════════════════════
#  USAGE & ACTIVITY
# ════════════════════════════════════════════════════════════════
def record_usage(uid: int):
    today = date.today()
    rec   = _usage_today.get(uid, {})
    if rec.get("date") != today:
        _usage_today[uid] = {"date": today, "count": 1}
    else:
        _usage_today[uid]["count"] = rec["count"] + 1
    _msg_total[uid] = _msg_total.get(uid, 0) + 1

async def persist_usage(uid: int):
    if _mongo_enabled:
        await _db.msg_totals.update_one(
            {"_id": uid}, {"$set": {"count": _msg_total.get(uid, 0)}}, upsert=True
        )

def get_usage_today(uid: int) -> int:
    rec = _usage_today.get(uid, {})
    return rec.get("count", 0) if rec.get("date") == date.today() else 0

def get_total_messages(uid: int) -> int:
    return _msg_total.get(uid, 0)

def update_activity(uid: int, username: str = "", first_name: str = ""):
    _activity[uid] = {
        "last_seen":  datetime.now(timezone.utc),
        "username":   username,
        "first_name": first_name,
    }

def get_activity(uid: int) -> dict:
    return _activity.get(uid, {})

def get_all_activity() -> dict:
    return _activity

def get_global_stats() -> dict:
    today        = date.today()
    msgs_today   = sum(v.get("count", 0) for v in _usage_today.values() if v.get("date") == today)
    total_msgs   = sum(_msg_total.values())
    active_today = sum(1 for v in _activity.values()
                       if v.get("last_seen") and v["last_seen"].date() == today)
    return {
        "total_users":   len(_allowed_users),
        "banned":        len(_banned_users),
        "msgs_today":    msgs_today,
        "total_msgs":    total_msgs,
        "active_today":  active_today,
        "mongo_enabled": _mongo_enabled,
    }


# ════════════════════════════════════════════════════════════════
#  PROFILES
# ════════════════════════════════════════════════════════════════
def get_profile(uid: int) -> dict:
    return _profiles.get(uid, {})

async def save_profile(uid: int, data: dict):
    _profiles[uid] = data
    if _mongo_enabled:
        await _db.profiles.update_one({"_id": uid}, {"$set": data}, upsert=True)

def search_user(query: str) -> list[tuple[int, dict]]:
    """Search users by username, name, or id. Returns [(uid, info), ...]"""
    q = query.lower().lstrip("@")
    results = []
    for uid, info in _allowed_users.items():
        uname = info.get("username", "").lower()
        prof  = _profiles.get(uid, {})
        name  = prof.get("name", "").lower()
        if q in uname or q in name or q == str(uid):
            results.append((uid, info))
    return results[:10]


# ════════════════════════════════════════════════════════════════
#  LEADERBOARD
# ════════════════════════════════════════════════════════════════
def get_leaderboard(top_n: int = 10) -> list[tuple[int, int]]:
    """Returns [(uid, total_msgs), ...] sorted by total_msgs desc."""
    return sorted(_msg_total.items(), key=lambda x: x[1], reverse=True)[:top_n]


# ════════════════════════════════════════════════════════════════
#  BADGES
# ════════════════════════════════════════════════════════════════
BADGE_DEFINITIONS = {
    "first_chat":   ("🥇", "First Chat",      "Pehla message bheja!"),
    "msgs_10":      ("💬", "Chatterbox",       "10 messages bheje"),
    "msgs_50":      ("🗨️",  "Talkative",       "50 messages bheje"),
    "msgs_100":     ("📢", "Century Club",     "100 messages bheje"),
    "msgs_500":     ("🏆", "Veteran",          "500 messages bheje"),
    "char_met":     ("💞", "First Connection", "Pehli baar character se mila"),
    "friend":       ("😊", "Friend",           "Relationship level: Friends"),
    "best_friend":  ("💕", "Best Friend",      "Relationship level: Best Friends"),
    "soulmate":     ("💞", "Soulmate",         "Soulmate status achieved!"),
    "gamer":        ("🎮", "Gamer",            "Pehla game khela"),
    "story_teller": ("📖", "Story Teller",     "Story mode use kiya"),
}

def get_badges(uid: int) -> set:
    return _badges.get(uid, set())

async def award_badge(uid: int, badge_key: str) -> bool:
    """Returns True if badge was newly awarded."""
    if uid not in _badges:
        _badges[uid] = set()
    if badge_key in _badges[uid]:
        return False
    _badges[uid].add(badge_key)
    if _mongo_enabled:
        await _db.badges.update_one(
            {"_id": uid}, {"$addToSet": {"list": badge_key}}, upsert=True
        )
    return True

async def check_and_award_badges(uid: int) -> list[str]:
    """Check all badge conditions and award new ones. Returns list of new badge keys."""
    total  = get_total_messages(uid)
    rels   = _relationships.get(uid, {})
    new_bs = []

    checks = [
        ("first_chat",  total >= 1),
        ("msgs_10",     total >= 10),
        ("msgs_50",     total >= 50),
        ("msgs_100",    total >= 100),
        ("msgs_500",    total >= 500),
        ("char_met",    bool(rels)),
        ("friend",      any(v >= 30 for v in rels.values())),
        ("best_friend", any(v >= 60 for v in rels.values())),
        ("soulmate",    any(v >= 100 for v in rels.values())),
    ]
    for key, condition in checks:
        if condition and await award_badge(uid, key):
            new_bs.append(key)
    return new_bs


# ════════════════════════════════════════════════════════════════
#  RELATIONSHIPS
# ════════════════════════════════════════════════════════════════
def get_relationship(uid: int, char_id: str) -> int:
    return _relationships.get(uid, {}).get(char_id, 0)

def get_relationship_label(pts: int) -> str:
    if pts < 5:    return "👋 Strangers"
    if pts < 15:   return "🙂 Acquaintances"
    if pts < 30:   return "😊 Friends"
    if pts < 60:   return "🥰 Close Friends"
    if pts < 100:  return "💕 Best Friends"
    return "💞 Soulmates"

async def add_relationship_point(uid: int, char_id: str, pts: int = 1):
    if uid not in _relationships:
        _relationships[uid] = {}
    _relationships[uid][char_id] = _relationships[uid].get(char_id, 0) + pts
    if _mongo_enabled:
        await _db.relationships.update_one(
            {"_id": uid},
            {"$set": {f"chars.{char_id}": _relationships[uid][char_id]}},
            upsert=True,
        )


# ════════════════════════════════════════════════════════════════
#  CHARACTER MOOD
# ════════════════════════════════════════════════════════════════
MOODS = ["😊 Happy", "😴 Tired", "🔥 Excited", "🥺 Clingy", "😏 Playful", "🌧️ Melancholy"]

def get_char_mood(char_id: str) -> str:
    import random
    entry = _char_mood.get(char_id)
    if not entry:
        mood = random.choice(MOODS)
        _char_mood[char_id] = {"mood": mood, "updated": date.today()}
        return mood
    # Refresh mood daily
    if entry.get("updated") != date.today():
        mood = random.choice(MOODS)
        _char_mood[char_id] = {"mood": mood, "updated": date.today()}
        return mood
    return entry["mood"]


# ════════════════════════════════════════════════════════════════
#  SCHEDULED BROADCASTS
# ════════════════════════════════════════════════════════════════
def get_schedules() -> list:
    return _schedules

async def add_schedule(schedule_data: dict):
    _schedules.append(schedule_data)
    if _mongo_enabled:
        await _db.schedules.update_one(
            {"id": schedule_data["id"]}, {"$set": schedule_data}, upsert=True
        )

async def remove_schedule(schedule_id: str):
    global _schedules
    _schedules = [s for s in _schedules if s.get("id") != schedule_id]
    if _mongo_enabled:
        await _db.schedules.delete_one({"id": schedule_id})

"""
database.py — MongoDB async driver (motor).
Falls back to in-memory dict if MONGODB_URI is not set.
"""

import logging
from datetime import date, datetime
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ─── Runtime state — always in-memory (fast access) ─────────────
# Persisted to Mongo on write; loaded from Mongo on startup.
_allowed_users: dict  = {}   # {user_id: {username, limit, added_by}}
_banned_users:  set   = set()
_user_profiles: dict  = {}   # {user_id: {name, bio, ...}}
_usage_today:   dict  = {}   # {user_id: {date, count}}
_msg_total:     dict  = {}   # {user_id: int}   lifetime messages
_char_relation: dict  = {}   # {user_id: {char_id: int}}  relationship points

_mongo_client   = None
_db             = None
_mongo_enabled  = False


async def init_db():
    """Call once at bot startup."""
    global _mongo_client, _db, _mongo_enabled
    global _allowed_users, _banned_users, _user_profiles
    global _usage_today, _msg_total, _char_relation

    if not config.MONGODB_URI:
        logger.warning("MONGODB_URI not set — running in-memory only (data lost on redeploy)")
        return

    try:
        import motor.motor_asyncio as motor
        _mongo_client  = motor.AsyncIOMotorClient(config.MONGODB_URI, serverSelectionTimeoutMS=5000)
        _db            = _mongo_client["telegram_ai_bot"]
        # Ping to verify connection
        await _mongo_client.admin.command("ping")
        _mongo_enabled = True
        logger.info("✅ MongoDB connected")

        # Load persisted data into memory
        await _load_from_mongo()

    except Exception as e:
        logger.error("MongoDB connection failed: %s — running in-memory", e)
        _mongo_enabled = False


async def _load_from_mongo():
    # Allowed users
    async for doc in _db.allowed_users.find():
        uid = doc["_id"]
        _allowed_users[uid] = {
            "username": doc.get("username", ""),
            "limit":    doc.get("limit"),
            "added_by": doc.get("added_by"),
        }

    # Banned users
    async for doc in _db.banned_users.find():
        _banned_users.add(doc["_id"])

    # Profiles
    async for doc in _db.profiles.find():
        uid = doc["_id"]
        _user_profiles[uid] = {k: v for k, v in doc.items() if k != "_id"}

    # Lifetime message counts
    async for doc in _db.msg_totals.find():
        _msg_total[doc["_id"]] = doc.get("count", 0)

    # Relationship levels
    async for doc in _db.relationships.find():
        _char_relation[doc["_id"]] = doc.get("chars", {})

    logger.info("✅ Loaded from MongoDB — %d users, %d banned, %d profiles",
                len(_allowed_users), len(_banned_users), len(_user_profiles))


# ─────────────────────────────────────────────────────────────────
#  PUBLIC API — all sync getters, async writers
# ─────────────────────────────────────────────────────────────────

# ── Allowed users ────────────────────────────────────────────────
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
        await _db.banned_users.update_one({"_id": uid}, {"$set": {}}, upsert=True)
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


# ── Usage tracking ───────────────────────────────────────────────
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
            {"_id": uid},
            {"$set": {"count": _msg_total.get(uid, 0)}},
            upsert=True,
        )

def get_usage_today(uid: int) -> int:
    rec = _usage_today.get(uid, {})
    return rec.get("count", 0) if rec.get("date") == date.today() else 0

def get_total_messages(uid: int) -> int:
    return _msg_total.get(uid, 0)

def get_global_stats() -> dict:
    total_users    = len(_allowed_users)
    banned_count   = len(_banned_users)
    msgs_today     = sum(
        v.get("count", 0) for v in _usage_today.values()
        if v.get("date") == date.today()
    )
    total_msgs_all = sum(_msg_total.values())
    return {
        "total_users":   total_users,
        "banned":        banned_count,
        "msgs_today":    msgs_today,
        "total_msgs":    total_msgs_all,
        "mongo_enabled": _mongo_enabled,
    }


# ── User profiles ────────────────────────────────────────────────
def get_profile(uid: int) -> dict:
    return _user_profiles.get(uid, {})

async def save_profile(uid: int, data: dict):
    _user_profiles[uid] = data
    if _mongo_enabled:
        await _db.profiles.update_one({"_id": uid}, {"$set": data}, upsert=True)


# ── Relationship levels ──────────────────────────────────────────
def get_relationship(uid: int, char_id: str) -> int:
    return _char_relation.get(uid, {}).get(char_id, 0)

def get_relationship_label(points: int) -> str:
    if points < 5:   return "👋 Strangers"
    if points < 15:  return "🙂 Acquaintances"
    if points < 30:  return "😊 Friends"
    if points < 60:  return "🥰 Close Friends"
    if points < 100: return "💕 Best Friends"
    return "💞 Soulmates"

async def add_relationship_point(uid: int, char_id: str, points: int = 1):
    if uid not in _char_relation:
        _char_relation[uid] = {}
    _char_relation[uid][char_id] = _char_relation[uid].get(char_id, 0) + points
    if _mongo_enabled:
        await _db.relationships.update_one(
            {"_id": uid},
            {"$set": {f"chars.{char_id}": _char_relation[uid][char_id]}},
            upsert=True,
        )

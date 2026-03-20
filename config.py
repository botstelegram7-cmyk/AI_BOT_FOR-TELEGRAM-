import os
import urllib.parse

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = (
    os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL", "")
).rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

# ── Owners ────────────────────────────────────────────────────
OWNER_IDS: set[int]       = {1598576202, 6518065496}
OWNER_USERNAMES: set[str] = {"xioqui_xin", "technicalserena"}
OWNER_CONTACTS            = [
    ("👑 @TechnicalSerena", "https://t.me/TechnicalSerena"),
    ("👑 @Xioqui_xin",      "https://t.me/Xioqui_xin"),
]

# ── Force Subscribe (optional) ─────────────────────────────────
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "serenaunzipbot")
FORCE_SUB_URL     = f"https://t.me/{FORCE_SUB_CHANNEL}" if FORCE_SUB_CHANNEL else ""

# ── Open Access ───────────────────────────────────────────────
FREE_DAILY_LIMIT = int(os.environ.get("FREE_DAILY_LIMIT", 10))

# ── AI Keys ───────────────────────────────────────────────────
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY       = os.environ.get("GROK_API_KEY", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
SAMBANOVA_API_KEY  = os.environ.get("SAMBANOVA_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
NVIDIA_API_KEY     = os.environ.get("NVIDIA_API_KEY", "")

# ── MongoDB — auto-encodes special chars in password ──────────
_raw_mongo = os.environ.get("MONGODB_URI", "")

def _fix_mongo_uri(uri: str) -> str:
    if not uri or "://" not in uri:
        return uri
    try:
        scheme_end = uri.index("://") + 3
        scheme     = uri[:scheme_end]
        rest       = uri[scheme_end:]
        at_pos     = rest.rfind("@")
        if at_pos == -1:
            return uri
        userinfo   = rest[:at_pos]
        hostpart   = rest[at_pos + 1:]
        if ":" in userinfo:
            user, pwd = userinfo.split(":", 1)
            pwd_enc   = urllib.parse.quote_plus(pwd)
            return f"{scheme}{user}:{pwd_enc}@{hostpart}"
    except Exception:
        pass
    return uri

MONGODB_URI = _fix_mongo_uri(_raw_mongo)

# ── Media ─────────────────────────────────────────────────────
WELCOME_PIC   = os.environ.get("WELCOME_PIC", "")
WELCOME_VIDEO = os.environ.get("WELCOME_VIDEO", "")
CHAR_1_PIC    = os.environ.get("CHAR_1_PIC", "")
CHAR_2_PIC    = os.environ.get("CHAR_2_PIC", "")
CHAR_3_PIC    = os.environ.get("CHAR_3_PIC", "")
CHAR_4_PIC    = os.environ.get("CHAR_4_PIC", "")
CHAR_5_PIC    = os.environ.get("CHAR_5_PIC", "")

# ── OpenRouter ────────────────────────────────────────────────
OPENROUTER_SITE_URL  = os.environ.get("OPENROUTER_SITE_URL",  "https://t.me")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "TelegramAIBot")

# ── Bot settings ──────────────────────────────────────────────
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "groq")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL",    "llama-3.3-70b-versatile")
MAX_HISTORY      = int(os.environ.get("MAX_HISTORY", 20))

# ── Anti-spam ─────────────────────────────────────────────────
SPAM_MSG_COUNT   = int(os.environ.get("SPAM_MSG_COUNT",  8))
SPAM_WINDOW_SECS = int(os.environ.get("SPAM_WINDOW_SECS", 10))

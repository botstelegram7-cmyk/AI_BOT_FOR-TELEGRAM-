import os

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

WEBHOOK_URL = (
    os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("WEBHOOK_URL", "")
).rstrip("/")

PORT = int(os.environ.get("PORT", 10000))

# ─────────────────────────────────────────────
#  OWNER / ACCESS CONTROL
# ─────────────────────────────────────────────
OWNER_IDS: set[int] = {1598576202, 6518065496}
OWNER_USERNAMES: set[str] = {"xioqui_xin", "technicalserena"}

# ─────────────────────────────────────────────
#  AI API KEYS
# ─────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY      = os.environ.get("GROK_API_KEY", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
SAMBANOVA_API_KEY = os.environ.get("SAMBANOVA_API_KEY", "")

# ─────────────────────────────────────────────
#  WELCOME MEDIA  (shown on /start)
#  Set WELCOME_PIC or WELCOME_VIDEO in Render env
# ─────────────────────────────────────────────
WELCOME_PIC   = os.environ.get("WELCOME_PIC", "")
WELCOME_VIDEO = os.environ.get("WELCOME_VIDEO", "")

# ─────────────────────────────────────────────
#  CHARACTER PHOTOS  (Tinder card images)
#  Set CHAR_1_PIC, CHAR_2_PIC ... CHAR_5_PIC
#  Value = direct image URL or Telegram file_id
# ─────────────────────────────────────────────
CHAR_1_PIC = os.environ.get("CHAR_1_PIC", "")
CHAR_2_PIC = os.environ.get("CHAR_2_PIC", "")
CHAR_3_PIC = os.environ.get("CHAR_3_PIC", "")
CHAR_4_PIC = os.environ.get("CHAR_4_PIC", "")
CHAR_5_PIC = os.environ.get("CHAR_5_PIC", "")

# ─────────────────────────────────────────────
#  OPTIONAL SETTINGS
# ─────────────────────────────────────────────
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "groq")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL", "llama-3.3-70b-versatile")
MAX_HISTORY      = int(os.environ.get("MAX_HISTORY", 20))

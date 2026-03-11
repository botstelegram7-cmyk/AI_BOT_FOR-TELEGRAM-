import os

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Render automatically injects RENDER_EXTERNAL_URL → no manual entry needed!
WEBHOOK_URL = (
    os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("WEBHOOK_URL", "")
).rstrip("/")

PORT = int(os.environ.get("PORT", 10000))

# ─────────────────────────────────────────────
#  OWNER / ACCESS CONTROL
# ─────────────────────────────────────────────
# Hardcoded owner Telegram user IDs — full admin powers, always allowed
OWNER_IDS: set[int] = {1598576202, 6518065496}
OWNER_USERNAMES: set[str] = {"xioqui_xin", "technicalserena"}   # lowercase

# ─────────────────────────────────────────────
#  AI API KEYS  (add in Render → Environment)
# ─────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY      = os.environ.get("GROK_API_KEY", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
SAMBANOVA_API_KEY = os.environ.get("SAMBANOVA_API_KEY", "")

# ─────────────────────────────────────────────
#  OPTIONAL SETTINGS
# ─────────────────────────────────────────────
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "groq")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL", "llama-3.3-70b-versatile")
MAX_HISTORY      = int(os.environ.get("MAX_HISTORY", 20))

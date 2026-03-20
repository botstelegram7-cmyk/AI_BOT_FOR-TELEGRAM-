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
OWNER_IDS: set[int]      = {1598576202, 6518065496}
OWNER_USERNAMES: set[str] = {"xioqui_xin", "technicalserena"}

# ─────────────────────────────────────────────
#  AI API KEYS
# ─────────────────────────────────────────────
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY        = os.environ.get("GROK_API_KEY", "")         # xAI / Grok
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")       # Google Gemini
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")         # Groq (fast LLMs)
SAMBANOVA_API_KEY   = os.environ.get("SAMBANOVA_API_KEY", "")    # SambaNova
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")   # OpenRouter
NVIDIA_API_KEY      = os.environ.get("NVIDIA_API_KEY", "")       # NVIDIA NIM

# ─────────────────────────────────────────────
#  MONGODB
# ─────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")   # e.g. mongodb+srv://...

# ─────────────────────────────────────────────
#  WELCOME MEDIA  (/start pe dikhega)
# ─────────────────────────────────────────────
WELCOME_PIC   = os.environ.get("WELCOME_PIC", "")    # image URL or file_id
WELCOME_VIDEO = os.environ.get("WELCOME_VIDEO", "")  # video URL or file_id

# ─────────────────────────────────────────────
#  CHARACTER PHOTOS  (Tinder cards)
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

# Site info sent to OpenRouter (optional, for rankings)
OPENROUTER_SITE_URL  = os.environ.get("OPENROUTER_SITE_URL", "https://t.me")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "TelegramAIBot")

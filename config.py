import os

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Render automatically injects RENDER_EXTERNAL_URL → no manual entry needed!
# Fallback: set WEBHOOK_URL manually if running on another host.
WEBHOOK_URL = (
    os.environ.get("RENDER_EXTERNAL_URL")   # ← Render auto-injects this ✅
    or os.environ.get("WEBHOOK_URL", "")    # ← manual fallback
).rstrip("/")

# Render injects PORT automatically; default 10000
PORT = int(os.environ.get("PORT", 10000))

# ─────────────────────────────────────────────
#  AI API KEYS  (add these in Render → Environment)
# ─────────────────────────────────────────────
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY        = os.environ.get("GROK_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
SAMBANOVA_API_KEY   = os.environ.get("SAMBANOVA_API_KEY", "")

# ─────────────────────────────────────────────
#  OPTIONAL SETTINGS
# ─────────────────────────────────────────────
# Default model used on first message
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "groq")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL", "llama-3.3-70b-versatile")

# Max messages kept in per-user chat history
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 20))

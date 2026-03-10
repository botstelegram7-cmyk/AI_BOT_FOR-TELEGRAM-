import os

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Automatically use Render's public URL if available
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") or RENDER_EXTERNAL_URL

# Port (Render injects this)
PORT = int(os.environ.get("PORT", 10000))

# API keys
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GROK_API_KEY        = os.environ.get("GROK_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
SAMBANOVA_API_KEY   = os.environ.get("SAMBANOVA_API_KEY", "")

# Optional settings
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "groq")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL", "llama-3.3-70b-versatile")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 20))

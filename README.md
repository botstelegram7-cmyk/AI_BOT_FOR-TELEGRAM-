# 🤖 Telegram AI Multi-Model Bot

Telegram bot jo multiple AI providers ko support karta hai — deploy karo Render par ek click mein.

---

## ✨ Features
- **6 AI Providers** — OpenAI, Anthropic Claude, Grok (xAI), Google Gemini, Groq, SambaNova
- **2-step model picker** via inline keyboards
- **Per-user chat history** (configurable length)
- **Render-ready webhook** — Flask nahi chahiye, koi "No ports detected" error nahi

---

## 📁 Files
```
telegram-ai-bot/
├── config.py        ← all env var reads (fill on Render)
├── ai_client.py     ← unified AI provider abstraction
├── bot.py           ← bot logic + webhook server
├── requirements.txt
└── README.md
```

---

## 🚀 Render Deployment Steps

### 1. GitHub pe push karo
```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 2. Render → New Web Service
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python bot.py`
- **Runtime:** Python 3.11

### 3. Environment Variables set karo (Render → Environment tab)

| Variable | Value | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather se mila token | ✅ |
| `WEBHOOK_URL` | `https://your-app.onrender.com` | ✅ |
| `PORT` | (Render auto-injects, chhod do) | auto |
| `OPENAI_API_KEY` | OpenAI key | optional |
| `ANTHROPIC_API_KEY` | Anthropic key | optional |
| `GROK_API_KEY` | xAI Grok key | optional |
| `GEMINI_API_KEY` | Google AI Studio key | optional |
| `GROQ_API_KEY` | Groq key | optional |
| `SAMBANOVA_API_KEY` | SambaNova key | optional |
| `DEFAULT_PROVIDER` | e.g. `groq` | optional |
| `DEFAULT_MODEL` | e.g. `llama-3.3-70b-versatile` | optional |

> **Note:** Sirf woh providers active honge jinke API keys set hain.

### 4. Bot ka webhook manually set karne ki zaroorat NAHI hai
`bot.py` khud `/webhook` endpoint register kar leta hai jab `WEBHOOK_URL` set ho.

---

## 💻 Local Development (polling mode)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token"
# WEBHOOK_URL set mat karo — auto polling mode mein chala jaata hai
python bot.py
```

---

## 🗨️ Bot Commands
| Command | Description |
|---|---|
| `/start` | Welcome message + active providers |
| `/model` | 2-step AI model picker |
| `/status` | Current provider + model |
| `/clear` | Chat history saaf karo |
| `/help` | Help menu |

---

## 🔧 Troubleshooting

**"No ports detected" on Render**
→ `WEBHOOK_URL` env var set karo. Bot aiohttp server port par sunne lagega.

**Provider nahi dikh raha /model mein**
→ Us provider ki API key Render environment mein add karo.

**Webhook kaam nahi kar raha**
→ `WEBHOOK_URL` mein trailing slash mat rakho. Format: `https://my-app.onrender.com`

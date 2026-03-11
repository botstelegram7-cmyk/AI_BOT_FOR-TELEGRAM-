"""
Unified AI client — supports OpenAI, Anthropic, Grok, Gemini, Groq, SambaNova.
All providers are accessed via their respective Python SDKs or the OpenAI-compatible API.
"""

import asyncio
import logging
import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  MODEL REGISTRY
#  Structure: { provider_key: { "name": str, "models": {id: label} } }
# ─────────────────────────────────────────────────────────────────
AVAILABLE_MODELS = {
    "openai": {
        "name": "🤖 OpenAI",
        "env_key": "OPENAI_API_KEY",
        "models": {
            "gpt-4o":            "GPT-4o",
            "gpt-4o-mini":       "GPT-4o Mini ⚡",
            "gpt-4-turbo":       "GPT-4 Turbo",
            "gpt-3.5-turbo":     "GPT-3.5 Turbo",
        },
    },
    "anthropic": {
        "name": "🧠 Claude (Anthropic)",
        "env_key": "ANTHROPIC_API_KEY",
        "models": {
            "claude-opus-4-5":             "Claude Opus 4.5",
            "claude-sonnet-4-5":           "Claude Sonnet 4.5 ⚡",
            "claude-haiku-4-5-20251001":   "Claude Haiku 4.5",
        },
    },
    "grok": {
        "name": "🌟 Grok (xAI)",
        "env_key": "GROK_API_KEY",
        "models": {
            "grok-4-latest":   "Grok 4 (Latest)",
            "grok-3-latest":   "Grok 3 (Latest)",
            "grok-2-latest":   "Grok 2 (Latest)",
        },
    },
    "gemini": {
        "name": "💎 Google Gemini",
        "env_key": "GEMINI_API_KEY",
        "models": {
            "gemini-2.0-flash":   "Gemini 2.0 Flash ⚡",
            "gemini-1.5-pro":     "Gemini 1.5 Pro",
            "gemini-1.5-flash":   "Gemini 1.5 Flash",
        },
    },
    "groq": {
        "name": "⚡ Groq (Fast LLMs)",
        "env_key": "GROQ_API_KEY",
        "models": {
            "llama-3.3-70b-versatile":     "Llama 3.3 70B",
            "llama-3.1-8b-instant":        "Llama 3.1 8B Instant ⚡",
            "llama3-70b-8192":             "Llama 3 70B",
            "mixtral-8x7b-32768":          "Mixtral 8x7B",
            "openai/gpt-oss-120b":         "GPT-OSS 120B (Groq) 🔥",
            "deepseek-r1-distill-llama-70b": "DeepSeek R1 70B",
        },
    },
    "sambanova": {
        "name": "🚀 SambaNova",
        "env_key": "SAMBANOVA_API_KEY",
        "models": {
            "Meta-Llama-3.3-70B-Instruct":   "Llama 3.3 70B",
            "Meta-Llama-3.1-405B-Instruct":  "Llama 3.1 405B 🔥",
            "ALLaM-7B-Instruct-preview":     "ALLaM 7B",
        },
    },
}


def get_active_providers() -> list:
    """Return providers that have an API key configured."""
    active = []
    key_map = {
        "openai":    config.OPENAI_API_KEY,
        "anthropic": config.ANTHROPIC_API_KEY,
        "grok":      config.GROK_API_KEY,
        "gemini":    config.GEMINI_API_KEY,
        "groq":      config.GROQ_API_KEY,
        "sambanova": config.SAMBANOVA_API_KEY,
    }
    for provider, key in key_map.items():
        if key.strip():
            active.append(provider)
    return active


def get_model_label(provider: str, model_id: str) -> str:
    return AVAILABLE_MODELS.get(provider, {}).get("models", {}).get(model_id, model_id)


# ─────────────────────────────────────────────────────────────────
#  ASYNC WRAPPER  (runs sync SDK calls in thread pool)
# ─────────────────────────────────────────────────────────────────
async def get_ai_response(provider: str, model: str, messages: list) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _dispatch, provider, model, messages
    )


def _dispatch(provider: str, model: str, messages: list) -> str:
    handlers = {
        "openai":    _openai,
        "anthropic": _anthropic,
        "grok":      _grok,
        "gemini":    _gemini,
        "groq":      _groq,
        "sambanova": _sambanova,
    }
    fn = handlers.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")
    return fn(model, messages)


# ─────────────────────────────────────────────────────────────────
#  OPENAI-COMPATIBLE HELPER  (used by OpenAI, Grok, Groq, SambaNova)
# ─────────────────────────────────────────────────────────────────
def _openai_compat(base_url: str, api_key: str, model: str, messages: list) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
    )
    return resp.choices[0].message.content


# ─────────────────────────────────────────────────────────────────
#  PROVIDER IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────
def _openai(model: str, messages: list) -> str:
    return _openai_compat(
        "https://api.openai.com/v1",
        config.OPENAI_API_KEY,
        model, messages,
    )


def _grok(model: str, messages: list) -> str:
    return _openai_compat(
        "https://api.x.ai/v1",
        config.GROK_API_KEY,
        model, messages,
    )


def _groq(model: str, messages: list) -> str:
    # Official Groq SDK with streaming (as per Groq playground)
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=1,
        max_completion_tokens=8192,
        top_p=1,
        stream=True,
        stop=None,
    )
    # Collect all streamed chunks into one string
    result = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            result += delta
    return result


def _sambanova(model: str, messages: list) -> str:
    # SambaNova exposes an OpenAI-compatible endpoint
    return _openai_compat(
        "https://api.sambanova.ai/v1",
        config.SAMBANOVA_API_KEY,
        model, messages,
    )


def _anthropic(model: str, messages: list) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Split system message from chat messages
    system_text = "You are a helpful assistant."
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            chat_msgs.append(m)

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system_text,
        messages=chat_msgs,
    )
    return resp.content[0].text


def _gemini(model: str, messages: list) -> str:
    import google.generativeai as genai
    genai.configure(api_key=config.GEMINI_API_KEY)

    gemini_model = genai.GenerativeModel(model)

    # Convert OpenAI-style messages → Gemini chat history
    history = []
    last_user_msg = None

    for m in messages:
        role = m["role"]
        content = m["content"]
        if role == "system":
            continue  # Gemini doesn't have explicit system turns
        elif role == "user":
            last_user_msg = content
            if history:  # only add to history if there's prior context
                history.append({"role": "user", "parts": [content]})
        elif role == "assistant":
            history.append({"role": "model", "parts": [content]})

    if not history:
        # Single-turn call
        resp = gemini_model.generate_content(last_user_msg or "Hello")
        return resp.text

    # Multi-turn: rebuild history without the last user message
    chat_history = history[:-1] if history[-1]["role"] == "user" else history
    chat = gemini_model.start_chat(history=chat_history)
    resp = chat.send_message(last_user_msg or "Hello")
    return resp.text

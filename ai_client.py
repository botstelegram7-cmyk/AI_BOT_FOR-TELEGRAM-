"""
ai_client.py — Unified AI provider abstraction.
Supports: OpenAI · Anthropic · Grok · Gemini · Groq · SambaNova · OpenRouter · NVIDIA
"""

import asyncio
import logging
import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  MODEL REGISTRY
# ─────────────────────────────────────────────────────────────────
AVAILABLE_MODELS: dict[str, dict] = {
    "openai": {
        "name": "🤖 OpenAI",
        "env_key": "OPENAI_API_KEY",
        "models": {
            "gpt-4o":        "GPT-4o 🔥",
            "gpt-4o-mini":   "GPT-4o Mini ⚡",
            "gpt-4-turbo":   "GPT-4 Turbo",
            "gpt-3.5-turbo": "GPT-3.5 Turbo",
        },
    },
    "anthropic": {
        "name": "🧠 Claude (Anthropic)",
        "env_key": "ANTHROPIC_API_KEY",
        "models": {
            "claude-opus-4-5":           "Claude Opus 4.5 🔥",
            "claude-sonnet-4-5":         "Claude Sonnet 4.5 ⚡",
            "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
        },
    },
    "grok": {
        "name": "🌟 Grok (xAI)",
        "env_key": "GROK_API_KEY",
        "models": {
            "grok-4-latest": "Grok 4 🔥",
            "grok-3-latest": "Grok 3",
            "grok-2-latest": "Grok 2",
        },
    },
    "gemini": {
        "name": "💎 Google Gemini",
        "env_key": "GEMINI_API_KEY",
        "models": {
            "gemini-2.5-flash-preview-05-20": "Gemini 2.5 Flash 🔥",
            "gemini-2.0-flash":               "Gemini 2.0 Flash ⚡",
            "gemini-1.5-pro":                 "Gemini 1.5 Pro",
        },
    },
    "groq": {
        "name": "⚡ Groq (Fast LLMs)",
        "env_key": "GROQ_API_KEY",
        "models": {
            "llama-3.3-70b-versatile":        "Llama 3.3 70B ⚡",
            "llama-3.1-8b-instant":           "Llama 3.1 8B Instant 🚀",
            "openai/gpt-oss-120b":            "GPT-OSS 120B 🔥",
            "deepseek-r1-distill-llama-70b":  "DeepSeek R1 70B",
            "mixtral-8x7b-32768":             "Mixtral 8x7B",
        },
    },
    "sambanova": {
        "name": "🚀 SambaNova",
        "env_key": "SAMBANOVA_API_KEY",
        "models": {
            "Meta-Llama-3.3-70B-Instruct":  "Llama 3.3 70B ⚡",
            "Meta-Llama-3.1-405B-Instruct": "Llama 3.1 405B 🔥",
            "ALLaM-7B-Instruct-preview":    "ALLaM 7B",
        },
    },
    "openrouter": {
        "name": "🔀 OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "models": {
            "z-ai/glm-4.5-air:free":              "GLM-4.5 Air (Free)",
            "google/gemini-2.0-flash-exp:free":   "Gemini 2.0 Flash (Free)",
            "meta-llama/llama-3.3-70b-instruct":  "Llama 3.3 70B",
            "deepseek/deepseek-chat-v3-0324:free": "DeepSeek V3 (Free)",
            "mistralai/mistral-7b-instruct:free":  "Mistral 7B (Free)",
        },
    },
    "nvidia": {
        "name": "🟢 NVIDIA NIM",
        "env_key": "NVIDIA_API_KEY",
        "models": {
            "meta/llama-4-maverick-17b-128e-instruct": "Llama 4 Maverick 17B 🔥",
            "meta/llama-3.3-70b-instruct":             "Llama 3.3 70B",
            "nvidia/llama-3.1-nemotron-70b-instruct":  "Nemotron 70B",
            "mistralai/mistral-large-2-instruct":      "Mistral Large 2",
        },
    },
}


def get_active_providers() -> list[str]:
    key_map = {
        "openai":      config.OPENAI_API_KEY,
        "anthropic":   config.ANTHROPIC_API_KEY,
        "grok":        config.GROK_API_KEY,
        "gemini":      config.GEMINI_API_KEY,
        "groq":        config.GROQ_API_KEY,
        "sambanova":   config.SAMBANOVA_API_KEY,
        "openrouter":  config.OPENROUTER_API_KEY,
        "nvidia":      config.NVIDIA_API_KEY,
    }
    return [p for p, k in key_map.items() if k.strip()]


def get_model_label(provider: str, model_id: str) -> str:
    return AVAILABLE_MODELS.get(provider, {}).get("models", {}).get(model_id, model_id)


# ─────────────────────────────────────────────────────────────────
#  ASYNC ENTRY POINT
# ─────────────────────────────────────────────────────────────────
async def get_ai_response(provider: str, model: str, messages: list) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _dispatch, provider, model, messages)


def _dispatch(provider: str, model: str, messages: list) -> str:
    handlers = {
        "openai":     _openai,
        "anthropic":  _anthropic,
        "grok":       _grok,
        "gemini":     _gemini,
        "groq":       _groq,
        "sambanova":  _sambanova,
        "openrouter": _openrouter,
        "nvidia":     _nvidia,
    }
    fn = handlers.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")
    return fn(model, messages)


# ─────────────────────────────────────────────────────────────────
#  OPENAI-COMPATIBLE HELPER
# ─────────────────────────────────────────────────────────────────
def _openai_compat(base_url: str, api_key: str, model: str,
                   messages: list, extra_headers: dict = None) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url,
                    default_headers=extra_headers or {})
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
    return _openai_compat("https://api.openai.com/v1", config.OPENAI_API_KEY, model, messages)


def _grok(model: str, messages: list) -> str:
    return _openai_compat("https://api.x.ai/v1", config.GROK_API_KEY, model, messages)


def _sambanova(model: str, messages: list) -> str:
    return _openai_compat("https://api.sambanova.ai/v1", config.SAMBANOVA_API_KEY, model, messages)


def _openrouter(model: str, messages: list) -> str:
    return _openai_compat(
        "https://openrouter.ai/api/v1",
        config.OPENROUTER_API_KEY,
        model, messages,
        extra_headers={
            "HTTP-Referer": config.OPENROUTER_SITE_URL,
            "X-Title":      config.OPENROUTER_SITE_NAME,
        },
    )


def _nvidia(model: str, messages: list) -> str:
    return _openai_compat(
        "https://integrate.api.nvidia.com/v1",
        config.NVIDIA_API_KEY,
        model, messages,
    )


def _groq(model: str, messages: list) -> str:
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=1,
        max_completion_tokens=4096,
        top_p=1,
        stream=True,
    )
    return "".join(
        chunk.choices[0].delta.content or ""
        for chunk in stream
    )


def _anthropic(model: str, messages: list) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    system_text = "You are a helpful assistant."
    chat_msgs   = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            chat_msgs.append(m)
    resp = client.messages.create(
        model=model, max_tokens=2048,
        system=system_text, messages=chat_msgs,
    )
    return resp.content[0].text


def _gemini(model: str, messages: list) -> str:
    from google import genai as google_genai
    client = google_genai.Client(api_key=config.GEMINI_API_KEY)

    # Build prompt from messages
    parts = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if role == "system":
            parts.append(f"[System Instructions]: {content}\n")
        elif role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")

    full_prompt = "\n".join(parts)
    resp = client.models.generate_content(model=model, contents=full_prompt)
    return resp.text

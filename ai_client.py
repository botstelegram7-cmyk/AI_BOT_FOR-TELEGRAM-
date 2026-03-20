"""
ai_client.py — Unified async streaming AI client.
All providers yield text chunks via async generators → ChatGPT-like typing in Telegram.

Fixes:
  • Gemini: removed unavailable preview models, uses stable IDs
  • OpenRouter: proper timeout + retry, updated free model list
  • NVIDIA: OpenAI-compat base URL
"""

import asyncio
import logging
from typing import AsyncGenerator

import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  MODEL REGISTRY
# ─────────────────────────────────────────────────────────────────
AVAILABLE_MODELS: dict[str, dict] = {
    "openai": {
        "name": "🤖 OpenAI",
        "models": {
            "gpt-4o":        "GPT-4o 🔥",
            "gpt-4o-mini":   "GPT-4o Mini ⚡",
            "gpt-4-turbo":   "GPT-4 Turbo",
            "gpt-3.5-turbo": "GPT-3.5 Turbo",
        },
    },
    "anthropic": {
        "name": "🧠 Claude (Anthropic)",
        "models": {
            "claude-opus-4-5":           "Claude Opus 4.5 🔥",
            "claude-sonnet-4-5":         "Claude Sonnet 4.5 ⚡",
            "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
        },
    },
    "grok": {
        "name": "🌟 Grok (xAI)",
        "models": {
            "grok-3-latest": "Grok 3 🔥",
            "grok-2-latest": "Grok 2",
        },
    },
    "gemini": {
        "name": "💎 Google Gemini",
        "models": {
            # Only stable, confirmed-working model IDs
            "gemini-2.0-flash":       "Gemini 2.0 Flash ⚡",
            "gemini-1.5-flash":       "Gemini 1.5 Flash",
            "gemini-1.5-pro":         "Gemini 1.5 Pro 🔥",
        },
    },
    "groq": {
        "name": "⚡ Groq",
        "models": {
            "llama-3.3-70b-versatile":       "Llama 3.3 70B ⚡",
            "llama-3.1-8b-instant":          "Llama 3.1 8B 🚀",
            "deepseek-r1-distill-llama-70b": "DeepSeek R1 70B",
            "mixtral-8x7b-32768":            "Mixtral 8x7B",
        },
    },
    "sambanova": {
        "name": "🚀 SambaNova",
        "models": {
            "Meta-Llama-3.3-70B-Instruct":  "Llama 3.3 70B ⚡",
            "Meta-Llama-3.1-405B-Instruct": "Llama 3.1 405B 🔥",
        },
    },
    "openrouter": {
        "name": "🔀 OpenRouter",
        "models": {
            # Reliably free models as of 2025
            "meta-llama/llama-3.3-70b-instruct:free":    "Llama 3.3 70B (Free)",
            "deepseek/deepseek-chat-v3-0324:free":        "DeepSeek V3 (Free)",
            "google/gemini-2.0-flash-exp:free":           "Gemini 2.0 Flash (Free)",
            "mistralai/mistral-7b-instruct:free":         "Mistral 7B (Free)",
            "microsoft/phi-3-mini-128k-instruct:free":    "Phi-3 Mini (Free)",
        },
    },
    "nvidia": {
        "name": "🟢 NVIDIA NIM",
        "models": {
            "meta/llama-3.3-70b-instruct":              "Llama 3.3 70B",
            "meta/llama-4-maverick-17b-128e-instruct":  "Llama 4 Maverick 🔥",
            "nvidia/llama-3.1-nemotron-70b-instruct":   "Nemotron 70B",
        },
    },
}


def get_active_providers() -> list[str]:
    key_map = {
        "openai":     config.OPENAI_API_KEY,
        "anthropic":  config.ANTHROPIC_API_KEY,
        "grok":       config.GROK_API_KEY,
        "gemini":     config.GEMINI_API_KEY,
        "groq":       config.GROQ_API_KEY,
        "sambanova":  config.SAMBANOVA_API_KEY,
        "openrouter": config.OPENROUTER_API_KEY,
        "nvidia":     config.NVIDIA_API_KEY,
    }
    return [p for p, k in key_map.items() if k.strip()]


def get_model_label(provider: str, model_id: str) -> str:
    return AVAILABLE_MODELS.get(provider, {}).get("models", {}).get(model_id, model_id)


# ─────────────────────────────────────────────────────────────────
#  ASYNC STREAMING ENTRY POINT
# ─────────────────────────────────────────────────────────────────
async def stream_ai_response(
    provider: str, model: str, messages: list
) -> AsyncGenerator[str, None]:
    """Yields text chunks. All providers supported."""
    try:
        if provider == "groq":
            async for chunk in _stream_groq(model, messages):
                yield chunk
        elif provider in ("openai", "grok", "sambanova", "nvidia"):
            base_urls = {
                "openai":    ("https://api.openai.com/v1",        config.OPENAI_API_KEY),
                "grok":      ("https://api.x.ai/v1",              config.GROK_API_KEY),
                "sambanova": ("https://api.sambanova.ai/v1",       config.SAMBANOVA_API_KEY),
                "nvidia":    ("https://integrate.api.nvidia.com/v1", config.NVIDIA_API_KEY),
            }
            url, key = base_urls[provider]
            async for chunk in _stream_openai_compat(url, key, model, messages):
                yield chunk
        elif provider == "openrouter":
            async for chunk in _stream_openrouter(model, messages):
                yield chunk
        elif provider == "anthropic":
            async for chunk in _stream_anthropic(model, messages):
                yield chunk
        elif provider == "gemini":
            # Gemini new SDK — run sync in executor, then simulate streaming
            loop = asyncio.get_event_loop()
            full = await loop.run_in_executor(None, _gemini_sync, model, messages)
            async for chunk in _simulate_stream(full):
                yield chunk
        else:
            raise ValueError(f"Unknown provider: {provider}")
    except Exception as e:
        logger.error("AI stream error [%s/%s]: %s", provider, model, e, exc_info=True)
        raise


# ─────────────────────────────────────────────────────────────────
#  SIMULATE STREAMING (for non-streaming providers)
# ─────────────────────────────────────────────────────────────────
async def _simulate_stream(text: str, chunk_size: int = 20) -> AsyncGenerator[str, None]:
    """Yield text in small chunks to simulate typing."""
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        await asyncio.sleep(0.03)


# ─────────────────────────────────────────────────────────────────
#  GROQ  (official async SDK)
# ─────────────────────────────────────────────────────────────────
async def _stream_groq(model: str, messages: list) -> AsyncGenerator[str, None]:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=config.GROQ_API_KEY)
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
        temperature=0.9,
        stream=True,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content
        if content:
            yield content


# ─────────────────────────────────────────────────────────────────
#  OPENAI-COMPATIBLE  (OpenAI, Grok, SambaNova, NVIDIA)
# ─────────────────────────────────────────────────────────────────
async def _stream_openai_compat(
    base_url: str, api_key: str, model: str, messages: list,
    extra_headers: dict = None
) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=extra_headers or {},
        timeout=30.0,
    )
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
        stream=True,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content
        if content:
            yield content


# ─────────────────────────────────────────────────────────────────
#  OPENROUTER  (OpenAI-compat with site headers + timeout)
# ─────────────────────────────────────────────────────────────────
async def _stream_openrouter(model: str, messages: list) -> AsyncGenerator[str, None]:
    headers = {
        "HTTP-Referer": config.OPENROUTER_SITE_URL,
        "X-Title":      config.OPENROUTER_SITE_NAME,
    }
    try:
        async for chunk in _stream_openai_compat(
            "https://openrouter.ai/api/v1",
            config.OPENROUTER_API_KEY,
            model, messages,
            extra_headers=headers,
        ):
            yield chunk
    except Exception as e:
        err_str = str(e).lower()
        if "connection" in err_str or "timeout" in err_str:
            raise ConnectionError(
                "OpenRouter se connect nahi ho paya. "
                "Network issue ya model unavailable. Dusra model try karo."
            ) from e
        raise


# ─────────────────────────────────────────────────────────────────
#  ANTHROPIC  (native streaming SDK)
# ─────────────────────────────────────────────────────────────────
async def _stream_anthropic(model: str, messages: list) -> AsyncGenerator[str, None]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    system_text = "You are a helpful assistant."
    chat_msgs   = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            chat_msgs.append(m)

    async with client.messages.stream(
        model=model,
        max_tokens=2048,
        system=system_text,
        messages=chat_msgs,
    ) as stream:
        async for text in stream.text_stream:
            yield text


# ─────────────────────────────────────────────────────────────────
#  GEMINI  (sync, run in executor)
# ─────────────────────────────────────────────────────────────────
def _gemini_sync(model: str, messages: list) -> str:
    from google import genai as gai
    client = gai.Client(api_key=config.GEMINI_API_KEY)

    # Build a single prompt string from message history
    parts = []
    for m in messages:
        role    = m["role"]
        content = m["content"]
        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "user":
            parts.append(f"Human: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")

    prompt = "\n".join(parts)
    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        return resp.text
    except Exception as e:
        err = str(e)
        if "404" in err or "not found" in err.lower():
            raise RuntimeError(
                f"Model `{model}` available nahi hai. "
                "/model se dusra Gemini model chunein."
            ) from e
        raise

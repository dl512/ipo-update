"""
OpenAI-compatible client helpers (standalone HKEX copy).

Loads ``hkex/.env`` via ``env_loader``. Prefer ``AI_GATEWAY_API_KEY`` or ``OPENAI_API_KEY`` + optional ``BASE_URL``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from openai import AsyncOpenAI

from env_loader import load_hkex_dotenv

load_hkex_dotenv()

logger = logging.getLogger(__name__)


def initialize_openai_client() -> AsyncOpenAI:
    gateway_api_key = os.getenv("AI_GATEWAY_API_KEY")
    openai_api_key = gateway_api_key or os.getenv("OPENAI_API_KEY")
    openai_base_url = (
        os.getenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1")
        if gateway_api_key
        else os.getenv("BASE_URL")
    )

    if not openai_api_key:
        raise ValueError(
            "Missing API key: set AI_GATEWAY_API_KEY (preferred) or OPENAI_API_KEY in hkex/.env"
        )

    return AsyncOpenAI(
        base_url=openai_base_url,
        api_key=openai_api_key,
    )


def get_llm_model_main() -> str:
    return os.getenv("LLM_MODEL_MAIN", "openai/gpt-4.1")


def get_llm_model_light() -> str:
    return os.getenv("LLM_MODEL_LIGHT", "openai/gpt-4.1-nano")


def get_llm_model_main_fallback() -> str:
    return os.getenv("LLM_MODEL_MAIN_FALLBACK", "meta-llama/llama-3.3-70b-instruct")


def extract_json_text_from_llm_response(content: str) -> str:
    s = (content or "").strip()
    if not s:
        return s
    lower = s.lower()
    if "```json" in lower:
        idx = lower.find("```json")
        start = idx + 7
        end = s.find("```", start)
        if end != -1:
            return s[start:end].strip()
    if "```" in s:
        start = s.find("```") + 3
        end = s.find("```", start)
        if end != -1:
            return s[start:end].strip()
    lb = s.find("{")
    rb = s.rfind("}")
    if lb != -1 and rb != -1 and rb > lb:
        return s[lb : rb + 1]
    return s


async def chat_completion_with_fallback(
    client: AsyncOpenAI,
    tier: Literal["main", "light"],
    **kwargs: Any,
) -> Any:
    kwargs = {k: v for k, v in kwargs.items() if k != "model"}
    if tier == "light":
        return await client.chat.completions.create(
            model=get_llm_model_light(),
            **kwargs,
        )
    primary = get_llm_model_main()
    fallback = get_llm_model_main_fallback()
    try:
        return await client.chat.completions.create(model=primary, **kwargs)
    except Exception as e:
        logger.warning(
            "chat.completions primary model %s failed (%s); retrying with %s",
            primary,
            e,
            fallback,
        )
        return await client.chat.completions.create(model=fallback, **kwargs)

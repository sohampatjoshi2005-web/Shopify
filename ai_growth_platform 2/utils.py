"""Shared helpers: safe parsing and a small, failure-tolerant LLM abstraction."""
import json
import logging
import os
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

PROVIDERS = ["Template / Local", "OpenAI", "Ollama"]
LLM_TIMEOUT_SECONDS = 60


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def llm_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def get_llm_client():
    """Return an OpenAI client only when an API key is configured."""
    if not llm_enabled():
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=LLM_TIMEOUT_SECONDS, max_retries=2)
    except Exception as exc:  # pragma: no cover - depends on installed package
        log.warning("Could not create OpenAI client: %s", exc)
        return None


def call_openai(client, system_prompt: str, user_prompt: str, model: Optional[str] = None) -> str:
    if client is None:
        raise RuntimeError("OpenAI client is not configured (set OPENAI_API_KEY).")
    response = client.chat.completions.create(
        model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def call_ollama(system_prompt: str, user_prompt: str) -> str:
    """Optional local LLM path. Requires Ollama running locally."""
    import requests

    url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "llama3.2"),
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def llm_generate(provider: str, client, system_prompt: str, user_prompt: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, error). text is None when the template fallback should be used.

    Errors are returned (and logged) instead of silently swallowed so the UI can
    tell the operator why a template was used.
    """
    if provider not in ("OpenAI", "Ollama"):
        return None, None
    try:
        if provider == "OpenAI":
            if client is None:
                return None, "OpenAI selected but OPENAI_API_KEY is not set; using templates."
            text = call_openai(client, system_prompt, user_prompt)
        else:
            text = call_ollama(system_prompt, user_prompt)
        return (text or None), (None if text else "The model returned an empty response; using templates.")
    except Exception as exc:
        log.warning("LLM call failed (%s): %s", provider, exc)
        return None, f"{provider} request failed ({type(exc).__name__}); using templates."


def maybe_llm(provider: str, client, system_prompt: str, user_prompt: str) -> Optional[str]:
    """Backward-compatible wrapper returning only the text."""
    return llm_generate(provider, client, system_prompt, user_prompt)[0]


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)

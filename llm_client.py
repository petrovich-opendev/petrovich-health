"""OpenRouter HTTP client. Replaces subprocess `claude -p` calls.

Configured via env:
  OPENROUTER_API_KEY — required
  LLM_MODEL          — default model id (e.g. openai/gpt-oss-120b:free)
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("health-bot")

_FALLBACK_MODEL = "openai/gpt-oss-120b:free"


def _default_model() -> str:
    return os.getenv("LLM_MODEL", _FALLBACK_MODEL)


# Backwards-compat module attribute. Read via attribute access for test snapshots;
# call _default_model() in code paths that should reflect runtime env changes.
DEFAULT_MODEL = _FALLBACK_MODEL


DEFAULT_TIMEOUT = 240
DEFAULT_RETRIES = 2
RETRY_DELAY_S = 30
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(RuntimeError):
    pass


def chat_completion(
    prompt: str,
    model: str | None = None,
    max_tokens: int = 8000,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """Send a single-turn user message and return the assistant text.

    Raises LLMError on persistent failure.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")

    model = model or _default_model()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    data_collection = os.getenv("OR_DATA_COLLECTION", "deny").strip().lower()
    if data_collection != "allow":
        body["provider"] = {"data_collection": "deny", "allow_fallbacks": True}
        log.warning(
            "OpenRouter privacy gate active (data_collection=deny); "
            "request may fail if no compliant provider is available"
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    log.info("Calling OpenRouter model=%s (%d chars, max_tokens=%d)", model, len(prompt), max_tokens)
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            r = requests.post(ENDPOINT, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            log.warning("OpenRouter request error attempt %d/%d: %s", attempt, retries + 1, e)
            if attempt <= retries:
                time.sleep(RETRY_DELAY_S)
                continue
            raise LLMError(f"OpenRouter request failed: {e}") from e

        if r.status_code != 200:
            detail = r.text[:300]
            last_err = LLMError(f"HTTP {r.status_code}: {detail}")
            log.warning("OpenRouter non-200 attempt %d/%d: %s", attempt, retries + 1, detail)
            # Retry on transient codes; bail on auth errors
            if r.status_code in (400, 401, 403):
                raise last_err
            if attempt <= retries:
                time.sleep(RETRY_DELAY_S)
                continue
            raise last_err

        try:
            data = r.json()
        except ValueError as e:
            last_err = e
            if attempt <= retries:
                time.sleep(RETRY_DELAY_S)
                continue
            raise LLMError(f"OpenRouter returned non-JSON: {r.text[:300]}") from e

        choices = data.get("choices") or []
        if not choices:
            err = (data.get("error") or {}).get("message") or "no choices in response"
            last_err = LLMError(err)
            if attempt <= retries:
                log.warning("OpenRouter empty choices attempt %d/%d: %s", attempt, retries + 1, err)
                time.sleep(RETRY_DELAY_S)
                continue
            raise last_err

        content = ((choices[0] or {}).get("message") or {}).get("content") or ""
        if not content.strip():
            last_err = LLMError("empty content in response")
            if attempt <= retries:
                log.warning("OpenRouter empty content attempt %d/%d", attempt, retries + 1)
                time.sleep(RETRY_DELAY_S)
                continue
            raise last_err

        usage = data.get("usage") or {}
        log.info(
            "OpenRouter ok: in=%s out=%s tot=%s cost=%s",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
            usage.get("cost"),
        )
        return content.strip()

    raise LLMError(f"OpenRouter exhausted retries: {last_err}")

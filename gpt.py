# gpt.py
# Centralized OpenAI (ChatGPT) integration for tg-scraper
from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, List, Optional

from openai import OpenAI
from openai import APIError, RateLimitError, APITimeoutError

logger = logging.getLogger("tg-scraper.gpt")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "400"))
RETRY_MAX = int(os.getenv("OPENAI_RETRY_MAX", "3"))
RETRY_BASE_DELAY = float(os.getenv("OPENAI_RETRY_BASE_DELAY", "0.6"))

_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """
    Lazily initialize the OpenAI client (uses OPENAI_API_KEY env var).
    """
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _retryable_call(fn, *args, **kwargs):
    """
    Minimal retry wrapper for 429/5xx/timeouts with exponential backoff.
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(1, RETRY_MAX + 1):
        try:
            return fn(*args, **kwargs)
        except (RateLimitError, APITimeoutError) as e:
            logger.warning("OpenAI transient error (attempt %s/%s): %s", attempt, RETRY_MAX, e)
        except APIError as e:
            code = getattr(e, "status_code", 500)
            if 500 <= code < 600:
                logger.warning("OpenAI 5xx (attempt %s/%s): %s", attempt, RETRY_MAX, e)
            else:
                raise
        time.sleep(delay)
        delay *= 2
    return fn(*args, **kwargs)


def _to_dict(obj: Any) -> Dict[str, Any]:
    """
    Normalize ScrapeResult (Pydantic) or dict-like into a plain dict.
    """
    # Pydantic v2
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return obj.model_dump()
    # Pydantic v1
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    # Already a dict
    if isinstance(obj, dict):
        return obj
    # Fallback: best-effort string
    return {"_raw": str(obj)}


def _trim_text(s: Optional[str], limit: int) -> str:
    s = (s or "").strip().replace("\r", " ")
    return (s[:limit] + "…") if len(s) > limit else s


def _compact_payload(sr: Dict[str, Any],
                     max_posts: int = 120,
                     max_chars_per_post: int = 500) -> Dict[str, Any]:
    """
    Reduce payload size for LLM while keeping signal.
    """
    slim: Dict[str, Any] = {
        "chan_username": sr.get("chan_username"),
        "chan_name": sr.get("chan_name"),
        "chan_description": _trim_text(sr.get("chan_description"), 1200),
        "chan_subscribers": sr.get("chan_subscribers"),
        "chan_lang": sr.get("chan_lang"),
        "chan_avg_posts_day": sr.get("chan_avg_posts_day"),
        "chan_avg_reactions_post": sr.get("chan_avg_reactions_post"),
        "chan_img": sr.get("chan_img"),
        "posts": []
    }

    posts: List[Dict[str, Any]] = sr.get("posts") or []
    for p in posts[:max_posts]:
        slim["posts"].append({
            "post_timestamp": p.get("post_timestamp"),
            "post_text": _trim_text(p.get("post_text"), max_chars_per_post),
            "post_reactions_count": p.get("post_reactions_count"),
            "post_views_count": p.get("post_views_count"),
            "post_comments": p.get("post_comments"),
        })
    return slim


def chan_analysis(
    scrape_result_obj: Any,
    prompt: str,
    *,
    model: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    max_posts: int = 120,
    max_chars_per_post: int = 500,
) -> str:
    """
    Run a custom analysis prompt against the channel's scraped data.

    Args:
        scrape_result_obj: ScrapeResult Pydantic object or dict.
        prompt: The user prompt/instruction you will define.
        model: Optional override of the OpenAI model.
        max_output_tokens: Optional override for max output tokens.
        max_posts: Cap the number of posts sent to the LLM (token safety).
        max_chars_per_post: Truncate each post's text (token safety).

    Returns:
        LLM response text (string). Empty string on handled failure.
    """
    sr = _to_dict(scrape_result_obj)
    slim = _compact_payload(sr, max_posts=max_posts, max_chars_per_post=max_chars_per_post)

    system_msg = (
        "You are a precise analyst. Use only the provided JSON to answer. "
        "If data is missing, say so explicitly."
    )

    client = get_openai_client()
    use_model = model or OPENAI_MODEL
    use_max_tokens = max_output_tokens or MAX_OUTPUT_TOKENS

    def _call():
        return client.responses.create(
            model=use_model,
            input=[
                {"role": "system", "content": system_msg},
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\n"
                        f"Here is the channel data as JSON:\n{slim}"
                    ),
                },
            ],
            max_output_tokens=use_max_tokens,
        )

    try:
        resp = _retryable_call(_call)
        return (resp.output_text or "").strip()
    except Exception as e:
        logger.warning("chan_analysis error: %s", e)
        return ""

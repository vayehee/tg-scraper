# gpt.py
from __future__ import annotations

import os
import time
import json
import re
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

TOPIC_CHOICES = [
    "News",
    "Politics",
    "Business",
    "Finance",
    "Tech",
    "Cybersecurity",
    "Lifestyle",
    "Sports",
    "Education",
    "OSINT",
    "NSFW",
    "Memes",
    "Deals",
    "Gaming",
    "Health",
    "Culture",
    "Unknown",
]

def get_openai_client() -> OpenAI:
    """Lazily initialize the OpenAI client (uses OPENAI_API_KEY env var)."""
    global _client
    if _client is None:
        _client = OpenAI()
    return _client

def _retryable_call(fn, *args, **kwargs):
    """Minimal retry wrapper for 429/5xx/timeouts with exponential backoff."""
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
    """Normalize Pydantic model or dict-like into a plain dict."""
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return obj.model_dump()
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return {"_raw": str(obj)}

def _trim_text(s: Optional[str], limit: int) -> str:
    s = (s or "").strip().replace("\r", " ")
    return (s[:limit] + "…") if len(s) > limit else s

def _make_chan_json_prompt(scrape: Dict[str, Any]) -> str:
    """
    Build an instruction-heavy prompt that forces a JSON-only reply.
    Uses global TOPIC_CHOICES for allowed 'chan_topic' values.
    """
    head = {
        "chan_username": scrape.get("chan_username"),
        "chan_name": scrape.get("chan_name"),
        "chan_description": (scrape.get("chan_description") or "")[:800],
        "chan_subscribers": scrape.get("chan_subscribers"),
        "chan_lang": scrape.get("chan_lang"),
        "chan_avg_posts_day": scrape.get("chan_avg_posts_day"),
        "chan_avg_reactions_post": scrape.get("chan_avg_reactions_post"),
    }

    posts = scrape.get("posts") or []
    pruned = []
    max_posts = 80   # keep analysis affordable but informative
    max_text = 400   # snippet per post
    for p in posts[:max_posts]:
        pruned.append({
            "post_timestamp": p.get("post_timestamp"),
            "post_text": (p.get("post_text") or "")[:max_text],
            "post_reactions_count": p.get("post_reactions_count"),
            "post_views_count": p.get("post_views_count"),
            "post_comments": p.get("post_comments"),
        })

    topics_block = "\n".join(f"- {t}" for t in TOPIC_CHOICES)

    return (
        'You are a strict JSON classifier. OUTPUT ONLY JSON. No prose, no code fences, no commentary.\n'
        'Schema:\n'
        '{\n'
        '   "chan_topic": "<ONE of the allowed topics, exact match>",\n'
        '   "chan_focus": "<3 words or fewer>",\n'
        '   "chan_geotarget": "<English place name or null>",\n'
        '}\n\n'
        'Rules:\n'
        f'- Allowed topics (choose EXACTLY one; if none fits, use "Unknown"): \n{topics_block}\n'
        '- chan_focus must be ≤3 words.\n'
        '- chan_geotarget = the LIKELY AUDIENCE location, not the subject of coverage.\n'
        '- Use strong signals in this priority order:\n'
        '   1. explicit self-location of the channel/community,\n'
        '   2. consistent phone formats/currencies/holidays/demonyms in header.\n'
        '   3. if the channel language has a clear national anchor (e.g., Hebrew→Israel, Russian→Russia, etc.),\n'
        '   use that national anchor as chan_geotarget, UNLESS there is strong explicit evidence'
        '   of a different target audience.\n'
        '- Prefer audience location over topic geography (e.g., if a Hebrew channel covers Lebanon,'
        'chan_geotarget should still be "Israel" unless it clearly targets another country.\n'
        '- If evidence is weak, use \"Unknown\" or null.\n'
        '- DO NOT add extra keys; return exactly the schema.\n\n'
        'Channel header:\n'
        f'{head}\n\n'
        'Channel language:\n'
        f'{head.get("chan_lang")}\n\n'
        'Sample of recent posts:\n'
        f'{pruned}\n\n'
        'Return ONLY the JSON object described above.'
    )

def chan_analysis(
    scrape: Dict[str, Any],
    model: str = OPENAI_MODEL,
) -> Dict[str, Any]:
    """
    Classify a channel into:
      - chan_topic (one of TOPIC_CHOICES or 'Unknown')
      - chan_focus (<= 3 words)
      - chan_geotarget (English place name or null)
    """
    # Ensure we always have a plain dict
    scrape = _to_dict(scrape)

    system_msg = (
        "You are a careful analyst. Follow the user's schema exactly. "
        "If information is insufficient, use 'Unknown' or null. "
        "Never output anything but JSON."
    )
    user_prompt = _make_chan_json_prompt(scrape)

    client = get_openai_client()

    try:
        # If your model supports JSON mode, uncomment the response_format:
        resp = _retryable_call(
            client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=MAX_OUTPUT_TOKENS,
            # response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content.strip()
        try:
            return json.loads(text)
        except Exception:
            cleaned = text.strip().strip("`")
            cleaned = re.sub(r"^json\n", "", cleaned, flags=re.I)
            return json.loads(cleaned)
    except Exception as e:
        raise RuntimeError(f"chan_analysis failed: {e}") from e

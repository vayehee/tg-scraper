# main.py
# FastAPI Telegram channel scraper (public web pages only)
# - Scrapes https://t.me/s/<username>
# - Robustly parses message text, views, timestamps, and reactions (all types)
# - Works with both classic and new Telegram reaction layouts
# - Designed for Cloud Run: listens on PORT (default 8080)

import os
import re
import html
import logging
from typing import List, Optional, Dict, Any, Tuple
from collections import defaultdict  # <-- needed

import httpx
from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# google-cloud-translate v3 client import (works whether installed as translate_v3 or translate)
try:
    from google.cloud import translate_v3 as translate
except ImportError:  # pragma: no cover
    from google.cloud import translate  # type: ignore

# ---------------------------
# Logging
# ---------------------------
logger = logging.getLogger("tg-scraper")
logging.basicConfig(level=logging.INFO)
logger.info("tg-scraper starting; ready to listen")

# ---------------------------
# Config
# ---------------------------

TELEGRAM_BASE = "https://t.me"
CHANNEL_PATH = "/s/{username}"
DEFAULT_LIMIT = 50           # max posts to return per call
MAX_LIMIT = 200
REQUEST_TIMEOUT = 20.0
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Username validation: letters/digits/_ and dot.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\.]+$")

# Views / counts like "26.8K", "1.2M", "12 345" etc.
_KNUM_RE = re.compile(r'(\d[\d,.\u202f\u00A0]*)([KkMm]?)$')  # include thin/nbsp spaces

# Translate config
TRANSLATE_LOCATION = os.getenv("TRANSLATE_LOCATION", "global")
_translate_client: Optional["translate.TranslationServiceClient"] = None

def get_translate_client() -> "translate.TranslationServiceClient":
    global _translate_client
    if _translate_client is None:
        _translate_client = translate.TranslationServiceClient()
    return _translate_client

def gcp_project_id() -> str:
    return os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""

def detect_language(text: str) -> Tuple[Optional[str], float]:
    """Return (language_code, confidence). Safe on errors."""
    text = (text or "").strip()
    if not text:
        return None, 0.0
    project = gcp_project_id()
    if not project:
        logger.warning("detect_language skipped: GOOGLE_CLOUD_PROJECT not set")
        return None, 0.0
    try:
        client = get_translate_client()
        parent = f"projects/{project}/locations/{TRANSLATE_LOCATION}"
        resp = client.detect_language(
            request={
                "parent": parent,
                "content": text,
                "mime_type": "text/plain",
            }
        )
        if not resp.languages:
            return None, 0.0
        best = max(resp.languages, key=lambda l: getattr(l, "confidence", 0.0))
        return best.language_code, float(getattr(best, "confidence", 0.0))
    except Exception as e:  # keep service resilient
        logger.warning("detect_language failed: %s", e)
        return None, 0.0

def majority_language(votes: Dict[str, int], conf_sum: Dict[str, float]) -> Optional[str]:
    """Pick language with most votes; break ties by higher total confidence."""
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], conf_sum.get(kv[0], 0.0)))[0]

# ---------------------------
# Models
# ---------------------------

class Post(BaseModel):
    post_timestamp: Optional[str] = Field(None, description="ISO timestamp from web page")
    post_text: Optional[str] = Field(None, description="Visible text content")
    post_reactions_count: int = Field(0, description="Sum of all reaction types")
    post_views_count: Optional[int] = Field(None, description="Views shown on post, if present")

class ScrapeResult(BaseModel):
    channel_username: str
    channel_name: Optional[str] = None
    channel_description: Optional[str] = None
    channel_followers: Optional[int] = None
    channel_lang: Optional[str] = None
    posts: List[Post]

# ---------------------------
# Utilities
# ---------------------------

def _strip_ws(s: Optional[str]) -> str:
    return (s or "").strip()

def _unescape(s: Optional[str]) -> str:
    return html.unescape(s or "")

def _parse_knum(text: Optional[str]) -> int:
    """Parse compact numbers like '26.8K', '1.2M', '12 345' into int."""
    if not text:
        return 0
    text = text.replace("\u202f", "").replace("\u00A0", "").replace(" ", "")
    m = _KNUM_RE.search(text)
    if not m:
        digits = re.sub(r'\D', '', text)
        return int(digits) if digits else 0
    num, suf = m.groups()
    try:
        num_f = float(num.replace(",", ""))
    except ValueError:
        num_f = float(re.sub(r"[^\d.]", "", num) or 0)
    if suf in ("K", "k"):
        num_f *= 1_000
    elif suf in ("M", "m"):
        num_f *= 1_000_000
    return int(num_f)

def _get_emoji_from_el(container: Tag) -> str:
    """Best-effort extraction of the emoji glyph/label across layouts."""
    for sel in ['i.emoji b', 'i b', '.tgme_widget_message_reaction_emoji', '.emoji b', 'b', 'i']:
        node = container.select_one(sel)
        if node:
            txt = node.get_text(strip=True)
            if txt:
                return txt
    for attr in ('aria-label', 'title'):
        if val := container.get(attr):
            return val.strip()
    return "UNKNOWN"

def _parse_reactions_for_message(msg: Tag) -> Dict[str, Any]:
    """
    Extract per-emoji and total reactions from a single message element.
    Supports:
      A) Old layout: <span class="tgme_reaction"> … 123 </span>
      B) New layout: .tgme_widget_message_inline_buttons a.tgme_widget_message_reaction
    """
    by_emoji: Dict[str, int] = {}

    # Old layout
    for span in msg.select('.tgme_widget_message_reactions span.tgme_reaction'):
        style = (span.get('style') or '').lower()
        if 'visibility:hidden' in style:
            continue  # spacer
        emoji = _get_emoji_from_el(span)
        cnt = _parse_knum(span.get_text(separator=' ', strip=True))
        if cnt:
            by_emoji[emoji] = by_emoji.get(emoji, 0) + cnt

    # New layout
    for a in msg.select('.tgme_widget_message_inline_buttons a.tgme_widget_message_reaction'):
        emoji = _get_emoji_from_el(a)
        cnt_el = a.select_one('.tgme_widget_message_reaction_count')
        cnt = _parse_knum(cnt_el.get_text(strip=True)) if cnt_el else 0
        if cnt:
            by_emoji[emoji] = by_emoji.get(emoji, 0) + cnt

    total = sum(by_emoji.values())
    return {"total": total, "by_emoji": by_emoji}

def _parse_views_for_message(msg: Tag) -> Optional[int]:
    """Extract views counter as int (e.g., '26.8K')."""
    el = msg.select_one('.tgme_widget_message_views')
    if not el:
        return None
    return _parse_knum(el.get_text(strip=True))

def _parse_timestamp_for_message(msg: Tag) -> Optional[str]:
    """Extract ISO timestamp (from <time datetime=...>) if available."""
    t = msg.select_one('.tgme_widget_message_date time, .tgme_widget_message_meta time')
    if t and t.has_attr('datetime'):
        return t['datetime']
    return None

def _message_text(msg: Tag) -> str:
    """Extract visible text content of the post (without footer/meta)."""
    tnode = msg.select_one('.tgme_widget_message_text')
    if tnode:
        for br in tnode.select('br'):
            br.replace_with('\n')
        text = tnode.get_text(separator='\n', strip=True)
        return _unescape(text)
    return ""

def _parse_channel_header(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    """Derive channel title/description/followers from header when present."""
    info: Dict[str, Optional[str]] = {}
    title_el = soup.select_one('.tgme_channel_info_header_title, .tgme_channel_info_header_title span')
    if title_el:
        info['name'] = title_el.get_text(strip=True)

    desc_el = soup.select_one('.tgme_channel_info_description')
    if desc_el:
        info['description'] = desc_el.get_text(separator="\n", strip=True)

    for sel in [
        '.tgme_channel_info_counter .counter_value',
        '.tgme_channel_info_counter_value',
        '.tgme_channel_info_counters .tgme_channel_info_counter'
    ]:
        c = soup.select_one(sel)
        if c:
            followers = _parse_knum(c.get_text(strip=True))
            if followers:
                info['followers'] = followers
                break
    return info

def _extract_messages(soup: BeautifulSoup) -> List[Tag]:
    """Find all message bubbles in the page."""
    return list(soup.select('.tgme_widget_message'))

def _find_next_before_id(soup: BeautifulSoup) -> Optional[str]:
    """
    Telegram allows paging with ?before=<post_id>.
    We try to find the smallest data-post id on the page and subtract 1 as a heuristic.
    """
    posts = []
    for el in soup.select('.tgme_widget_message[data-post]'):
        dp = el.get('data-post', '')
        parts = dp.split('/')
        if len(parts) == 2 and parts[1].isdigit():
            posts.append(int(parts[1]))
    if not posts:
        return None
    min_id = min(posts)
    if min_id <= 1:
        return None
    return str(min_id - 1)

# ---------------------------
# HTTP client
# ---------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type(httpx.HTTPError),
)
async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
            "Cache-Control": "no-cache",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.text

# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="Telegram Scraper", version="1.1.0")

@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "tg-scraper"}

@app.get(
    "/scrape",
    response_model=ScrapeResult,
    response_model_exclude_none=False,   # <-- keep keys even if None
    tags=["scrape"],
)
async def scrape_channel(
    username: str = Query(..., pattern=r"^[A-Za-z0-9_\.]+$", description="Telegram channel username"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Max posts to return"),
    before: Optional[str] = Query(None, description="Fetch older messages before numeric post id"),
):
    # Validate username explicitly to give a cleaner error than 500
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=422, detail="Invalid username format.")

    params = {}
    if before:
        if not before.isdigit():
            raise HTTPException(status_code=422, detail="'before' must be a numeric post id.")
        params["before"] = before

    start_url = TELEGRAM_BASE + CHANNEL_PATH.format(username=username)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        posts: List[Post] = []
        channel_name = None
        channel_description = None
        channel_followers = None

        url = start_url
        if params:
            url = url + "?" + "&".join([f"{k}={v}" for k, v in params.items()])

        # Iterate pages until we fill 'limit' or no more pages
        while len(posts) < limit and url:
            html_text = await _fetch(client, url)
            soup = BeautifulSoup(html_text, "lxml")

            # Capture channel info on first page
            if channel_name is None:
                hdr = _parse_channel_header(soup)
                channel_name = hdr.get("name")
                channel_description = hdr.get("description")
                channel_followers = hdr.get("followers")

            # Parse messages
            msg_nodes = _extract_messages(soup)
            if not msg_nodes:
                break

            for msg in msg_nodes:
                if len(posts) >= limit:
                    break

                # Skip service/system messages
                if not msg.select_one('.tgme_widget_message_bubble'):
                    continue

                txt = _message_text(msg)
                ts = _parse_timestamp_for_message(msg)
                views = _parse_views_for_message(msg)
                reactions = _parse_reactions_for_message(msg)
                total_reacts = reactions["total"]

                posts.append(
                    Post(
                        post_timestamp=ts,
                        post_text=txt or None,
                        post_reactions_count=total_reacts,
                        post_views_count=views,
                    )
                )

            # Prepare next page
            if len(posts) < limit:
                next_before = _find_next_before_id(soup)
                url = f"{start_url}?before={next_before}" if next_before else None

        # --- Detect channel language from the first 5 posts with text ---
        votes: Dict[str, int] = defaultdict(int)
        conf_sum: Dict[str, float] = defaultdict(float)

        sample_texts = [p.post_text for p in posts if p.post_text]
        for text in sample_texts[:5]:
            code, conf = detect_language(text[:2000])  # cap length to keep it cheap/fast
            if code:
                votes[code] += 1
                conf_sum[code] += conf

        channel_lang = majority_language(votes, conf_sum) or "und"  # <-- ensure a value

        return ScrapeResult(
            channel_username=username,
            channel_name=channel_name,
            channel_description=channel_description,
            channel_followers=channel_followers,
            channel_lang=channel_lang,  # <-- always present
            posts=posts,
        )

# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

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
import asyncio
from typing import List, Optional, Dict, Any, Tuple
from collections import defaultdict
from datetime import datetime

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

from gpt import chan_analysis
from strings import str_analysis

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
POSTS_LIMIT = 300          # max posts to return per call
REQUEST_TIMEOUT = 20.0
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Username validation: letters/digits/_ and dot.
USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


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
    # clean the text str input
    text = (text or "").strip()
    # return None on empty text str input
    if not text:
        return None, 0.0
    # make sure we know which GCP project to use
    project = gcp_project_id()
    if not project:
        logger.warning("detect_language skipped: GOOGLE_CLOUD_PROJECT not set")
        return None, 0.0
    # call Google Cloud Translate detect_language
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
        # return None if no languages detected
        if not resp.languages:
            return None, 0.0
        # pick the best language by confidence from the response
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

def _model_to_dict(m: BaseModel) -> Dict[str, Any]:
    # pydantic v2
    if hasattr(m, "model_dump"):
        return m.model_dump()
    # pydantic v1
    return m.dict()

class Post(BaseModel):
    post_timestamp: Optional[str] = Field(None, description="ISO timestamp from web page")
    post_text: Optional[str] = Field(None, description="Visible text content")
    post_reactions_count: int = Field(0, description="Sum of all reaction types")
    post_views_count: Optional[int] = Field(None, description="Views shown on post, if present")
    post_comments: Optional[int] = Field(None, description="Number of comments on the post")

class ScrapeResult(BaseModel):
    chan_img: Optional[str] = None
    chan_username: str
    chan_name: Optional[str] = None
    chan_description: Optional[str] = None
    chan_subscribers: Optional[int] = None
    chan_lang: Optional[str] = None
    chan_avg_posts_day: Optional[int] = None
    chan_avg_reactions_post: Optional[int] = None
    posts: List[Post]

# ---------------------------
# Utilities
# ---------------------------

_LEGACY_LANG_MAP = {
    "iw": "he",  # Hebrew
    "ji": "yi",  # Yiddish
    "in": "id",  # Indonesian
}

def normalize_lang(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.strip().lower()
    return _LEGACY_LANG_MAP.get(code, code)

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

def _abs_url(u: Optional[str]) -> Optional[str]:
    """Return absolute URL for Telegram-relative paths; else pass through."""
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return TELEGRAM_BASE + u
    return u

def _parse_channel_image(soup: BeautifulSoup) -> Optional[str]:
    """
    Extract channel image URL from the page.
    Priority:
      1) <meta property="og:image" content="...">
      2) <link rel="image_src" href="...">
      3) Common header photo selectors used by Telegram pages.
    Returns an absolute URL or None.
    """
    # 1) OpenGraph image
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return _abs_url(og["content"])

    # 2) Older/alternate hint
    link_img = soup.find("link", attrs={"rel": "image_src"})
    if link_img and link_img.get("href"):
        return _abs_url(link_img["href"])

    # 3) Header photo fallbacks (Telegram’s HTML varies by layout/AB tests)
    candidates = [
        ".tgme_channel_info_header_photo img",
        ".tgme_page .tgme_page_photo img",
        "img.tgme_page_photo_image",
        ".tgme_channel_info .tgme_page_photo_image img",
    ]
    for sel in candidates:
        el = soup.select_one(sel)
        if not el:
            continue
        # Prefer srcset (highest res) if present
        if el.has_attr("srcset"):
            parts = [p.strip().split(" ")[0] for p in el["srcset"].split(",") if p.strip()]
            if parts:
                return _abs_url(parts[-1])
        if el.has_attr("src") and el["src"]:
            return _abs_url(el["src"])

    return None

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

def _message_comment_count(msg: Tag) -> Optional[int]:
    """
    Extract the number of comments for a message.
    Handles both classic and new inline-button layouts, e.g.:
      <a class="tgme_widget_message_comments">123 comments</a>
      .tgme_widget_message_inline_buttons a[href*="comments"]
    Returns None when no comment link/count is present.
    """
    # 1) Classic selector
    a = msg.select_one('a.tgme_widget_message_comments')
    if a:
        cnt = _parse_knum(a.get_text(strip=True))
        return cnt if cnt > 0 else 0

    # 2) Inline buttons that include a comments link
    for cand in msg.select('.tgme_widget_message_inline_buttons a'):
        href = cand.get('href', '')
        if 'comments' in href:
            cnt = _parse_knum(cand.get_text(strip=True))
            return cnt if cnt > 0 else 0

    # 3) Bottom meta area variants sometimes hold comment link
    a = msg.select_one('.tgme_widget_message_bottom a.tgme_widget_message_comments')
    if a:
        cnt = _parse_knum(a.get_text(strip=True))
        return cnt if cnt > 0 else 0

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
    """Derive channel title/description/subscribers from header when present."""
    info: Dict[str, Optional[str]] = {}
    title_el = soup.select_one('.tgme_channel_info_header_title, .tgme_channel_info_header_title span')
    if title_el:
        info['name'] = title_el.get_text(strip=True)

    # channel description
    desc_el = soup.select_one('.tgme_channel_info_description')
    if desc_el:
        info['description'] = desc_el.get_text(separator="\n", strip=True)

    # channel subscribers
    for sel in [
        '.tgme_channel_info_counter .counter_value',
        '.tgme_channel_info_counter_value',
        '.tgme_channel_info_counters .tgme_channel_info_counter'
    ]:
        c = soup.select_one(sel)
        if c:
            subscribers = _parse_knum(c.get_text(strip=True))
            if subscribers:
                info['subscribers'] = subscribers
                break

    # channel OpenGraph image
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        img_og = og_img["content"]

    # channel older rel image_source
    link_img = soup.find("link", attrs={"rel": "image_src"})
    if link_img and link_img.get("href"):
        img_link = _abs_url(link_img["href"])

    # channel header photo fallbacks (Telegram’s HTML varies by layout/AB tests)
    candidates = [
        ".tgme_channel_info_header_photo img",
        ".tgme_page .tgme_page_photo img",
        "img.tgme_page_photo_image",
        ".tgme_channel_info .tgme_page_photo_image img",
    ]
    for sel in candidates:
        el = soup.select_one(sel)
        if not el:
            continue
        # Prefer srcset (highest res) if present
        if el.has_attr("srcset"):
            parts = [p.strip().split(" ")[0] for p in el["srcset"].split(",") if p.strip()]
            if parts:
                img_fallback = _abs_url(parts[-1])
        if el.has_attr("src") and el["src"]:
            img_fallback = _abs_url(el["src"])

    # select best available image
    for img in (img_og, img_link, img_fallback):
        if img:
            info["img"] = img
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

def _avg_posts_per_day(posts: List[Post]) -> Optional[int]:
    """
    Group posts by calendar date (UTC offset respected if present) and
    return average posts per distinct day. Returns None if no dated posts.
    """
    day_counts: Dict[str, int] = defaultdict(int)

    for p in posts:
        ts = p.post_timestamp
        if not ts:
            continue
        try:
            # supports '...Z' and '+00:00' etc.
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            day_key = str(dt.date())  # 'YYYY-MM-DD'
            day_counts[day_key] += 1
        except Exception:
            # Fallback: naive slice if ISO parsing fails
            if len(ts) >= 10 and ts[4] == '-' and ts[7] == '-':
                day_counts[ts[:10]] += 1

    if not day_counts:
        return None

    total_posts = sum(day_counts.values())
    avg = total_posts / len(day_counts)
    return int(round(avg))

def _avg_reactions_per_post(posts: List[Post]) -> Optional[int]:
    """
    Return the rounded average of post_reactions_count across all posts.
    Treats missing counts as 0 if any appear (but your model defaults to 0).
    """
    if not posts:
        return None

    total = 0
    n = 0
    for p in posts:
        try:
            total += int(p.post_reactions_count or 0)
            n += 1
        except Exception:
            continue

    if n == 0:
        return None

    return int(round(total / n))

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

app = FastAPI(title="Telegram Scraper", version="1.2.0")

@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "tg-scraper"}

@app.get(
    "/scrape",
    # Remove response_model restriction so the handler can return any schema
    response_model=None,
    tags=["scrape"],
)

async def scrape_channel(
    username: str = Query(
        ..., 
        pattern=USERNAME_REGEX.pattern, 
        description="Telegram channel username"
    ),
):
    # Validate username explicitly to give a cleaner error than 500
    if not USERNAME_REGEX.match(username):
        return "Invalid channel username."

    start_url = TELEGRAM_BASE + CHANNEL_PATH.format(username=username)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        posts: List[Post] = []
        chan_name = None
        chan_description = None
        chan_subscribers = None
        chan_img = None

        url = start_url

        # Iterate pages until we fill 'limit' or no more pages
        while len(posts) < POSTS_LIMIT and url:
            html_text = await _fetch(client, url)
            soup = BeautifulSoup(html_text, "lxml")

            # Capture channel info on first page
            if chan_name is None:
                header = _parse_channel_header(soup)
                chan_name = header.get("name")
                chan_description = header.get("description")
                chan_subscribers = header.get("subscribers")
                chan_img = _parse_channel_image(soup)

            # Parse messages
            msg_nodes = _extract_messages(soup)
            if not msg_nodes:
                break

            for msg in msg_nodes:
                if len(posts) >= POSTS_LIMIT:
                    break

                # Skip service/system messages
                if not msg.select_one('.tgme_widget_message_bubble'):
                    continue

                txt = _message_text(msg)
                ts = _parse_timestamp_for_message(msg)
                views = _parse_views_for_message(msg)
                reactions = _parse_reactions_for_message(msg)
                total_reacts = reactions["total"]
                comments = _message_comment_count(msg)

                posts.append(
                    Post(
                        post_timestamp=ts,
                        post_text=txt or None,
                        post_reactions_count=total_reacts,
                        post_views_count=views,
                        post_comments=comments,
                    )
                )

            # Prepare next page
            if len(posts) < POSTS_LIMIT:
                next_before = _find_next_before_id(soup)
                url = f"{start_url}?before={next_before}" if next_before else None

    # --- Detect channel language: prefer description, else fall back to posts ---
    
    # prep channel name
    chan_name_str = (chan_name or "").strip()
    chan_name_lang, chan_name_ = detect_language(chan_name_str)
    
    # prep channel description
    chan_desc_str = (chan_description or "").strip()
    chan_desc_lang = detect_language(chan_desc_str)

    # initialize chan_lang
    chan_lang: Optional[str] = None

    code, conf = detect_language(chan_name_str)
    if code and conf >= 0.9:
        chan_lang = normalize_lang(code)



    # language detection logic
    if len(chan_name_str) > 3:
        code, conf = detect_language(chan_name_str[:2000])

    if len(chan_desc_str) > 3:
        # 1) If channel description exists and is longer than 3 chars,
        #    detect language ONLY from the description.
        code, conf = detect_language(chan_desc_str[:2000])  # cap length for safety
        if code:
            chan_lang = normalize_lang(code)
        else:
            chan_lang = "und"
    else:
        # 2) Otherwise, detect language from the first 5 posts with text (current logic).
        votes: Dict[str, int] = defaultdict(int)
        conf_sum: Dict[str, float] = defaultdict(float)

        sample_texts = [p.post_text for p in posts if p.post_text]
        for text in sample_texts[:5]:
            code, conf = detect_language(text[:2000])
            if code:
                votes[code] += 1
                conf_sum[code] += conf

        raw_lang = majority_language(votes, conf_sum) or "und"
        chan_lang = normalize_lang(raw_lang)

    chan_avg_posts_day = _avg_posts_per_day(posts)
    chan_avg_reactions_post = _avg_reactions_per_post(posts)

    scrape_obj = ScrapeResult(
        chan_img=chan_img,
        chan_username=username,
        chan_name=chan_name,
        chan_description=chan_description,
        chan_subscribers=chan_subscribers,
        chan_lang=chan_lang,
        chan_avg_posts_day=chan_avg_posts_day,
        chan_avg_reactions_post=chan_avg_reactions_post,
        posts=posts,
    )

    result: Dict[str, Any] = _model_to_dict(scrape_obj)

    analysis = await asyncio.to_thread(chan_analysis, result)

    if isinstance(analysis, dict):
        result.update(analysis)
    else:
        result["analysis_raw"] = analysis

    # Remove heavy posts array from both top-level and nested scrape
    result.pop("posts", None)

    result["chan_name_ext"] = str_analysis(result["chan_name"])

    return result

# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

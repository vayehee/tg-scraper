# scrape.py

import html
import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("tg-scraper.scrape")

# ---------------------------
# Config
# ---------------------------

TELEGRAM_BASE = "https://t.me"
CHANNEL_PATH = "/s/{username}"
POSTS_LIMIT = 300          # max posts to return per call
REQUEST_TIMEOUT = 20.0

# Windows 11-style Chrome on desktop (Win11 still uses Windows NT 10.0 token)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Views / counts like "26.8K", "1.2M", "12 345" etc.
KNUM_RE = re.compile(r'(\d[\d,.\u202f\u00A0]*)([KkMm]?)$')  # include thin/nbsp spaces


# ---------------------------
# Models
# ---------------------------

class ChannelMeta(BaseModel):
    chan_username: str
    chan_img: Optional[str] = None
    chan_name: Optional[str] = None
    chan_description: Optional[str] = None
    chan_subscribers: Optional[int] = None
    chan_avg_posts_per_day: Optional[int] = None
    chan_avg_views_per_post: Optional[int] = None
    chan_avg_comments_per_post: Optional[int] = None
    chan_avg_reactions_per_post: Optional[int] = None


class ChannelPosts(BaseModel):
    post_timestamp: Optional[str] = Field(None, description="ISO timestamp from web page")
    post_text: Optional[str] = Field(None, description="Visible text content")
    post_reactions_count: int = Field(0, description="Sum of all reaction types")
    post_views_count: Optional[int] = Field(None, description="Views shown on post, if present")
    post_comments_count: Optional[int] = Field(None, description="Number of comments on the post")


# ---------------------------
# Generic utilities
# ---------------------------

# Unescape HTML entities in a string, safely handling None.
def _unescape(s: Optional[str]) -> str:
    return html.unescape(s or "")


# Parse compact number strings like "26.8K", "1.2M", "12 345" into an int.
def _parse_knum(text: Optional[str]) -> int:
    """Parse compact numbers like '26.8K', '1.2M', '12 345' into int."""
    if not text:
        return 0
    text = text.replace("\u202f", "").replace("\u00A0", "").replace(" ", "")
    m = KNUM_RE.search(text)
    if not m:
        digits = re.sub(r"\D", "", text)
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


# Convert relative Telegram URLs into absolute URLs.
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


# ---------------------------
# Channel-related helpers
# ---------------------------

# Extract the best channel image URL from HTML (og:image, link, header photo).
def _parse_chan_img(soup: BeautifulSoup) -> Optional[str]:
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


# Parse channel title/description/subscribers from the header area.
def _parse_chan_meta(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    """Derive channel title/description/subscribers from header when present."""
    info: Dict[str, Optional[str]] = {}
    title_el = soup.select_one(".tgme_channel_info_header_title, .tgme_channel_info_header_title span")
    if title_el:
        info["name"] = title_el.get_text(strip=True)

    # channel description
    desc_el = soup.select_one(".tgme_channel_info_description")
    if desc_el:
        info["description"] = desc_el.get_text(separator="\n", strip=True)

    # channel subscribers
    for sel in [
        ".tgme_channel_info_counter .counter_value",
        ".tgme_channel_info_counter_value",
        ".tgme_channel_info_counters .tgme_channel_info_counter",
    ]:
        c = soup.select_one(sel)
        if c:
            subscribers = _parse_knum(c.get_text(strip=True))
            if subscribers:
                info["subscribers"] = subscribers
                break

    return info


# Collect all message bubble nodes (posts) from the soup.
def _parse_chan_posts(soup: BeautifulSoup) -> List[Tag]:
    """Find all message bubbles in the page."""
    return list(soup.select(".tgme_widget_message"))


# Determine the next ?before=<id> value for paging older posts.
def _parse_pagination_post_id(soup: BeautifulSoup) -> Optional[str]:
    """
    Telegram allows paging with ?before=<post_id>.
    We try to find the smallest data-post id on the page and subtract 1.
    """
    posts: List[int] = []
    for el in soup.select(".tgme_widget_message[data-post]"):
        dp = el.get("data-post", "")
        parts = dp.split("/")
        if len(parts) == 2 and parts[1].isdigit():
            posts.append(int(parts[1]))
    if not posts:
        return None
    min_id = min(posts)
    if min_id <= 1:
        return None
    return str(min_id - 1)


# ---------------------------
# Post-related helpers
# ---------------------------

# Extract emoji glyph/label from various Telegram reaction layouts.
def _get_reaction_emojis(container: Tag) -> str:
    """Best-effort extraction of the emoji glyph/label across layouts."""
    for sel in [
        "i.emoji b",
        "i b",
        ".tgme_widget_message_reaction_emoji",
        ".emoji b",
        "b",
        "i",
    ]:
        node = container.select_one(sel)
        if node:
            txt = node.get_text(strip=True)
            if txt:
                return txt
    for attr in ("aria-label", "title"):
        if val := container.get(attr):
            return val.strip()
    return "UNKNOWN"


# Parse per-emoji and total reactions for a single message bubble.
def _parse_post_reactions(msg: Tag) -> Dict[str, Any]:
    """
    Extract per-emoji and total reactions from a single message element.
    Supports:
      A) Old layout: <span class="tgme_reaction"> … 123 </span>
      B) New layout: .tgme_widget_message_inline_buttons a.tgme_widget_message_reaction
    """
    by_emoji: Dict[str, int] = {}

    # Old layout
    for span in msg.select(".tgme_widget_message_reactions span.tgme_reaction"):
        style = (span.get("style") or "").lower()
        if "visibility:hidden" in style:
            continue  # spacer
        emoji = _get_reaction_emojis(span)
        cnt = _parse_knum(span.get_text(separator=" ", strip=True))
        if cnt:
            by_emoji[emoji] = by_emoji.get(emoji, 0) + cnt

    # New layout
    for a in msg.select(".tgme_widget_message_inline_buttons a.tgme_widget_message_reaction"):
        emoji = _get_reaction_emojis(a)
        cnt_el = a.select_one(".tgme_widget_message_reaction_count")
        cnt = _parse_knum(cnt_el.get_text(strip=True)) if cnt_el else 0
        if cnt:
            by_emoji[emoji] = by_emoji.get(emoji, 0) + cnt

    total = sum(by_emoji.values())
    return {"total": total, "by_emoji": by_emoji}


# Extract the numeric views count from a message bubble.
def _parse_post_views(msg: Tag) -> Optional[int]:
    """Extract views counter as int (e.g., '26.8K')."""
    el = msg.select_one(".tgme_widget_message_views")
    if not el:
        return None
    return _parse_knum(el.get_text(strip=True))


# Extract the ISO timestamp from a message’s <time> tag.
def _parse_post_timestamp(msg: Tag) -> Optional[str]:
    """Extract ISO timestamp (from <time datetime=...>) if available."""
    t = msg.select_one(".tgme_widget_message_date time, .tgme_widget_message_meta time")
    if t and t.has_attr("datetime"):
        return t["datetime"]
    return None


# Extract the number of comments associated with a message.
def _parse_post_comment_count(msg: Tag) -> Optional[int]:
    """
    Extract the number of comments for a message.
    Handles classic layouts, inline buttons, and the new replies-element footer.
    """
    # 1) Classic selector
    a = msg.select_one("a.tgme_widget_message_comments")
    if a:
        cnt = _parse_knum(a.get_text(strip=True))
        return cnt if cnt > 0 else 0

    # 2) Inline buttons that include a comments link
    for cand in msg.select(".tgme_widget_message_inline_buttons a"):
        href = cand.get("href", "")
        if "comments" in href:
            cnt = _parse_knum(cand.get_text(strip=True))
            return cnt if cnt > 0 else 0

    # 3) Bottom meta area variants sometimes hold comment link
    a = msg.select_one(".tgme_widget_message_bottom a.tgme_widget_message_comments")
    if a:
        cnt = _parse_knum(a.get_text(strip=True))
        return cnt if cnt > 0 else 0

    # 4) New replies footer used in some Telegram layouts:
    #    <replies-element> ... <span class="replies-footer-text"><span class="i18n">2 Comments</span></span>
    replies = (
        msg.select_one("replies-element .replies-footer-text .i18n")
        or msg.select_one("replies-element .replies-footer-text")
    )
    if replies:
        cnt = _parse_knum(replies.get_text(strip=True))
        return cnt if cnt > 0 else 0

    return None


# Extract visible post text content from a message bubble.
def _parse_post_text(msg: Tag) -> str:
    """Extract visible text content of the post (without footer/meta)."""
    tnode = msg.select_one(".tgme_widget_message_text")
    if tnode:
        for br in tnode.select("br"):
            br.replace_with("\n")
        text = tnode.get_text(separator="\n", strip=True)
        return _unescape(text)
    return ""


# ---------------------------
# Calc-related helpers
# ---------------------------

# Compute average posts per distinct calendar day based on timestamps.
def _calc_avg_posts_per_day(posts: List[ChannelPosts]) -> Optional[int]:
    """Return average posts per distinct day, based on timestamps."""
    day_counts: Dict[str, int] = defaultdict(int)

    for p in posts:
        ts = p.post_timestamp
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            day_key = str(dt.date())  # 'YYYY-MM-DD'
            day_counts[day_key] += 1
        except Exception:
            if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
                day_counts[ts[:10]] += 1

    if not day_counts:
        return None

    total_posts = sum(day_counts.values())
    avg = total_posts / len(day_counts)
    return int(round(avg))


# Compute average views per post across all posts.
def _calc_avg_views_per_post(posts: List[ChannelPosts]) -> Optional[int]:
    """Return rounded average of post_views_count across all posts."""
    if not posts:
        return None

    total = 0
    n = 0
    for p in posts:
        try:
            total += int(p.post_views_count or 0)
            n += 1
        except Exception:
            continue

    if n == 0:
        return None

    return int(round(total / n))


# Compute average comments per post across all posts.
def _calc_avg_comments_per_post(posts: List[ChannelPosts]) -> Optional[int]:
    """Return rounded average of post_comments_count across all posts."""
    if not posts:
        return None

    total = 0
    n = 0
    for p in posts:
        try:
            if p.post_comments_count is None:
                continue  # skip posts where we have no comment data
            total += int(p.post_comments_count)
            n += 1
        except Exception:
            continue

    if n == 0:
        return None

    return int(round(total / n))


# Compute average reactions per post across all posts.
def _calc_avg_reactions_per_post(posts: List[ChannelPosts]) -> Optional[int]:
    """Return rounded average of post_reactions_count across all posts."""
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

# Fetch a Telegram page with retry logic using an async HTTP client.
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type(httpx.HTTPError),
)
async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # English primary, EU-ish flavour, French as a common secondary
            "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.text


# ---------------------------
# Core scrape
# ---------------------------

# Core internal scraper that fetches raw channel meta (no aggregates) and posts.
async def _scrape_chan(username: str) -> Tuple[ChannelMeta, List[ChannelPosts]]:
    """Internal: scrape channel pages once and return base meta plus posts."""
    start_url = TELEGRAM_BASE + CHANNEL_PATH.format(username=username)
    posts: List[ChannelPosts] = []
    chan_name = None
    chan_description = None
    chan_subscribers = None
    chan_img = None

    url = start_url

    async with httpx.AsyncClient(follow_redirects=True) as client:
        while len(posts) < POSTS_LIMIT and url:
            html_text = await _fetch(client, url)
            soup = BeautifulSoup(html_text, "lxml")

            # Capture channel info on first page
            if chan_name is None:
                header = _parse_chan_meta(soup)
                chan_name = header.get("name")
                chan_description = header.get("description")
                chan_subscribers = header.get("subscribers")
                chan_img = chan_img or _parse_chan_img(soup)

            # Parse messages
            msg_nodes = _parse_chan_posts(soup)
            if not msg_nodes:
                break

            for msg in msg_nodes:
                if len(posts) >= POSTS_LIMIT:
                    break

                # Skip service/system messages
                if not msg.select_one(".tgme_widget_message_bubble"):
                    continue

                txt = _parse_post_text(msg)
                ts = _parse_post_timestamp(msg)
                views = _parse_post_views(msg)
                reactions = _parse_post_reactions(msg)
                total_reacts = reactions["total"]
                comments = _parse_post_comment_count(msg)

                posts.append(
                    ChannelPosts(
                        post_timestamp=ts,
                        post_text=txt or None,
                        post_reactions_count=total_reacts,
                        post_views_count=views,
                        post_comments_count=comments,
                    )
                )

            # Prepare next page
            if len(posts) < POSTS_LIMIT:
                next_before = _parse_pagination_post_id(soup)
                url = f"{start_url}?before={next_before}" if next_before else None

    # Base meta without aggregates
    meta = ChannelMeta(
        chan_username=username,
        chan_img=chan_img,
        chan_name=chan_name,
        chan_description=chan_description,
        chan_subscribers=chan_subscribers,
        chan_avg_posts_per_day=None,
        chan_avg_views_per_post=None,
        chan_avg_comments_per_post=None,
        chan_avg_reactions_per_post=None,
    )

    return meta, posts


# ---------------------------
# Public API
# ---------------------------

async def CHANNEL(username: str) -> Dict[str, Any]:
    """
    Public API: scrape channel, compute aggregates, and return metadata as dict.
    """
    base_meta, posts = await _scrape_chan(username)

    chan_avg_posts_per_day = _calc_avg_posts_per_day(posts)
    chan_avg_views_per_post = _calc_avg_views_per_post(posts)
    chan_avg_comments_per_post = _calc_avg_comments_per_post(posts)
    chan_avg_reactions_per_post = _calc_avg_reactions_per_post(posts)

    meta = ChannelMeta(
        chan_username=base_meta.chan_username,
        chan_img=base_meta.chan_img,
        chan_name=base_meta.chan_name,
        chan_description=base_meta.chan_description,
        chan_subscribers=base_meta.chan_subscribers,
        chan_avg_posts_per_day=chan_avg_posts_per_day,
        chan_avg_views_per_post=chan_avg_views_per_post,
        chan_avg_comments_per_post=chan_avg_comments_per_post,
        chan_avg_reactions_per_post=chan_avg_reactions_per_post,
    )

    return meta.model_dump()


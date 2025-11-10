#main.py
from fastapi import FastAPI, HTTPException, Query
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
import re
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-scraper")

TG_BASE = "https://t.me/s"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

app = FastAPI(title="tg-scraper", version="1.0.0")

class Post(BaseModel):
    post_timestamp: Optional[str] = Field(None, description="ISO timestamp from web page")
    post_text: Optional[str] = None
    post_reactions_count: int = 0
    post_views_count: Optional[int] = None

class ScrapeResult(BaseModel):
    channel_username: str
    channel_name: Optional[str] = None
    channel_description: Optional[str] = None
    channel_followers: Optional[int] = None
    posts: List[Post] = Field(default_factory=list)

class TelegramHTTPError(Exception):
    pass

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
    retry=retry_if_exception_type((TelegramHTTPError, httpx.HTTPError)),
)
async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=30)
    if resp.status_code == 404:
        raise TelegramHTTPError("Channel not found or not public.")
    if resp.status_code >= 500:
        raise TelegramHTTPError(f"Telegram returned {resp.status_code}")
    resp.raise_for_status()
    return resp.text

def _parse_int_from_text(txt: str) -> Optional[int]:
    if not txt:
        return None
    # normalize like "12.3K", "1,234", "2.5M"
    txt = txt.strip()
    # replace thin/nbsp spaces
    txt = txt.replace("\xa0", " ").replace("\u202f", " ")
    # Extract leading number with optional suffix
    m = re.search(r"([0-9][0-9\.,]*)\s*([KkMm])?", txt)
    if not m:
        # fallback numbers like '1234'
        m2 = re.search(r"\d+", txt.replace(",", "").replace(".", ""))
        return int(m2.group()) if m2 else None
    num = m.group(1).replace(",", "")
    # If contains more than one dot, keep first
    try:
        val = float(num)
    except ValueError:
        # strip non-digits
        digits = re.sub(r"[^\d.]", "", num)
        val = float(digits) if digits else 0.0
    suffix = m.group(2)
    if suffix:
        if suffix.lower() == "k":
            val *= 1_000
        elif suffix.lower() == "m":
            val *= 1_000_000
    return int(val)

def _sum_reactions(msg: BeautifulSoup) -> int:
    # Reactions are grouped under 'tgme_widget_message_reactions'
    total = 0
    for group in msg.select(".tgme_widget_message_reactions .tgme_widget_message_reaction_count"):
        total += _parse_int_from_text(group.get_text(strip=True)) or 0
    # Some older markup uses '.tgme_widget_message_reactions' with anchors containing counts
    if total == 0:
        for a in msg.select(".tgme_widget_message_reactions a"):
            # each a may have a span count
            span = a.select_one(".tgme_widget_message_reaction_count")
            if span:
                total += _parse_int_from_text(span.get_text(strip=True)) or 0
    return total

def _parse_posts(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg:
            continue

        # Timestamp
        ts = None
        date_a = msg.select_one("a.tgme_widget_message_date time")
        if date_a and date_a.has_attr("datetime"):
            ts = date_a["datetime"]

        # Text
        text_el = msg.select_one(".tgme_widget_message_text")
        text = None
        if text_el:
            # get visible text, keep line breaks
            text = text_el.get_text("\n", strip=True)

        # Views
        views = None
        v_el = msg.select_one(".tgme_widget_message_views")
        if v_el:
            views = _parse_int_from_text(v_el.get_text(strip=True))

        # Reactions (sum of all counts)
        reactions = _sum_reactions(msg)

        posts.append(
            dict(
                post_timestamp=ts,
                post_text=text,
                post_reactions_count=reactions,
                post_views_count=views,
            )
        )
    return posts

def _parse_channel_meta(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    # Name/title
    name_el = soup.select_one(".tgme_channel_info_header_title")
    channel_name = None
    if name_el:
        channel_name = name_el.get_text(strip=True)

    # Description
    desc_el = soup.select_one(".tgme_channel_info_description")
    channel_description = None
    if desc_el:
        channel_description = desc_el.get_text("\n", strip=True)

    # Followers / subscribers (look into counters block)
    followers = None
    for cnt in soup.select(".tgme_channel_info_counters .tgme_channel_info_counter"):
        label = cnt.get_text(" ", strip=True).lower()
        if "subscriber" in label or "member" in label or "followers" in label:
            num_el = cnt.select_one(".tgme_channel_info_counter_value")
            if num_el:
                followers = _parse_int_from_text(num_el.get_text(strip=True))
            else:
                # sometimes the number sits before the word
                followers = _parse_int_from_text(label)
            break

    return {
        "channel_name": channel_name,
        "channel_description": channel_description,
        "channel_followers": followers,
    }

async def _collect_posts(username: str, max_posts: int = 100) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    seen = set()
    before: Optional[int] = None
    pages = 0
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": DEFAULT_UA}) as client:
        while len(posts) < max_posts and pages < 20:  # safety cap
            url = f"{TG_BASE}/{username}"
            if before:
                url += f"?before={before}"
            html = await _fetch(client, url)
            soup = BeautifulSoup(html, "lxml")

            page_posts = _parse_posts(soup)
            if not page_posts:
                break

            # Determine min message id on the page for pagination
            msg_ids = []
            for a in soup.select("a.tgme_widget_message_date"):
                href = a.get("href", "")
                m = re.search(r"/(\d+)(\?.*)?$", href)
                if m:
                    msg_ids.append(int(m.group(1)))

            added_this_page = 0
            for p in page_posts:
                # Make a dedup key with timestamp+text length to reduce dup risk
                key = (p.get("post_timestamp"), (p.get("post_text") or "")[:50])
                if key in seen:
                    continue
                seen.add(key)
                posts.append(p)
                added_this_page += 1
                if len(posts) >= max_posts:
                    break

            if not msg_ids:
                # No pagination anchors found; break
                break

            new_before = min(msg_ids) - 1
            if before is not None and new_before >= before:
                break  # no progress
            before = new_before
            pages += 1

            if added_this_page == 0:
                break

    return posts[:max_posts]

@app.get("/scrape", response_model=ScrapeResult)
async def scrape(username: str = Query(..., min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_\.]+$")):
    """
    Scrape public Telegram channel web page and return meta + last <=100 posts.
    """
    # First page to pull channel meta
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": DEFAULT_UA}) as client:
        html = await _fetch(client, f"{TG_BASE}/{username}")
    soup = BeautifulSoup(html, "lxml")

    # If Telegram shows a "This channel can't be displayed" banner, treat as not public
    if soup.find(string=re.compile("This channel can't be displayed|is not available", re.I)):
        raise HTTPException(status_code=404, detail="Channel is not publicly accessible.")

    meta = _parse_channel_meta(soup)
    posts = await _collect_posts(username, max_posts=100)

    if not posts:
        # Could be empty/new channel, still return meta
        pass

    return ScrapeResult(
        channel_username=username,
        channel_name=meta["channel_name"],
        channel_description=meta["channel_description"],
        channel_followers=meta["channel_followers"],
        posts=[Post(**p) for p in posts],
    )

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/")
async def index():
    return {"service": "tg-scraper", "status": "ok", "docs": "/docs", "health": "/healthz"}

@app.on_event("startup")
async def on_startup():
    logger.info("tg-scraper starting; ready to listen")

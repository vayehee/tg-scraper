# main.py

import os
import re
import html
import logging
import asyncio
from typing import List, Optional, Dict, Any, Tuple
from collections import defaultdict
from datetime import datetime
from fastapi import FastAPI, Query, HTTPException


import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# import helper
import scrape
import gpt
import gtranslate



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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------
# Utilities
# ---------------------------

# Valid Telegram channel username regex
USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
# Views / counts like "26.8K", "1.2M", "12 345" etc.
KNUM_RE = re.compile(r'(\d[\d,.\u202f\u00A0]*)([KkMm]?)$')  # include thin/nbsp spaces

# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="Telegram Scraper", version="1.2.0")

@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "tg-scraper"}

@app.get("/scrape", response_model=None, tags=["scrape"])

async def scrape(
    username: str = Query(..., description="Telegram channel username"),
):
    
    # 1. Validate username
    if not USERNAME_REGEX.match(username):
        raise HTTPException(status_code=400, detail="Invalid channel username.")
    return

    # 2. Scrape channel and posts
    channel = await scrape.CHANNEL(username)

    return channel

# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

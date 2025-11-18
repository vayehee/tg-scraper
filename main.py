# main.py

import os
import re
import logging
from typing import Optional

from fastapi import FastAPI, Query, HTTPException

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
# Config / utilities
# ---------------------------

USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="Telegram Scraper", version="1.2.0")


@app.get("/", response_model=None, tags=["health", "chan"])
async def root(
    chan: Optional[str] = Query(
        None,
        description="Telegram channel username (Telegram @username without @)",
    ),
):
    # 1. If no channel provided, return health check
    if chan is None:
        return {"status": "ok", "service": "tg-scraper"}

    # 2. Validate username
    if not USERNAME_REGEX.match(chan):
        raise HTTPException(status_code=400, detail="Invalid channel username.")

    # 3. Scrape channel meta & aggregates
    channel = await scrape.CHANNEL(chan)
    return channel


# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

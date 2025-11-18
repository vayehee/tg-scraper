# main.py

import os
import re
import logging
from fastapi import FastAPI, Query, HTTPException

from scrape
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

# Valid Telegram channel username regex
USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="Telegram Scraper", version="1.2.0")

@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "tg-scraper"}

@app.get("/chan", response_model=None, tags=["chan"])
async def chan(
    username: str = Query(..., description="Telegram channel username"),
):
    # 1. Validate username
    if not USERNAME_REGEX.match(username):
        raise HTTPException(status_code=400, detail="Invalid channel username.")

    # 2. Scrape channel meta
    channel = await scrape.CHANNEL(username)
    return channel

# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

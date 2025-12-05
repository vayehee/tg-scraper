# main.py

import os
import re
import logging
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import scrape
import gpt
import gtranslate

from pathlib import Path
import hmac
import hashlib


# ---------------------------
# Logging
# ---------------------------
logger = logging.getLogger("tg-scraper")
logging.basicConfig(level=logging.INFO)
logger.info("tg-scraper starting; ready to listen")

# ---------------------------
# Config / utilities
# ---------------------------

BASE_DIR = Path(__file__).resolve().parent
LOGIN_HTML_PATH = BASE_DIR / "login.html"

TELEGRAM_BOT_NAME = os.getenv("TELEGRAM_BOT_NAME", "YOUR_BOT_NAME_HERE")  # without @
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # required for verification

USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


# ---------------------------
# Telegram Auth Verification
# ---------------------------


def verify_telegram_auth(payload: dict) -> bool:
    """
    Verify Telegram Login Widget data using the bot token.
    Implements the check-string / HMAC-SHA256 algorithm from Telegram docs.
    """
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is not set; cannot verify Telegram login.")
        return False

    data = dict(payload)  # copy, don't mutate caller
    received_hash = data.pop("hash", None)
    if not received_hash:
        return False

    # Build data-check-string: sorted key=value lines except 'hash'
    data_check_arr = [f"{k}={v}" for k, v in sorted(data.items(), key=lambda kv: kv[0])]
    data_check_string = "\n".join(data_check_arr)

    # secret_key = SHA256(bot_token)
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).digest()
    computed_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_hash, received_hash)



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



@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    """
    Serves the Telegram login HTML page from login.html
    in the same directory as main.py.
    """
    try:
        html = LOGIN_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.error("login.html not found at %s", LOGIN_HTML_PATH)
        raise HTTPException(status_code=500, detail="login.html not found")

    # Inject bot name into the placeholder
    html = html.replace("__TELEGRAM_BOT_NAME__", TELEGRAM_BOT_NAME or "")
    return HTMLResponse(content=html)



@app.post("/auth/telegram")
async def telegram_auth(user: dict):
    """
    Receives Telegram Login Widget user object as JSON,
    verifies it, and returns a simple success payload.

    This is where you'd normally create a session/JWT.
    """
    if not verify_telegram_auth(user):
        raise HTTPException(status_code=400, detail="Invalid Telegram login")

    public_user = {
        "id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "photo_url": user.get("photo_url"),
    }

    # TODO: create session / JWT / Firestore record here
    return JSONResponse({"ok": True, "user": public_user})


@app.post("/auth/logout")
async def logout():
    """
    Logs the user out from the app perspective.

    Right now we don't persist any server-side session,
    so this simply returns ok=True.

    If you later switch to cookie-based or DB-backed
    sessions, you can clear them here.
    """
    # Example for future cookie-based sessions:
    # response = JSONResponse({"ok": True})
    # response.delete_cookie("session")
    # return response

    return JSONResponse({"ok": True})



# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

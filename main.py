# main.py

import os
import re
import logging
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

import scrape
import gpt
import gtranslate

from pathlib import Path
import hmac
import hashlib

import user as user_db
import session as session_db  # <-- new import for session handling


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
EXT_LOGIN_HTML_PATH = BASE_DIR / "ext_login.html"

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


# ---------------------------
# Web login + session key
# ---------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    """
    Serves the Telechan login HTML page from login.html
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
async def telegram_auth(payload: dict, request: Request):
    """
    Expects JSON:
      {
        "user": { ...Telegram login payload... },
        "ga":   { ...GA context... }
      }
    Verifies Telegram data, upserts user in Firestore,
    creates a web session, and returns public user info + web_session_key.
    """
    tg_user = payload.get("user") or {}
    ga_ctx = payload.get("ga") or {}

    if not verify_telegram_auth(tg_user):
        raise HTTPException(status_code=400, detail="Invalid Telegram login")

    try:
        stored_user = user_db.create_or_update_user_from_telegram(
            tg_payload=tg_user,
            ga_ctx=ga_ctx,
            user_agent=request.headers.get("user-agent"),
            source="telegram_widget",
        )

        # Enrich GA context with backend-seen IP for session
        client_ip = request.client.host if request.client else None
        if client_ip:
            ga_ctx = dict(ga_ctx)  # shallow copy
            ga_ctx.setdefault("ip", client_ip)

        # Create a session for the web app
        web_session_key = session_db.create_session_for_user(
            telegram_id=stored_user.get("telegram_id"),
            ttl_hours=24,
            source="web_app",  # stored in front_end field
            user_agent=request.headers.get("user-agent"),
            ga_ctx=ga_ctx,
        )
    except Exception as e:
        logger.exception("Error while creating/updating user or session in Firestore")
        raise HTTPException(status_code=500, detail=f"Firestore error: {e}")

    public_user = {
        "id": stored_user.get("telegram_id"),
        "username": stored_user.get("username"),
        "first_name": stored_user.get("first_name"),
        "last_name": stored_user.get("last_name"),
        "photo_url": stored_user.get("photo_url"),
        "login_count": stored_user.get("login_count"),
        "user_type": stored_user.get("user_type"),
        "restricted": stored_user.get("restricted"),
        "is_admin": stored_user.get("is_admin"),
    }

    return JSONResponse(
        {
            "ok": True,
            "user": public_user,
            "web_session_key": web_session_key,
        }
    )


@app.post("/auth/session/ext")
async def create_ext_session(payload: dict, request: Request):
    """
    Called from login.html when user clicks "Get Session Key".

    Expects:
      {
        "web_session_key": "...",
        "ga": { ... optional GA context ... }
      }

    Uses the web session to verify the user, then creates a new
    extension pairing session (front_end=None initially), and
    returns that session_key.
    """
    web_session_key = (payload or {}).get("web_session_key")
    ga_ctx = (payload or {}).get("ga") or {}

    if not web_session_key:
        raise HTTPException(status_code=400, detail="web_session_key is required")

    web_session = session_db.resolve_session_key(web_session_key)
    if not web_session:
        raise HTTPException(
            status_code=401,
            detail="Web session invalid or expired. Please log in again.",
        )

    telegram_id = web_session.get("telegram_id")
    if not telegram_id:
        raise HTTPException(status_code=500, detail="Web session missing telegram_id")

    # Add IP from backend for this new session
    client_ip = request.client.host if request.client else None
    if client_ip:
        ga_ctx = dict(ga_ctx)
        ga_ctx.setdefault("ip", client_ip)

    try:
        ext_session_key = session_db.create_session_for_user(
            telegram_id=telegram_id,
            ttl_hours=24,
            source=None,  # pairing session; will become "extension" when used
            user_agent=request.headers.get("user-agent"),
            ga_ctx=ga_ctx,
        )
    except Exception as e:
        logger.exception("Error while creating extension session")
        raise HTTPException(status_code=500, detail=f"Firestore error: {e}")

    return JSONResponse({"ok": True, "session_key": ext_session_key})


@app.post("/auth/logout")
async def logout(payload: Optional[dict] = Body(default=None)):
    """
    Logs the user out from the app perspective.

    If a session_key is provided, invalidate that session.
    """
    session_key = (payload or {}).get("session_key")
    if session_key:
        session_db.invalidate_session(session_key, reason="logout")

    return JSONResponse({"ok": True})


# ---------------------------
# Extension login via session key
# ---------------------------

@app.get("/ext_login", response_class=HTMLResponse)
async def ext_login_page() -> HTMLResponse:
    """
    Serves the Telechan extension login HTML page
    from ext_login.html in the same directory as main.py.
    """
    try:
        html = EXT_LOGIN_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("ext_login.html not found at %s", EXT_LOGIN_HTML_PATH)
        raise HTTPException(status_code=500, detail="ext_login.html not found")

    # Inject bot name into placeholder if present (for legacy / future flows)
    html = html.replace("__TELEGRAM_BOT_NAME__", TELEGRAM_BOT_NAME or "")
    return HTMLResponse(content=html)


@app.post("/auth/ext/session")
async def ext_session_auth(payload: dict, request: Request):
    """
    Validates a session_key created from the web app and
    marks it as used by the extension.
    """
    session_key = (payload or {}).get("session_key")
    if not session_key:
        raise HTTPException(status_code=400, detail="Missing session_key")

    session = session_db.resolve_session_key(session_key)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session_key")

    telegram_id = session.get("telegram_id")
    if not telegram_id:
        raise HTTPException(status_code=500, detail="Session missing telegram_id")

    user = user_db.get_user_by_id(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found for this session")

    # mark as extension session
    session_db.mark_session_used_by_extension(session_key)

    # prepare expiry as ISO string for JSON
    expires_at = session.get("expires_at")
    expires_at_str = None
    if isinstance(expires_at, datetime):
        expires_at_str = expires_at.isoformat()
    elif isinstance(expires_at, str):
        expires_at_str = expires_at

    public_user = {
        "id": user.get("telegram_id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "photo_url": user.get("photo_url"),
        "login_count": user.get("login_count"),
        "user_type": user.get("user_type"),
        "restricted": user.get("restricted"),
        "is_admin": user.get("is_admin"),
    }

    session_info = {
        "session_key": session_key,
        "expires_at": expires_at_str,
    }

    return JSONResponse({"ok": True, "user": public_user, "session": session_info})



# ---------------------------
# Legacy / optional: direct Telegram login for extension
# ---------------------------

@app.post("/auth/telegram/ext")
async def telegram_auth_ext(payload: dict, request: Request):
    """
    Similar to /auth/telegram but used by a possible direct
    Telegram login flow from the Chrome extension.

    Expects JSON:
      {
          "user": { ...Telegram login payload... },
          "ga":   { ...GA context... }
      }
    """
    tg_user = payload.get("user") or {}
    ga_ctx = payload.get("ga") or {}

    if not verify_telegram_auth(tg_user):
        raise HTTPException(status_code=400, detail="Invalid Telegram login")

    try:
        stored_user = user_db.create_or_update_user_from_telegram(
            tg_payload=tg_user,
            ga_ctx=ga_ctx,
            user_agent=request.headers.get("user-agent"),
            source="telechan_ext",
        )
    except Exception as e:
        logger.exception("Error while creating/updating user in Firestore (ext)")
        raise HTTPException(status_code=500, detail=f"Firestore error: {e}")

    public_user = {
        "id": stored_user.get("telegram_id"),
        "username": stored_user.get("username"),
        "first_name": stored_user.get("first_name"),
        "last_name": stored_user.get("last_name"),
        "photo_url": stored_user.get("photo_url"),
        "login_count": stored_user.get("login_count"),
        "user_type": stored_user.get("user_type"),
        "restricted": stored_user.get("restricted"),
        "is_admin": stored_user.get("is_admin"),
    }

    return JSONResponse({"ok": True, "user": public_user})


# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

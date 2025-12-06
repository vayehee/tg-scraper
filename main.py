# main.py

import os
import re
import hmac
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

import scrape
import gpt
import gtranslate
import user as user_db
import session as session_db


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

WEB_SESSION_COOKIE = "telechan_web_session"
EXT_SESSION_COOKIE = "telechan_ext_session"

WEB_SESSION_TTL_HOURS = 24 * 7   # ~7 days for web app
EXT_SESSION_TTL_HOURS = 24       # 24h for extension

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
    Verifies Telegram data, upserts user in Firestore, creates a web session,
    sets a cookie, and returns public user info + web_session_key.
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
    except Exception as e:
        logger.exception("Error while creating/updating user in Firestore")
        raise HTTPException(status_code=500, detail=f"Firestore error: {e}")

    # Create a web_app session for this user
    web_session = session_db.create_session_for_user(
        telegram_id=stored_user.get("telegram_id"),
        source="web_app",
        user_agent=request.headers.get("user-agent"),
        ga_ctx=ga_ctx,
        ttl_hours=WEB_SESSION_TTL_HOURS,
    )

    expires_at = web_session.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_str = expires_at.isoformat()
    else:
        expires_at_str = str(expires_at) if expires_at else None

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

    body = {
        "ok": True,
        "user": public_user,
        "web_session_key": web_session["session_key"],
        "session": {
            "session_key": web_session["session_key"],
            "expires_at": expires_at_str,
        },
    }

    response = JSONResponse(body)
    response.set_cookie(
        WEB_SESSION_COOKIE,
        web_session["session_key"],
        max_age=WEB_SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


@app.post("/auth/logout")
async def logout(payload: Optional[dict] = Body(default=None)):
    """
    Logs the user out from the app perspective.

    If a session_key is provided, invalidate that session.
    Always clears both web and extension cookies.
    """
    session_key = (payload or {}).get("session_key")
    if session_key:
        try:
            session_db.invalidate_session(session_key, reason="logout")
        except Exception:
            logger.exception("Failed to invalidate session_key %s", session_key)

    response = JSONResponse({"ok": True})
    response.delete_cookie(WEB_SESSION_COOKIE)
    response.delete_cookie(EXT_SESSION_COOKIE)
    return response


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
    marks it as used by the extension. Sets a 24h cookie.

    Expects JSON:
      { "session_key": "...", "ga": { ... }? }
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

    expires_at = session.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_str = expires_at.isoformat()
    else:
        expires_at_str = str(expires_at) if expires_at else None

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

    response = JSONResponse({"ok": True, "user": public_user, "session": session_info})
    response.set_cookie(
        EXT_SESSION_COOKIE,
        session_key,
        max_age=EXT_SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


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
# "Who am I" endpoints (web + extension)
# ---------------------------

@app.get("/auth/me")
async def auth_me(request: Request):
    session_key = request.cookies.get(WEB_SESSION_COOKIE)
    if not session_key:
        raise HTTPException(status_code=401, detail="No session cookie")

    session = session_db.resolve_session_key(session_key)
    if not session:
        resp = JSONResponse({"ok": False, "detail": "Session invalid"})
        resp.delete_cookie(WEB_SESSION_COOKIE)
        resp.status_code = 401
        return resp

    user = user_db.get_user_by_id(session.get("telegram_id"))
    if not user:
        resp = JSONResponse({"ok": False, "detail": "User not found"})
        resp.delete_cookie(WEB_SESSION_COOKIE)
        resp.status_code = 401
        return resp

    expires_at = session.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_str = expires_at.isoformat()
    else:
        expires_at_str = str(expires_at) if expires_at else None

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

    return JSONResponse({
        "ok": True,
        "user": public_user,
        "session": {
            "session_key": session_key,
            "expires_at": expires_at_str,
        },
    })


@app.get("/auth/ext/me")
async def auth_ext_me(request: Request):
    session_key = request.cookies.get(EXT_SESSION_COOKIE)
    if not session_key:
        raise HTTPException(status_code=401, detail="No extension session cookie")

    session = session_db.resolve_session_key(session_key)
    if not session:
        resp = JSONResponse({"ok": False, "detail": "Session invalid"})
        resp.delete_cookie(EXT_SESSION_COOKIE)
        resp.status_code = 401
        return resp

    user = user_db.get_user_by_id(session.get("telegram_id"))
    if not user:
        resp = JSONResponse({"ok": False, "detail": "User not found"})
        resp.delete_cookie(EXT_SESSION_COOKIE)
        resp.status_code = 401
        return resp

    expires_at = session.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_str = expires_at.isoformat()
    else:
        expires_at_str = str(expires_at) if expires_at else None

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

    return JSONResponse({
        "ok": True,
        "user": public_user,
        "session": {
            "session_key": session_key,
            "expires_at": expires_at_str,
        },
    })


# ---------------------------
# Local dev entry (optional)
# ---------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

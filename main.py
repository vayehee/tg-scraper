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

logger = logging.getLogger("tg-scraper")
logging.basicConfig(level=logging.INFO)
logger.info("tg-scraper starting; ready to listen")

BASE_DIR = Path(__file__).resolve().parent
LOGIN_HTML_PATH = BASE_DIR / "login.html"
EXT_LOGIN_HTML_PATH = BASE_DIR / "ext_login.html"

TELEGRAM_BOT_NAME = os.getenv("TELEGRAM_BOT_NAME", "YOUR_BOT_NAME_HERE")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

WEB_SESSION_COOKIE = "telechan_web_session"
EXT_SESSION_COOKIE = "telechan_ext_session"

WEB_SESSION_TTL_HOURS = 24 * 7
EXT_SESSION_TTL_HOURS = 24

USERNAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def verify_telegram_auth(payload: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is not set.")
        return False

    data = dict(payload)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return False

    check_str = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).digest()
    computed_hash = hmac.new(secret_key, check_str.encode("utf-8"), hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed_hash, received_hash)


app = FastAPI(title="Telegram Scraper", version="1.2.0")


@app.get("/", response_model=None, tags=["health", "chan"])
async def root(chan: Optional[str] = Query(None)):
    if chan is None:
        return {"status": "ok", "service": "tg-scraper"}
    if not USERNAME_REGEX.match(chan):
        raise HTTPException(status_code=400, detail="Invalid channel username.")
    channel = await scrape.CHANNEL(chan)
    return channel


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    try:
        html = LOGIN_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.error("login.html not found.")
        raise HTTPException(status_code=500, detail="login.html not found")
    html = html.replace("__TELEGRAM_BOT_NAME__", TELEGRAM_BOT_NAME or "")
    return HTMLResponse(content=html)


@app.post("/auth/session/login")
async def session_login(payload: dict, request: Request):
    user_data = payload.get("user")
    if not user_data or not verify_telegram_auth(user_data):
        raise HTTPException(status_code=400, detail="Invalid Telegram login")

    user = user_db.create_or_update_user_from_telegram(
        tg_payload=user_data,
        ga_ctx=None,
        user_agent=request.headers.get("user-agent"),
        source="web_app",
    )

    session = session_db.create_session_for_user(
        telegram_id=user["telegram_id"],
        source="web_app",
        user_agent=request.headers.get("user-agent"),
        ga_ctx=None,
        ttl_hours=WEB_SESSION_TTL_HOURS,
    )

    response = JSONResponse({"ok": True, "session_key": session["session_key"]})
    response.set_cookie(
        WEB_SESSION_COOKIE,
        session["session_key"],
        max_age=WEB_SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


@app.post("/auth/session/key")
async def session_key(request: Request):
    session_key = request.cookies.get(WEB_SESSION_COOKIE)
    if not session_key:
        raise HTTPException(status_code=401, detail="No session cookie")

    session = session_db.resolve_session_key(session_key)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found")

    new_session = session_db.create_session_for_user(
        telegram_id=session["telegram_id"],
        front_end=None,
        user_agent=request.headers.get("user-agent"),
        ga_ctx=None,
        ttl_hours=EXT_SESSION_TTL_HOURS,
    )
    return {"ok": True, "session_key": new_session["session_key"]}


@app.post("/auth/session/logout")
async def session_logout(request: Request):
    session_key = request.cookies.get(WEB_SESSION_COOKIE)
    if session_key:
        try:
            session_db.invalidate_session(session_key, reason="logout")
        except Exception:
            logger.exception("Failed to invalidate session.")

    response = JSONResponse({"ok": True})
    response.delete_cookie(WEB_SESSION_COOKIE)
    return response


@app.get("/ext_login", response_class=HTMLResponse)
async def ext_login_page() -> HTMLResponse:
    try:
        html = EXT_LOGIN_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.error("ext_login.html not found.")
        raise HTTPException(status_code=500, detail="ext_login.html not found")
    html = html.replace("__TELEGRAM_BOT_NAME__", TELEGRAM_BOT_NAME or "")
    return HTMLResponse(content=html)


@app.post("/auth/session/ext")
async def ext_session_auth(payload: dict, request: Request):
    session_key = payload.get("session_key")
    if not session_key:
        raise HTTPException(status_code=400, detail="Missing session_key")

    session = session_db.resolve_session_key(session_key)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session_key")

    user = user_db.get_user_by_id(session["telegram_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    session_db.mark_session_used_by_extension(session_key)

    expires_at = session.get("expires_at")
    session_info = {
        "session_key": session_key,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at),
    }

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


@app.post("/auth/telegram/ext")
async def telegram_auth_ext(payload: dict, request: Request):
    tg_user = payload.get("user") or {}
    ga_ctx = payload.get("ga") or {}

    if not verify_telegram_auth(tg_user):
        raise HTTPException(status_code=400, detail="Invalid Telegram login")

    stored_user = user_db.create_or_update_user_from_telegram(
        tg_payload=tg_user,
        ga_ctx=ga_ctx,
        user_agent=request.headers.get("user-agent"),
        source="telechan_ext",
    )

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
    return JSONResponse({
        "ok": True,
        "user": user,
        "session": {
            "session_key": session_key,
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at),
        },
    })


@app.get("/auth/me/ext")
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
    return JSONResponse({
        "ok": True,
        "user": user,
        "session": {
            "session_key": session_key,
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at),
        },
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

# session.py

import os
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
import secrets

from google.cloud import firestore

logger = logging.getLogger(__name__)

SESSION_SCHEMA: Dict[str, Any] = {
    "session_key": None,
    "telegram_id": None,

    # Lifecycle
    "created_at": None,
    "expires_at": None,
    "valid": True,

    # Context
    "front_end": None,
    "user_agent": None,

    # GA-related
    "ga_client_id": None,
    "ga_session_id": None,
    "ga_session_number": None,

    # Geo & language
    "ip": None,
    "country": None,
    "region": None,
    "city": None,
    "address": None,
    "continent": None,
    "language": None,
    "browser_language": None,
}

_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
_db: Optional[firestore.Client] = None


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        if _PROJECT_ID:
            logger.info("Initializing Firestore client for project %s", _PROJECT_ID)
            _db = firestore.Client(project=_PROJECT_ID)
        else:
            logger.info("Initializing Firestore client with default project")
            _db = firestore.Client()
    return _db


def sessions_col() -> firestore.CollectionReference:
    return get_db().collection("sessions")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def apply_session_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    full = SESSION_SCHEMA.copy()
    for k, v in data.items():
        if k in SESSION_SCHEMA:
            full[k] = v
    return full


# --------------------------------------------------------------------------
# PUBLIC API
# --------------------------------------------------------------------------

async def create_session_for_user(
    telegram_id: str,
    front_end: Optional[str],
    user_agent: Optional[str],
    ga_ctx: Optional[Dict[str, Any]] = None,
    ttl_hours: int = 24,
    ip: Optional[str] = None,
) -> Dict[str, Any]:

    ga_ctx = ga_ctx or {}

    # NEW REQUIRED LOGIC:
    # Only one valid session per (telegram_id, front_end)
    if front_end is not None:
        q = (
            sessions_col()
            .where("telegram_id", "==", str(telegram_id))
            .where("front_end", "==", front_end)
            .where("valid", "==", True)
        )
        for doc in q.stream():
            doc.reference.set({"valid": False}, merge=True)
            logger.info("Invalidated previous session %s", doc.id)

    # Create new session
    session_key = secrets.token_urlsafe(32)
    now = _now_utc()
    expires_at = now + timedelta(hours=ttl_hours)

    base_data: Dict[str, Any] = {
        "session_key": session_key,
        "telegram_id": str(telegram_id),

        "created_at": now,
        "expires_at": expires_at,
        "valid": True,

        "front_end": front_end,
        "user_agent": user_agent,

        "ga_client_id": ga_ctx.get("client_id"),
        "ga_session_id": ga_ctx.get("session_id"),
        "ga_session_number": ga_ctx.get("session_number"),

        "ip": ip,
        "country": ga_ctx.get("country"),
        "region": ga_ctx.get("region"),
        "city": ga_ctx.get("city"),
        "address": ga_ctx.get("address"),
        "continent": ga_ctx.get("continent"),
        "language": ga_ctx.get("language"),
        "browser_language": ga_ctx.get("browser_language"),
    }

    stored = apply_session_schema(base_data)
    sessions_col().document(session_key).set(stored)

    logger.info(
        "Created session %s for telegram_id=%s (front_end=%s, ttl=%sh)",
        session_key, telegram_id, front_end, ttl_hours
    )
    return stored


def resolve_session_key(session_key: str) -> Optional[Dict[str, Any]]:
    if not session_key:
        return None

    doc = sessions_col().document(session_key).get()
    if not doc.exists:
        return None

    data = apply_session_schema(doc.to_dict() or {})
    if not data.get("valid", True):
        return None

    expires_at = data.get("expires_at")
    now = _now_utc()

    if isinstance(expires_at, datetime) and expires_at < now:
        sessions_col().document(session_key).set({"valid": False}, merge=True)
        logger.info("Session %s expired", session_key)
        return None

    return data


def invalidate_session(session_key: str, reason: Optional[str] = None) -> None:
    if not session_key:
        return

    doc_ref = sessions_col().document(session_key)
    snap = doc_ref.get()
    if not snap.exists:
        return

    logger.info(
        "Invalidating session %s%s",
        session_key,
        f" (reason={reason})" if reason else "",
    )
    doc_ref.set({"valid": False}, merge=True)


def mark_session_used_by_extension(session_key: str) -> Optional[Dict[str, Any]]:
    if not session_key:
        return None

    doc_ref = sessions_col().document(session_key)
    snap = doc_ref.get()
    if not snap.exists:
        return None

    data = apply_session_schema(snap.to_dict() or {})
    if not data.get("valid", True):
        return data

    data["front_end"] = "extension"
    doc_ref.set({"front_end": "extension"}, merge=True)

    logger.info("Session %s marked as used by extension", session_key)
    return data

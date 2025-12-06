# session.py

import os
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
import secrets

from google.cloud import firestore

logger = logging.getLogger(__name__)

SESSION_SCHEMA: Dict[str, Any] = {
    "session_key": None,          # str: public-facing key user copies/pastes
    "telegram_id": None,          # str: user this session belongs to

    # Lifecycle
    "created_at": None,           # datetime (UTC) in Firestore
    "expires_at": None,           # datetime (UTC) in Firestore
    "valid": True,                # bool: true when created, false when invalidated/expired

    # Context
    # e.g. "web_app", "extension", or None (for pairing session before ext uses it)
    "front_end": None,            # str | None
    "user_agent": None,           # str | None

    # GA-related (session-level analytics context)
    "ga_client_id": None,
    "ga_session_id": None,
    "ga_session_number": None,

    # Geo & language (per-session context)
    "ip": None,                   # str | None
    "country": None,
    "region": None,
    "city": None,
    "address": None,
    "continent": None,
    "language": None,
    "browser_language": None,
}

# --------------------------------------------------------------------------
# Firestore plumbing
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def apply_session_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay SESSION_SCHEMA defaults with provided data (no mutation)."""
    full = SESSION_SCHEMA.copy()
    for k, v in data.items():
        if k in SESSION_SCHEMA:
            full[k] = v
    return full


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def create_session_for_user(
    telegram_id: str,
    source: Optional[str],
    user_agent: Optional[str],
    ga_ctx: Optional[Dict[str, Any]] = None,
    ttl_hours: int = 24,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new session:

    - `source="web_app"` → normal web session
    - `source="extension"` → direct extension session (if ever used)
    - `source=None` → pairing session (key to be pasted into ext_login)

    Document ID == session_key.
    """
    ga_ctx = ga_ctx or {}

    session_key = secrets.token_urlsafe(32)
    now = _now_utc()
    expires_at = now + timedelta(hours=ttl_hours)

    base_data: Dict[str, Any] = {
        "session_key": session_key,
        "telegram_id": str(telegram_id),

        "created_at": now,
        "expires_at": expires_at,
        "valid": True,

        "front_end": source,  # None for pairing; "web_app" / "extension" otherwise
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

    doc_ref = sessions_col().document(session_key)
    stored = apply_session_schema(base_data)
    doc_ref.set(stored)
    logger.info(
        "Created session %s for telegram_id=%s (front_end=%s, ttl=%sh)",
        session_key, telegram_id, source, ttl_hours
    )
    return stored


def resolve_session_key(session_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a session by key.

    - Returns None if not found, invalid, or expired.
    - Automatically marks expired sessions as valid=False.
    """
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
        # auto-expire
        data["valid"] = False
        sessions_col().document(session_key).set({"valid": False}, merge=True)
        logger.info("Session %s expired", session_key)
        return None

    return data


def invalidate_session(session_key: str, reason: Optional[str] = None) -> None:
    """
    Mark a session as invalid (e.g. on logout).
    `reason` is only logged, not stored.
    """
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
    """
    Set `front_end="extension"` to indicate the key has been claimed by ext_login.

    Called AFTER resolve_session_key has already ensured:
    - session exists
    - session.valid == True
    - session not expired
    """
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

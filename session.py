import os
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from google.cloud import firestore  # ensure google-cloud-firestore is in requirements.txt

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Session schema
# -----------------------------------------------------------------------------

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
    "ip": None,                   # str | None: IP as seen from backend / frontend hint
    "country": None,
    "region": None,
    "city": None,
    "address": None,
    "continent": None,
    "language": None,
    "browser_language": None,
}

# -----------------------------------------------------------------------------
# Firestore client helpers
# -----------------------------------------------------------------------------

_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
_db: Optional[firestore.Client] = None


def get_db() -> firestore.Client:
    """
    Return a singleton Firestore client, using FIRESTORE_PROJECT_ID or
    GOOGLE_CLOUD_PROJECT if provided.
    """
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
    """
    Convenience accessor for the 'sessions' collection.
    """
    return get_db().collection("sessions")


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def apply_session_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure all SESSION_SCHEMA keys exist in the returned dict.
    Does NOT mutate the input.
    """
    full = SESSION_SCHEMA.copy()
    full.update({k: v for k, v in data.items() if k in SESSION_SCHEMA})
    return full


# -----------------------------------------------------------------------------
# Public operations
# -----------------------------------------------------------------------------

def create_session_for_user(
    telegram_id: str,
    ttl_hours: int = 24,
    source: Optional[str] = None,         # kept name for backward compatibility
    user_agent: Optional[str] = None,
    ga_ctx: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Create a new session record for the given telegram_id.
    Returns the session_key (used by the user / extension).

    Session is valid until expires_at (now + ttl_hours), unless invalidated.

    Conventions:
      - Web app login:   source="web_app"  -> front_end="web_app"
      - Ext pairing key: source=None       -> front_end=None (updated on ext use)
    """
    import secrets

    if not telegram_id:
        raise ValueError("telegram_id is required to create a session")

    ga_ctx = ga_ctx or {}

    now = _now_utc()
    expires_at = now + timedelta(hours=ttl_hours)

    # High-entropy, URL-safe key (about 43 chars, 256 bits of entropy)
    session_key = secrets.token_urlsafe(32)

    base_data: Dict[str, Any] = {
        "session_key": session_key,
        "telegram_id": str(telegram_id),

        "created_at": now,
        "expires_at": expires_at,
        "valid": True,           # always true on creation

        # Context
        "front_end": source,
        "user_agent": user_agent,

        # GA context
        "ga_client_id": ga_ctx.get("client_id"),
        "ga_session_id": ga_ctx.get("session_id"),
        "ga_session_number": ga_ctx.get("session_number"),

        # Geo & language
        "ip": ga_ctx.get("ip"),
        "country": ga_ctx.get("country"),
        "region": ga_ctx.get("region"),
        "city": ga_ctx.get("city"),
        "address": ga_ctx.get("address"),
        "continent": ga_ctx.get("continent"),
        "language": ga_ctx.get("language") or ga_ctx.get("browser_language"),
        "browser_language": ga_ctx.get("browser_language"),
    }

    doc = apply_session_schema(base_data)
    sessions_col().document(session_key).set(doc)
    logger.info("Created session for user %s", telegram_id)
    return session_key


def resolve_session_key(session_key: str) -> Optional[Dict[str, Any]]:
    """
    Look up a session by its key and check that it is valid
    (exists, valid=True, not expired).

    If the session exists but has expired, this will mark valid=False
    and return None.

    Returns the session dict (with schema applied) or None if invalid.
    """
    if not session_key:
        return None

    snap = sessions_col().document(session_key).get()
    if not snap.exists:
        return None

    data = apply_session_schema(snap.to_dict() or {})

    # Already invalidated
    if not data.get("valid", False):
        return None

    expires_at = data.get("expires_at")
    if isinstance(expires_at, datetime):
        now = _now_utc()
        if expires_at < now:
            # Auto-invalidate on expiry
            try:
                sessions_col().document(session_key).update(
                    {
                        "valid": False,
                    }
                )
                logger.info("Session %s expired and marked invalid", session_key)
            except Exception as e:
                logger.warning(
                    "Failed to mark session %s invalid on expiry: %s",
                    session_key,
                    e,
                )
            return None

    return data


def mark_session_used_by_extension(session_key: str) -> None:
    """
    Mark a session as being used by the extension.
    This will set front_end="extension".
    """
    try:
        sessions_col().document(session_key).update(
            {
                "front_end": "extension",
            }
        )
        logger.info("Session %s marked as front_end=extension", session_key)
    except Exception as e:
        logger.warning(
            "Failed to mark session %s front_end=extension: %s",
            session_key,
            e,
        )


def invalidate_session(session_key: str, reason: Optional[str] = None) -> None:
    """
    Mark a given session as invalid (valid=False).
    The 'reason' is logged but not stored in the document.
    """
    try:
        sessions_col().document(session_key).update({"valid": False})
        logger.info("Session %s invalidated (reason=%s)", session_key, reason)
    except Exception as e:
        logger.warning("Failed to invalidate session %s: %s", session_key, e)


def get_session_by_key(session_key: str) -> Optional[Dict[str, Any]]:
    """
    Debug/helper: fetch a session by key without validity checks.
    """
    if not session_key:
        return None

    snap = sessions_col().document(session_key).get()
    if not snap.exists:
        return None

    return apply_session_schema(snap.to_dict() or {})

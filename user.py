import os
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone

from google.cloud import firestore  # ensure google-cloud-firestore in requirements.txt

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# User schema
# -----------------------------------------------------------------------------

USER_SCHEMA: Dict[str, Any] = {
    # Telegram identity
    "telegram_id": None,        # str: Telegram user id (we'll also use as doc id)
    "username": None,           # str | None: @username
    "first_name": None,         # str | None
    "last_name": None,          # str | None
    "photo_url": None,          # str | None
    "admin_of": [],             # List[str]: ids of channels this user is admin of

    # App flags
    # Suggested values: "basic" | "advertiser" | "monetiser" | "agent" | "admin"
    "user_type": "basic",       # str: user type
    "customer_id": None,        # str | None: customer id for billing purposes
    "restricted": False,        # bool: can be used to soft-disable a user
    "is_admin": False,          # bool: manual flag for privileged users

    # Login bookkeeping (user-level summary)
    "created_at": None,         # ISO8601 str (UTC)
    "updated_at": None,         # ISO8601 str (UTC)
    "last_login_at": None,      # ISO8601 str (UTC)
    "last_login_source": None,  # e.g. "telegram_widget"
    "last_login_user_agent": None,
    "login_count": 0,           # int
}

# -----------------------------------------------------------------------------
# Firestore client helpers
# -----------------------------------------------------------------------------

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


def users_col() -> firestore.CollectionReference:
    return get_db().collection("users")


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_user_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure all USER_SCHEMA keys exist in the returned dict.
    Also makes sure admin_of is a fresh list instance.
    """
    full = USER_SCHEMA.copy()
    full.update({k: v for k, v in data.items() if k in USER_SCHEMA})
    # ensure a fresh list, not the shared default
    full["admin_of"] = list(full.get("admin_of") or [])
    return full


# -----------------------------------------------------------------------------
# Public operations
# -----------------------------------------------------------------------------

def create_or_update_user_from_telegram(
    tg_payload: Dict[str, Any],
    ga_ctx: Optional[Dict[str, Any]] = None,
    user_agent: Optional[str] = None,
    source: str = "telegram_widget",
) -> Dict[str, Any]:
    """
    Upsert a user document in Firestore based on Telegram Login payload.

    - Uses Telegram user id as Firestore document id.
    - On first login: creates a new doc with login_count=1.
    - On subsequent logins: updates profile, bumps login_count and timestamps.

    GA / geo / language context is now session-level and handled in session.py.
    """
    if "id" not in tg_payload:
        raise ValueError("Telegram payload is missing 'id' field")

    # kept for interface compatibility; not used at user level anymore
    ga_ctx = ga_ctx or {}

    doc_id = str(tg_payload["id"])
    col = users_col()
    doc_ref = col.document(doc_id)
    snap = doc_ref.get()

    now = _now_iso()

    base_data: Dict[str, Any] = {
        "telegram_id": doc_id,
        "username": tg_payload.get("username"),
        "first_name": tg_payload.get("first_name"),
        "last_name": tg_payload.get("last_name"),
        "photo_url": tg_payload.get("photo_url"),

        "last_login_at": now,
        "last_login_source": source,
        "last_login_user_agent": user_agent,
    }

    if snap.exists:
        existing = snap.to_dict() or {}
        login_count = int(existing.get("login_count", 0)) + 1

        updated = existing.copy()
        updated.update(base_data)
        updated["login_count"] = login_count
        updated["updated_at"] = now

        final = apply_user_schema(updated)
        doc_ref.set(final)
        logger.info("Updated existing user %s (login_count=%s)", doc_id, login_count)
    else:
        base_data["created_at"] = now
        base_data["updated_at"] = now
        base_data["login_count"] = 1

        # new users default flags (respect schema defaults)
        base_data.setdefault("user_type", USER_SCHEMA["user_type"])
        base_data.setdefault("customer_id", USER_SCHEMA["customer_id"])
        base_data.setdefault("restricted", USER_SCHEMA["restricted"])
        base_data.setdefault("is_admin", USER_SCHEMA["is_admin"])
        base_data.setdefault("admin_of", USER_SCHEMA["admin_of"])

        final = apply_user_schema(base_data)
        doc_ref.set(final)
        logger.info("Created new user %s", doc_id)

    return final


def get_user_by_id(telegram_id: str) -> Optional[Dict[str, Any]]:
    doc = users_col().document(str(telegram_id)).get()
    if not doc.exists:
        return None
    return apply_user_schema(doc.to_dict() or {})

"""Helpers for retaining a small, restorable set of公众号 sessions."""

from datetime import datetime
import time


def _as_timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def is_restorable_account_session(session: dict, now: float | None = None) -> bool:
    """Only complete, unexpired credentials can occupy one of the three slots."""
    if not isinstance(session, dict):
        return False
    if not session.get("token") or not isinstance(session.get("cookies"), list) or not session["cookies"]:
        return False
    if str(session.get("token_status") or "").lower() == "expired":
        return False
    expiry = session.get("expiry") or {}
    expiry_at = _as_timestamp(expiry.get("expiry_timestamp"))
    return expiry_at > (time.time() if now is None else now)


def _retention_timestamp(session: dict) -> float:
    return max(
        _as_timestamp(session.get("last_active_at")),
        _as_timestamp(session.get("authorized_at")),
        _as_timestamp(session.get("updated_at")),
    )


def retain_recent_valid_sessions(sessions: dict, limit: int = 3, now: float | None = None) -> dict:
    """Return complete, currently valid sessions, newest authorization/use first."""
    if not isinstance(sessions, dict) or limit <= 0:
        return {}
    current_time = time.time() if now is None else now
    valid = []
    for account_id, session in sessions.items():
        account_id = str(account_id or "").strip()
        if account_id and is_restorable_account_session(session, current_time):
            valid.append((account_id, session))
    valid.sort(key=lambda item: (_retention_timestamp(item[1]), item[0]), reverse=True)
    return {account_id: dict(session) for account_id, session in valid[:limit]}

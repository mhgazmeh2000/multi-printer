"""
PrintGuard Onboarding State Manager (Phase J.9)
Tracks per-user onboarding tours completion, dismissal, and global disable preference.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.db.connection import connect

log = logging.getLogger("printguard.onboarding")

KNOWN_TOURS = [
    "dashboard_intro",
    "dashboard_builder",
    "panel_creation",
    "datasource_connect",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_user_tours_status(user_id: int) -> Dict[str, Any]:
    """Retrieve onboarding status for all standard tours for a given user."""
    status: Dict[str, bool] = {t: False for t in KNOWN_TOURS}
    dismissed_status: Dict[str, bool] = {t: False for t in KNOWN_TOURS}
    disabled_all = False

    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT tour_name, completed_at, dismissed, disabled_all FROM user_onboarding_state WHERE user_id = ?",
                (user_id,),
            ).fetchall()

            for r in rows:
                t_name = r[0]
                is_completed = bool(r[1])
                is_dismissed = bool(r[2])
                if bool(r[3]):
                    disabled_all = True

                if t_name in status:
                    status[t_name] = is_completed
                    dismissed_status[t_name] = is_dismissed
    except Exception as exc:
        log.warning("Could not load onboarding status for user %s: %s", user_id, exc)

    return {
        "disabled_all": disabled_all,
        "completed": status,
        "dismissed": dismissed_status,
    }


def set_tour_status(
    user_id: int,
    tour_name: str,
    completed: bool = True,
    dismissed: bool = False,
    disabled_all: Optional[bool] = None,
) -> bool:
    """Save or update the status of a specific onboarding tour for a user."""
    completed_at = _utc_now_iso() if completed else None
    dismissed_val = 1 if dismissed else 0
    disabled_val = 1 if disabled_all else 0

    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT id, disabled_all FROM user_onboarding_state WHERE user_id = ? AND tour_name = ?",
                (user_id, tour_name),
            ).fetchone()

            if disabled_all is None and row:
                disabled_val = row[1]

            if row:
                conn.execute(
                    """
                    UPDATE user_onboarding_state
                    SET completed_at = ?, dismissed = ?, disabled_all = ?
                    WHERE user_id = ? AND tour_name = ?
                    """,
                    (completed_at, dismissed_val, disabled_val, user_id, tour_name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_onboarding_state (user_id, tour_name, completed_at, dismissed, disabled_all)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, tour_name, completed_at, dismissed_val, disabled_val),
                )
            conn.commit()
            return True
    except Exception as exc:
        log.error("Failed to update onboarding status for user %s, tour %s: %s", user_id, tour_name, exc)
        return False


def set_user_onboarding_disabled(user_id: int, disabled: bool) -> bool:
    """Globally enable or disable all tours for a user."""
    disabled_val = 1 if disabled else 0
    try:
        with connect() as conn:
            conn.execute(
                "UPDATE user_onboarding_state SET disabled_all = ? WHERE user_id = ?",
                (disabled_val, user_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO user_onboarding_state (user_id, tour_name, completed_at, dismissed, disabled_all)
                VALUES (?, '_global_', NULL, 0, ?)
                """,
                (user_id, disabled_val),
            )
            conn.commit()
            return True
    except Exception as exc:
        log.error("Failed to set onboarding disabled=%s for user %s: %s", disabled, user_id, exc)
        return False


def reset_user_tours(user_id: int, tour_name: Optional[str] = None) -> bool:
    """Reset onboarding tour progress so the user can re-run tours."""
    try:
        with connect() as conn:
            if tour_name:
                conn.execute(
                    "DELETE FROM user_onboarding_state WHERE user_id = ? AND tour_name = ?",
                    (user_id, tour_name),
                )
            else:
                conn.execute(
                    "DELETE FROM user_onboarding_state WHERE user_id = ?",
                    (user_id,),
                )
            conn.commit()
            return True
    except Exception as exc:
        log.error("Failed to reset onboarding tours for user %s: %s", user_id, exc)
        return False

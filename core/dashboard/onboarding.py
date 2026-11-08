"""
PrintGuard Onboarding State Manager (Phase J.9)
Tracks per-user onboarding tours completion, dismissal, and global disable preference.
Uses `user_onboarding_state` on SQLite and `meta` (`onboarding:user:<id>`) on PostgreSQL
so Alembic schema strictness (Rule E.4.5, head 0008) is preserved without extra DDL.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.db.connection import BACKEND_POSTGRESQL, connect

log = logging.getLogger("printguard.onboarding")

KNOWN_TOURS = [
    "dashboard_intro",
    "dashboard_builder",
    "panel_creation",
    "datasource_connect",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta_key(user_id: int) -> str:
    return f"onboarding:user:{int(user_id)}"


def _default_payload() -> Dict[str, Any]:
    return {
        "disabled_all": False,
        "completed": {t: False for t in KNOWN_TOURS},
        "dismissed": {t: False for t in KNOWN_TOURS},
    }


def _load_pg_state(conn: Any, user_id: int) -> Dict[str, Any]:
    state = _default_payload()
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (_meta_key(user_id),),
    ).fetchone()
    if not row or not row[0]:
        return state
    raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    if not isinstance(raw, dict):
        return state
    state["disabled_all"] = bool(raw.get("disabled_all", False))
    comp = raw.get("completed") if isinstance(raw.get("completed"), dict) else {}
    dism = raw.get("dismissed") if isinstance(raw.get("dismissed"), dict) else {}
    for t in KNOWN_TOURS:
        state["completed"][t] = bool(comp.get(t, False))
        state["dismissed"][t] = bool(dism.get(t, False))
    return state


def _save_pg_state(conn: Any, user_id: int, state: Dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = EXCLUDED.updated_at
        """,
        (_meta_key(user_id), payload, now),
    )


def get_user_tours_status(user_id: int) -> Dict[str, Any]:
    """Retrieve onboarding status for all standard tours for a given user."""
    status: Dict[str, bool] = {t: False for t in KNOWN_TOURS}
    dismissed_status: Dict[str, bool] = {t: False for t in KNOWN_TOURS}
    disabled_all = False

    try:
        with connect() as conn:
            if getattr(conn, "backend", None) == BACKEND_POSTGRESQL:
                return _load_pg_state(conn, user_id)

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
            if getattr(conn, "backend", None) == BACKEND_POSTGRESQL:
                state = _load_pg_state(conn, user_id)
                if tour_name in state["completed"]:
                    state["completed"][tour_name] = bool(completed)
                    state["dismissed"][tour_name] = bool(dismissed)
                if disabled_all is not None:
                    state["disabled_all"] = bool(disabled_all)
                _save_pg_state(conn, user_id, state)
                conn.commit()
                return True

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
            if getattr(conn, "backend", None) == BACKEND_POSTGRESQL:
                state = _load_pg_state(conn, user_id)
                state["disabled_all"] = bool(disabled)
                _save_pg_state(conn, user_id, state)
                conn.commit()
                return True

            conn.execute(
                "UPDATE user_onboarding_state SET disabled_all = ? WHERE user_id = ?",
                (disabled_val, user_id),
            )
            conn.execute(
                """
                INSERT INTO user_onboarding_state (user_id, tour_name, completed_at, dismissed, disabled_all)
                VALUES (?, '_global_', NULL, 0, ?)
                ON CONFLICT (user_id, tour_name) DO UPDATE SET
                    disabled_all = EXCLUDED.disabled_all
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
            if getattr(conn, "backend", None) == BACKEND_POSTGRESQL:
                if tour_name:
                    state = _load_pg_state(conn, user_id)
                    if tour_name in state["completed"]:
                        state["completed"][tour_name] = False
                        state["dismissed"][tour_name] = False
                    _save_pg_state(conn, user_id, state)
                else:
                    conn.execute(
                        "DELETE FROM meta WHERE key = ?",
                        (_meta_key(user_id),),
                    )
                conn.commit()
                return True

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

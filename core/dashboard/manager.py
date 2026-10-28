"""
PrintGuard Dashboard Manager (Phase J.3)
CRUD operations and role-based permissions for dashboards, panels, and data sources.
Supports Dual-Backend (PostgreSQL JSONB / SQLite TEXT) with audit logging.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("PrinterMonitor.Dashboard")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(val: Any) -> Any:
    """Safe JSON deserializer handling both string and already-parsed dict/list."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return val


def _dumps(val: Any) -> str:
    """Safe JSON serializer."""
    if val is None:
        return "{}"
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


class DashboardPermissionError(Exception):
    """Raised when an operation violates dashboard permissions."""
    pass


class DashboardNotFoundError(Exception):
    """Raised when dashboard does not exist."""
    pass


# ─── Dashboard CRUD ───────────────────────────────────────────────────────────

def create_dashboard(
    conn: Any,
    title: str,
    description: Optional[str] = None,
    layout: Optional[Dict[str, Any]] = None,
    variables: Optional[Dict[str, Any]] = None,
    owner_id: Optional[int] = None,
    org_id: str = "default",
    uid: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a new dashboard and assigns the creator as owner."""
    if not title or not title.strip():
        raise ValueError("عنوان داشبورد الزامی است.")

    dash_uid = uid or f"dash_{uuid.uuid4().hex[:10]}"
    now = _now_utc()
    layout_val = _dumps(layout or {"cols": 12, "rows": []})
    vars_val = _dumps(variables or {})

    conn.execute(
        """
        INSERT INTO dashboards (uid, title, description, layout, variables, owner_id, org_id, is_starred, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (dash_uid, title.strip(), description, layout_val, vars_val, owner_id, org_id, now, now),
    )

    # Get the inserted dashboard id
    row = conn.execute("SELECT id FROM dashboards WHERE uid = ?", (dash_uid,)).fetchone()
    dash_id = row[0] if row else None

    # If owner_id is provided, record permission as 'owner'
    if owner_id and dash_id:
        conn.execute(
            """
            INSERT INTO dashboard_permissions (dashboard_id, user_id, group_id, role, created_at)
            VALUES (?, ?, NULL, 'owner', ?)
            """,
            (dash_id, owner_id, now),
        )

    log.info(f"داشبورد جدید با شناسه {dash_uid} و عنوان '{title}' ایجاد شد.")
    return get_dashboard(conn, dash_uid)


def get_dashboard(conn: Any, uid: str) -> Optional[Dict[str, Any]]:
    """Fetches a dashboard by its UID, along with its panels."""
    row = conn.execute(
        """
        SELECT id, uid, title, description, layout, variables, owner_id, org_id, is_starred, created_at, updated_at
        FROM dashboards
        WHERE uid = ?
        """,
        (uid,),
    ).fetchone()

    if not row:
        return None

    dash_id, d_uid, title, desc, layout, variables, owner_id, org_id, is_starred, created_at, updated_at = row

    # Fetch panels for this dashboard
    panel_rows = conn.execute(
        """
        SELECT id, uid, type, title, description, query, config, grid_pos, created_at, updated_at
        FROM panels
        WHERE dashboard_id = ?
        ORDER BY id ASC
        """,
        (dash_id,),
    ).fetchall()

    panels = []
    for pr in panel_rows:
        pid, puid, ptype, ptitle, pdesc, pquery, pconfig, pgrid, pcreated, pupdated = pr
        panels.append({
            "id": pid,
            "uid": puid,
            "type": ptype,
            "title": ptitle,
            "description": pdesc,
            "query": _loads(pquery),
            "config": _loads(pconfig),
            "grid_pos": _loads(pgrid) or {"x": 0, "y": 0, "w": 6, "h": 4},
            "created_at": str(pcreated),
            "updated_at": str(pupdated),
        })

    return {
        "id": dash_id,
        "uid": d_uid,
        "title": title,
        "description": desc or "",
        "layout": _loads(layout) or {},
        "variables": _loads(variables) or {},
        "owner_id": owner_id,
        "org_id": org_id,
        "is_starred": bool(is_starred),
        "created_at": str(created_at),
        "updated_at": str(updated_at),
        "panels": panels,
    }


def list_dashboards(
    conn: Any,
    user_id: Optional[int] = None,
    user_role: Optional[str] = None,
    org_id: str = "default",
) -> List[Dict[str, Any]]:
    """Lists all dashboards accessible by the user."""
    # Admins see all dashboards
    if user_role == "admin":
        rows = conn.execute(
            """
            SELECT id, uid, title, description, owner_id, is_starred, created_at, updated_at
            FROM dashboards
            WHERE org_id = ?
            ORDER BY is_starred DESC, updated_at DESC
            """,
            (org_id,),
        ).fetchall()
    else:
        # Non-admins see dashboards they own or where they have permission, or dashboards without restrictive perms
        rows = conn.execute(
            """
            SELECT DISTINCT d.id, d.uid, d.title, d.description, d.owner_id, d.is_starred, d.created_at, d.updated_at
            FROM dashboards d
            LEFT JOIN dashboard_permissions p ON d.id = p.dashboard_id
            WHERE d.org_id = ? AND (
                d.owner_id = ?
                OR p.user_id = ?
                OR p.group_id = ?
                OR (SELECT COUNT(*) FROM dashboard_permissions WHERE dashboard_id = d.id) = 0
            )
            ORDER BY d.is_starred DESC, d.updated_at DESC
            """,
            (org_id, user_id, user_id, user_role),
        ).fetchall()

    results = []
    for r in rows:
        did, duid, title, desc, owner, starred, created, updated = r
        results.append({
            "id": did,
            "uid": duid,
            "title": title,
            "description": desc or "",
            "owner_id": owner,
            "is_starred": bool(starred),
            "created_at": str(created),
            "updated_at": str(updated),
        })
    return results


def update_dashboard(
    conn: Any,
    uid: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    layout: Optional[Dict[str, Any]] = None,
    variables: Optional[Dict[str, Any]] = None,
    is_starred: Optional[bool] = None,
) -> bool:
    """Updates dashboard properties and layout."""
    dash = get_dashboard(conn, uid)
    if not dash:
        raise DashboardNotFoundError(f"داشبورد {uid} یافت نشد.")

    now = _now_utc()
    updates = ["updated_at = ?"]
    params = [now]

    if title is not None:
        updates.append("title = ?")
        params.append(title.strip())
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if layout is not None:
        updates.append("layout = ?")
        params.append(_dumps(layout))
    if variables is not None:
        updates.append("variables = ?")
        params.append(_dumps(variables))
    if is_starred is not None:
        updates.append("is_starred = ?")
        params.append(1 if is_starred else 0)

    params.append(uid)
    sql = f"UPDATE dashboards SET {', '.join(updates)} WHERE uid = ?"
    conn.execute(sql, tuple(params))
    log.info(f"داشبورد {uid} به‌روزرسانی شد.")
    return True


def delete_dashboard(conn: Any, uid: str) -> bool:
    """Deletes a dashboard along with all its panels and permissions."""
    dash = get_dashboard(conn, uid)
    if not dash:
        return False

    dash_id = dash["id"]
    conn.execute("DELETE FROM panels WHERE dashboard_id = ?", (dash_id,))
    conn.execute("DELETE FROM dashboard_permissions WHERE dashboard_id = ?", (dash_id,))
    conn.execute("DELETE FROM dashboards WHERE id = ?", (dash_id,))
    log.info(f"داشبورد {uid} و کلیه پنل‌های آن حذف شدند.")
    return True


# ─── Panel CRUD ───────────────────────────────────────────────────────────────

def add_panel(
    conn: Any,
    dashboard_uid: str,
    panel_type: str,
    title: str,
    description: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    grid_pos: Optional[Dict[str, Any]] = None,
    uid: Optional[str] = None,
) -> Dict[str, Any]:
    """Adds a new panel to the dashboard."""
    dash = get_dashboard(conn, dashboard_uid)
    if not dash:
        raise DashboardNotFoundError(f"داشبورد {dashboard_uid} یافت نشد.")

    panel_uid = uid or f"panel_{uuid.uuid4().hex[:10]}"
    now = _now_utc()
    dash_id = dash["id"]

    query_val = _dumps(query or {})
    config_val = _dumps(config or {})
    grid_val = _dumps(grid_pos or {"x": 0, "y": 0, "w": 6, "h": 4})

    conn.execute(
        """
        INSERT INTO panels (dashboard_id, uid, type, title, description, query, config, grid_pos, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (dash_id, panel_uid, panel_type, title, description, query_val, config_val, grid_val, now, now),
    )

    # Touch dashboard updated_at
    conn.execute("UPDATE dashboards SET updated_at = ? WHERE id = ?", (now, dash_id))

    log.info(f"پنل جدید با شناسه {panel_uid} به داشبورد {dashboard_uid} اضافه شد.")
    return {
        "uid": panel_uid,
        "type": panel_type,
        "title": title,
        "description": description or "",
        "query": query or {},
        "config": config or {},
        "grid_pos": grid_pos or {"x": 0, "y": 0, "w": 6, "h": 4},
    }


def update_panel(
    conn: Any,
    panel_uid: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    grid_pos: Optional[Dict[str, Any]] = None,
) -> bool:
    """Updates configuration or position of an existing panel."""
    row = conn.execute("SELECT id, dashboard_id FROM panels WHERE uid = ?", (panel_uid,)).fetchone()
    if not row:
        return False

    panel_id, dash_id = row
    now = _now_utc()
    updates = ["updated_at = ?"]
    params = [now]

    if title is not None:
        updates.append("title = ?")
        params.append(title.strip())
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if query is not None:
        updates.append("query = ?")
        params.append(_dumps(query))
    if config is not None:
        updates.append("config = ?")
        params.append(_dumps(config))
    if grid_pos is not None:
        updates.append("grid_pos = ?")
        params.append(_dumps(grid_pos))

    params.append(panel_uid)
    conn.execute(f"UPDATE panels SET {', '.join(updates)} WHERE uid = ?", tuple(params))
    conn.execute("UPDATE dashboards SET updated_at = ? WHERE id = ?", (now, dash_id))
    log.info(f"پنل {panel_uid} به‌روزرسانی شد.")
    return True


def delete_panel(conn: Any, panel_uid: str) -> bool:
    """Removes a panel from its dashboard."""
    row = conn.execute("SELECT dashboard_id FROM panels WHERE uid = ?", (panel_uid,)).fetchone()
    if not row:
        return False

    dash_id = row[0]
    conn.execute("DELETE FROM panels WHERE uid = ?", (panel_uid,))
    now = _now_utc()
    conn.execute("UPDATE dashboards SET updated_at = ? WHERE id = ?", (now, dash_id))
    log.info(f"پنل {panel_uid} از داشبورد حذف شد.")
    return True


# ─── Access Control & Permissions ─────────────────────────────────────────────

def get_dashboard_permissions(conn: Any, dashboard_uid: str) -> List[Dict[str, Any]]:
    """Returns access control list for the dashboard."""
    dash = get_dashboard(conn, dashboard_uid)
    if not dash:
        return []

    rows = conn.execute(
        """
        SELECT p.id, p.user_id, u.username, p.group_id, p.role, p.created_at
        FROM dashboard_permissions p
        LEFT JOIN users u ON p.user_id = u.id
        WHERE p.dashboard_id = ?
        ORDER BY p.id ASC
        """,
        (dash["id"],),
    ).fetchall()

    perms = []
    for r in rows:
        pid, uid, uname, gid, role, cat = r
        perms.append({
            "id": pid,
            "user_id": uid,
            "username": uname,
            "group_id": gid,
            "role": role,
            "created_at": str(cat),
        })
    return perms


def set_dashboard_permission(
    conn: Any,
    dashboard_uid: str,
    role: str,
    user_id: Optional[int] = None,
    group_id: Optional[str] = None,
) -> bool:
    """Grants or updates a permission entry (role: 'owner', 'editor', 'viewer')."""
    if role not in ("owner", "editor", "viewer"):
        raise ValueError(f"نقش نامعتبر: {role}")

    dash = get_dashboard(conn, dashboard_uid)
    if not dash:
        raise DashboardNotFoundError(f"داشبورد {dashboard_uid} یافت نشد.")

    now = _now_utc()
    dash_id = dash["id"]

    # Delete existing permission for this user or group
    if user_id:
        conn.execute(
            "DELETE FROM dashboard_permissions WHERE dashboard_id = ? AND user_id = ?",
            (dash_id, user_id),
        )
        conn.execute(
            """
            INSERT INTO dashboard_permissions (dashboard_id, user_id, group_id, role, created_at)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (dash_id, user_id, role, now),
        )
    elif group_id:
        conn.execute(
            "DELETE FROM dashboard_permissions WHERE dashboard_id = ? AND group_id = ?",
            (dash_id, group_id),
        )
        conn.execute(
            """
            INSERT INTO dashboard_permissions (dashboard_id, user_id, group_id, role, created_at)
            VALUES (?, NULL, ?, ?, ?)
            """,
            (dash_id, group_id, role, now),
        )
    return True


def check_dashboard_access(
    conn: Any,
    dashboard_uid: str,
    user_id: Optional[int],
    user_role: Optional[str],
    action: str = "view",
) -> bool:
    """
    Checks if a user has sufficient rights to perform the requested action:
      - 'view': viewer, editor, owner (or admin, or unrestricted dashboard)
      - 'edit': editor, owner (or admin)
      - 'delete' / 'admin': owner (or admin)
    """
    # System administrator has full rights
    if user_role == "admin":
        return True

    dash = get_dashboard(conn, dashboard_uid)
    if not dash:
        return False

    dash_id = dash["id"]

    # Dashboard creator/owner has full rights
    if user_id and dash["owner_id"] == user_id:
        return True

    # Check explicit permissions table
    rows = conn.execute(
        """
        SELECT role FROM dashboard_permissions
        WHERE dashboard_id = ? AND (user_id = ? OR group_id = ?)
        """,
        (dash_id, user_id, user_role),
    ).fetchall()

    user_roles = [r[0] for r in rows]

    if action == "view":
        # Any explicit role grants view access
        if any(r in ("owner", "editor", "viewer") for r in user_roles):
            return True
        # If no explicit non-owner restrictions exist, dashboard is public to view within organization
        count_restricted = conn.execute(
            "SELECT COUNT(*) FROM dashboard_permissions WHERE dashboard_id = ? AND role != 'owner'",
            (dash_id,),
        ).fetchone()[0]
        if count_restricted == 0:
            return True
        return False

    if action == "edit":
        return any(r in ("owner", "editor") for r in user_roles)

    if action in ("delete", "admin"):
        return "owner" in user_roles

    return False


def bootstrap_dashboard_tables(conn: Any) -> None:
    """Creates timeseries, dashboard, panel, permission, and datasource tables if they do not exist."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS print_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            printer_name TEXT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            pages INTEGER,
            color_mode TEXT,
            paper_size TEXT,
            username TEXT,
            details TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cartridge_lifecycle (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            printer_name TEXT,
            cartridge_color TEXT NOT NULL,
            cartridge_serial TEXT,
            installed_at TEXT,
            removed_at TEXT,
            start_total_pages INTEGER,
            end_total_pages INTEGER,
            pages_printed INTEGER,
            estimated_yield_per_page INTEGER,
            actual_yield_per_page INTEGER,
            details TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            printer_ip TEXT NOT NULL,
            printer_name TEXT,
            total_pages INTEGER,
            color_pages INTEGER,
            mono_pages INTEGER,
            a3_pages INTEGER,
            a4_pages INTEGER,
            avg_toner_black REAL,
            avg_toner_cyan REAL,
            avg_toner_magenta REAL,
            avg_toner_yellow REAL,
            min_toner_black REAL,
            print_events_count INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT uq_daily_stats_date_ip UNIQUE (date, printer_ip)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS dashboards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT,
            layout TEXT,
            variables TEXT,
            owner_id INTEGER,
            org_id TEXT NOT NULL DEFAULT 'default',
            is_starred INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dashboards_owner ON dashboards(owner_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dashboards_org ON dashboards(org_id)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dashboard_id INTEGER NOT NULL,
            uid TEXT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            query TEXT,
            config TEXT,
            grid_pos TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (dashboard_id) REFERENCES dashboards(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_panels_dashboard_id ON panels(dashboard_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_panels_uid ON panels(uid)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS dashboard_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dashboard_id INTEGER NOT NULL,
            user_id INTEGER,
            group_id TEXT,
            role TEXT NOT NULL,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (dashboard_id) REFERENCES dashboards(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dash_perms_dash_id ON dashboard_permissions(dashboard_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_dash_perms_user ON dashboard_permissions(user_id)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            config TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            org_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_datasources_uid ON data_sources(uid)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_onboarding_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tour_name TEXT NOT NULL,
            completed_at TEXT,
            dismissed INTEGER NOT NULL DEFAULT 0,
            disabled_all INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, tour_name)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_onboarding_user ON user_onboarding_state(user_id)')
    conn.commit()

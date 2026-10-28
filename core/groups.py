# =============================================================================
# @file:         core/groups.py
# @brief:        Management of custom printer groups stored in groups.json.
# @details:      Responsibilities: Load, save and edit printer groups (create, rename, delete).
#                Main API: load_groups(), save_groups(), create_group(), rename_group(),
#                delete_group()
#                Side effects: starts threads, file I/O
# @dependencies: internal: core.time_utils
# @notes:        None.
# =============================================================================

"""
Management of custom printer groups (groups.json).

Data model:
    [
      {"id": "g_management", "name": "Management", "icon": "🏢", "color": "cyan",
       "created_at": "...", "updated_at": "..."}
    ]

Important note: the default groups of offices (imamat/soroush/...) which are based on subnet,
They are hardcoded in the frontend and are not here. This module only "custom" groups.
It keeps the user to:
  1) The group exists even without any printer.
  2) It is possible to change the name/delete the group.
  3) The group id (permanent key to store on the printer) should be separate from the display name.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime

from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

GROUPS_FILE = "groups.json"
_groups_lock = threading.Lock()

_GROUP_ICONS = ["🏢", "🏦", "🏛️", "🏥", "🏫", "🏭", "🏬", "🏗️", "💼", "📦", "🖨️", "🏪"]
_GROUP_COLORS = ["cyan", "green", "yellow", "magenta", "orange", "red", "blue"]

_slug_re = re.compile(r"[^a-z0-9_]+")


def _slugify(name: str) -> str:
    """Construct persistent id from group name. We create a unique time key for Persian names."""
    base = (name or "").strip().lower()
    base = _slug_re.sub("", base.replace(" ", "_").replace("-", "_")).strip("_")
    if base and base[0].isdigit():
        base = "g_" + base
    if not base:
        # The name is all Farsi/non-Latin → make a unique key from time
        base = datetime.now().strftime("%H%M%S%f")[:12]
    return base if base.startswith("g_") else "g_" + base


def load_groups() -> list:
    """Load the printer groups from groups.json."""

    try:
        if os.path.exists(GROUPS_FILE):
            with open(GROUPS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [g for g in data if isinstance(g, dict) and g.get("id") and g.get("name")]
    except Exception:
        log.exception("خطا در خواندن groups.json")
    return []


def save_groups(groups: list) -> list:
    """Write the printer groups to groups.json."""

    with _groups_lock:
        clean = []
        seen = set()
        for g in groups:
            gid = str(g.get("id", "")).strip()
            name = str(g.get("name", "")).strip()
            if not gid or not name or gid in seen:
                continue
            seen.add(gid)
            clean.append({
                "id": gid,
                "name": name,

                "created_at": g.get("created_at"),
                "updated_at": g.get("updated_at"),
            })
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def create_group(name: str, icon: str = None, color: str = None) -> dict:
    """Create a printer group with a unique id and return it."""

    name = (name or "").strip()
    if not name:
        raise ValueError("نام گروه الزامی است")
    if len(name) > 60:
        raise ValueError("نام گروه نباید بیش از ۶۰ کاراکتر باشد")

    groups = load_groups()
    if any(g["name"].strip().lower() == name.lower() for g in groups):
        raise ValueError("گروهی با این نام قبلاً وجود دارد")

    gid = _slugify(name)
    existing = {g["id"] for g in groups}
    suffix = 2
    candidate = gid
    while candidate in existing:
        candidate = f"{gid}_{suffix}"
        suffix += 1

    now = utc_now_iso()
    group = {
        "id": candidate,
        "name": name,

        "created_at": now,
        "updated_at": now,
    }
    groups.append(group)
    save_groups(groups)
    log.info("گروه جدید ساخته شد: %s (%s)", name, candidate)
    return group


def rename_group(group_id: str, new_name: str) -> bool:
    """Rename a printer group; returns False when the group does not exist."""

    new_name = (new_name or "").strip()
    if not new_name or len(new_name) > 60:
        raise ValueError("نام گروه نامعتبر است")
    groups = load_groups()
    if any(g["name"].strip().lower() == new_name.lower() and g["id"] != group_id for g in groups):
        raise ValueError("گروهی با این نام قبلاً وجود دارد")
    for g in groups:
        if g["id"] == group_id:
            g["name"] = new_name
            g["updated_at"] = utc_now_iso()
            save_groups(groups)
            return True
    return False


def delete_group(group_id: str) -> bool:
    """group deletion; The group assignment of printers must be cleared by the caller."""
    groups = load_groups()
    remaining = [g for g in groups if g["id"] != group_id]
    if len(remaining) == len(groups):
        return False
    save_groups(remaining)
    log.info("گروه %s حذف شد", group_id)
    return True



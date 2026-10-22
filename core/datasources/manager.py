"""
PrintGuard Data Source Registry & Manager (Phase J.6)
Manages driver registration, database persistence, connection testing, and secure querying.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

from core.datasources.base import BaseDataSource, DataSourceError
from core.datasources.crypto import mask_config, merge_masked_config
from core.datasources.drivers.github_ds import GitHubDataSource
from core.datasources.drivers.postgresql_ds import PostgreSQLDataSource
from core.datasources.drivers.rest_ds import RestDataSource

log = logging.getLogger("PrinterMonitor.DataSourceManager")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(val: Any) -> Any:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


def _dumps(val: Any) -> str:
    if val is None:
        return "{}"
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


class DataSourceRegistry:
    """Registry holding all available data source driver implementations."""

    def __init__(self):
        self._drivers: Dict[str, Type[BaseDataSource]] = {}
        self._register_defaults()

    def _register_defaults(self):
        self.register(PostgreSQLDataSource)
        self.register(RestDataSource)
        self.register(GitHubDataSource)

    def register(self, driver_cls: Type[BaseDataSource]):
        if not issubclass(driver_cls, BaseDataSource):
            raise TypeError(f"{driver_cls} must subclass BaseDataSource.")
        self._drivers[driver_cls.type_name] = driver_cls

    def get_driver(self, type_name: str) -> Optional[Type[BaseDataSource]]:
        return self._drivers.get(type_name)

    def list_drivers(self) -> List[Dict[str, Any]]:
        catalog = []
        for t_name, cls in self._drivers.items():
            catalog.append({
                "type": t_name,
                "display_name": cls.display_name,
                "description": cls.description,
                "icon": cls.icon,
                "schema": cls.config_schema(),
            })
        return catalog


# Global Driver Registry instance
datasource_registry = DataSourceRegistry()


# ─── Data Source Database Persistence ─────────────────────────────────────────

def create_datasource(
    conn: Any,
    name: str,
    ds_type: str,
    config: Optional[Dict[str, Any]] = None,
    is_default: bool = False,
    org_id: str = "default",
    uid: Optional[str] = None,
) -> Dict[str, Any]:
    """Persists a new data source into database."""
    if not name or not name.strip():
        raise ValueError("نام Data Source الزامی است.")

    driver_cls = datasource_registry.get_driver(ds_type)
    if not driver_cls:
        raise ValueError(f"نوع منبع داده ناشناخته است: {ds_type}")

    ds_uid = uid or f"ds_{uuid.uuid4().hex[:10]}"
    now = _now_utc()
    cfg_val = _dumps(config or {})

    conn.execute(
        """
        INSERT INTO data_sources (uid, name, type, config, is_default, org_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ds_uid, name.strip(), ds_type, cfg_val, 1 if is_default else 0, org_id, now, now),
    )

    log.info(f"منبع داده جدید با شناسه {ds_uid} و نام '{name}' ایجاد شد.")
    return get_datasource(conn, ds_uid, mask=True)


def get_datasource(conn: Any, uid: str, mask: bool = True) -> Optional[Dict[str, Any]]:
    """Retrieves a data source by UID, optionally masking credentials."""
    row = conn.execute(
        """
        SELECT id, uid, name, type, config, is_default, org_id, created_at, updated_at
        FROM data_sources
        WHERE uid = ?
        """,
        (uid,),
    ).fetchone()

    if not row:
        return None

    ds_id, d_uid, name, ds_type, cfg, is_def, org, cat, uat = row
    config_dict = _loads(cfg)
    if mask:
        config_dict = mask_config(config_dict)

    return {
        "id": ds_id,
        "uid": d_uid,
        "name": name,
        "type": ds_type,
        "config": config_dict,
        "is_default": bool(is_def),
        "org_id": org,
        "created_at": str(cat),
        "updated_at": str(uat),
    }


def list_datasources(conn: Any, org_id: str = "default", mask: bool = True) -> List[Dict[str, Any]]:
    """Lists all data sources in the organization."""
    rows = conn.execute(
        """
        SELECT id, uid, name, type, config, is_default, org_id, created_at, updated_at
        FROM data_sources
        WHERE org_id = ?
        ORDER BY is_default DESC, name ASC
        """,
        (org_id,),
    ).fetchall()

    results = []
    for r in rows:
        ds_id, d_uid, name, ds_type, cfg, is_def, org, cat, uat = r
        config_dict = _loads(cfg)
        if mask:
            config_dict = mask_config(config_dict)

        results.append({
            "id": ds_id,
            "uid": d_uid,
            "name": name,
            "type": ds_type,
            "config": config_dict,
            "is_default": bool(is_def),
            "org_id": org,
            "created_at": str(cat),
            "updated_at": str(uat),
        })
    return results


def update_datasource(
    conn: Any,
    uid: str,
    name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    is_default: Optional[bool] = None,
) -> bool:
    """Updates a data source while preserving masked credentials."""
    existing = get_datasource(conn, uid, mask=False)
    if not existing:
        return False

    now = _now_utc()
    updates = ["updated_at = ?"]
    params = [now]

    if name is not None:
        updates.append("name = ?")
        params.append(name.strip())

    if config is not None:
        merged_cfg = merge_masked_config(config, existing["config"])
        updates.append("config = ?")
        params.append(_dumps(merged_cfg))

    if is_default is not None:
        updates.append("is_default = ?")
        params.append(1 if is_default else 0)

    params.append(uid)
    conn.execute(f"UPDATE data_sources SET {', '.join(updates)} WHERE uid = ?", tuple(params))
    log.info(f"منبع داده {uid} به‌روزرسانی شد.")
    return True


def delete_datasource(conn: Any, uid: str) -> bool:
    """Deletes a data source from the system."""
    row = conn.execute("SELECT id FROM data_sources WHERE uid = ?", (uid,)).fetchone()
    if not row:
        return False

    conn.execute("DELETE FROM data_sources WHERE uid = ?", (uid,))
    log.info(f"منبع داده {uid} حذف شد.")
    return True


def instantiate_datasource(conn: Any, uid: str) -> BaseDataSource:
    """Loads a data source from DB and returns an instantiated driver."""
    rec = get_datasource(conn, uid, mask=False)
    if not rec:
        raise DataSourceError(f"منبع داده {uid} یافت نشد.")

    driver_cls = datasource_registry.get_driver(rec["type"])
    if not driver_cls:
        raise DataSourceError(f"درایور برای نوع {rec['type']} یافت نشد.")

    return driver_cls(uid=rec["uid"], name=rec["name"], config=rec["config"])


def test_datasource_connection(conn: Any, uid: str) -> Dict[str, Any]:
    """Tests connectivity to a specific data source."""
    ds = instantiate_datasource(conn, uid)
    return ds.test_connection()


def execute_datasource_query(conn: Any, uid: str, query_params: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a query against a data source."""
    ds = instantiate_datasource(conn, uid)
    return ds.query(query_params)

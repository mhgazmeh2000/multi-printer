"""
REST API Data Source Driver (Phase J.7)
Fetches and flattens JSON data from REST endpoints with configurable authentication.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
import requests

from core.datasources.base import BaseDataSource, DataSourceError

log = logging.getLogger("PrinterMonitor.RESTDataSource")


class RestDataSource(BaseDataSource):
    type_name = "rest"
    display_name = "وب‌سرویس REST API"
    description = "دریافت داده‌های ساخت‌یافته JSON از وب‌سرویس‌ها و APIهای سازمانی"
    icon = "globe"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "auth_type": {"type": "string", "enum": ["none", "bearer", "basic", "api_key"], "default": "none"},
                "token": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "api_key_header": {"type": "string", "default": "X-API-Key"},
                "timeout": {"type": "integer", "default": 10},
            },
            "required": ["base_url"],
        }

    def _prepare_headers_and_auth(self) -> tuple[Dict[str, str], Any]:
        cfg = self.config
        headers = {"Accept": "application/json"}
        auth = None

        auth_type = cfg.get("auth_type", "none")
        if auth_type == "bearer" and cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        elif auth_type == "basic" and cfg.get("username"):
            auth = (cfg.get("username"), cfg.get("password", ""))
        elif auth_type == "api_key" and cfg.get("token"):
            h_name = cfg.get("api_key_header", "X-API-Key")
            headers[h_name] = cfg["token"]

        return headers, auth

    def test_connection(self) -> Dict[str, Any]:
        url = self.config.get("base_url")
        if not url:
            raise DataSourceError("آدرس base_url تعریف نشده است.")

        headers, auth = self._prepare_headers_and_auth()
        timeout = self.config.get("timeout", 10)

        try:
            res = requests.get(url, headers=headers, auth=auth, timeout=timeout)
            if res.status_code < 400:
                return {"status": "ok", "message": f"اتصال به وب‌سرویس برقرار شد (HTTP {res.status_code})."}
            raise DataSourceError(f"پاسخ خطای سرور: HTTP {res.status_code}")
        except Exception as e:
            raise DataSourceError(f"خطا در اتصال به وب‌سرویس REST: {e}")

    def query(self, query_params: Dict[str, Any]) -> Dict[str, Any]:
        base_url = self.config.get("base_url", "").rstrip("/")
        endpoint = query_params.get("endpoint", "").lstrip("/")
        full_url = f"{base_url}/{endpoint}" if endpoint else base_url

        headers, auth = self._prepare_headers_and_auth()
        params = query_params.get("params", {})
        timeout = self.config.get("timeout", 10)

        try:
            res = requests.get(full_url, headers=headers, auth=auth, params=params, timeout=timeout)
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            raise DataSourceError(f"خطا در دریافت داده از وب‌سرویس: {e}")

        # Flatten records into tabular columns & rows
        records: List[Dict[str, Any]] = []
        if isinstance(data, list):
            records = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            # Check if there is a primary list field like 'items', 'data', 'results', 'rows'
            for key in ("items", "data", "results", "rows", "readings", "events"):
                if key in data and isinstance(data[key], list):
                    records = [item for item in data[key] if isinstance(item, dict)]
                    break
            if not records:
                records = [data]

        if not records:
            return {"columns": [], "rows": [], "total": 0}

        col_keys = list(records[0].keys())
        columns = [{"field": k, "title": k, "type": "text"} for k in col_keys]

        return {
            "columns": columns,
            "rows": records,
            "total": len(records),
        }

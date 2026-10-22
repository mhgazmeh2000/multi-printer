"""
PrintGuard Data Source Base & Contract (Phase J.6)
Defines standard interfaces for database, REST, and external integrations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DataSourceError(Exception):
    """Base exception for data source errors."""
    pass


class DataSourceSecurityError(DataSourceError):
    """Raised when a query violates security restrictions (e.g. non-SELECT queries)."""
    pass


class BaseDataSource(ABC):
    """Abstract base class for all PrintGuard data source drivers."""

    type_name: str = "base"
    display_name: str = "Base Data Source"
    description: str = ""
    icon: str = "database"

    def __init__(self, uid: str, name: str, config: Optional[Dict[str, Any]] = None):
        self.uid = uid
        self.name = name
        self.config = config or {}

    @classmethod
    @abstractmethod
    def config_schema(cls) -> Dict[str, Any]:
        """Returns JSON schema describing connection and auth parameters."""
        raise NotImplementedError

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validates configuration parameters."""
        if not isinstance(config, dict):
            raise DataSourceError("Configuration must be a dictionary.")
        return config

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """Tests connectivity and returns a status dictionary."""
        raise NotImplementedError

    @abstractmethod
    def query(self, query_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a query and returns standardized tabular format:
        {
            "columns": [{"field": "date", "title": "تاریخ", "type": "date"}, ...],
            "rows": [{"date": "2026-10-15", "pages": 120}, ...],
            "total": 1
        }
        """
        raise NotImplementedError

    def get_schema(self) -> Dict[str, Any]:
        """Returns metadata regarding available tables, metrics, or endpoints."""
        return {"tables": []}

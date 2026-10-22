"""
PrintGuard Data Sources Framework package.
"""

from core.datasources.base import BaseDataSource, DataSourceError, DataSourceSecurityError
from core.datasources.manager import datasource_registry

data_source_registry = datasource_registry

__all__ = [
    "BaseDataSource",
    "DataSourceError",
    "DataSourceSecurityError",
    "datasource_registry",
    "data_source_registry",
]

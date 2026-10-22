"""
Built-in Data Source drivers.
"""

from core.datasources.drivers.postgresql_ds import PostgreSQLDataSource
from core.datasources.drivers.rest_ds import RestDataSource
from core.datasources.drivers.github_ds import GitHubDataSource

__all__ = [
    "PostgreSQLDataSource",
    "RestDataSource",
    "GitHubDataSource",
]

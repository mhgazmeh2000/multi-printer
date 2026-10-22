"""
PrintGuard Dashboard Framework package.
"""

from core.dashboard.panel_registry import panel_registry
from core.dashboard.manager import (
    create_dashboard,
    get_dashboard,
    list_dashboards,
    update_dashboard,
    delete_dashboard,
    add_panel,
    update_panel,
    delete_panel,
    get_dashboard_permissions,
    set_dashboard_permission,
    bootstrap_dashboard_tables,
)

__all__ = [
    "panel_registry",
    "create_dashboard",
    "get_dashboard",
    "list_dashboards",
    "update_dashboard",
    "delete_dashboard",
    "add_panel",
    "update_panel",
    "delete_panel",
    "get_dashboard_permissions",
    "set_dashboard_permission",
    "bootstrap_dashboard_tables",
]

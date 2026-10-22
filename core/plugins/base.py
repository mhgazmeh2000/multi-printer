"""
PrintGuard Plugin Base (Phase J.5)
Defines the plugin contract, lifecycle hooks, and context.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Dict, List, Optional


class PluginContext:
    """Runtime context provided to plugin lifecycle hooks."""

    def __init__(self, app: Any = None, org_id: str = "default"):
        self.app = app
        self.org_id = org_id


class BasePlugin(ABC):
    """Abstract base class for all PrintGuard plugins."""

    name: str = ""
    version: str = "1.0.0"
    manifest: Dict[str, Any] = {}

    def __init__(self, manifest: Optional[Dict[str, Any]] = None):
        self.manifest = manifest or {}
        self.name = self.manifest.get("name", self.name)
        self.version = self.manifest.get("version", self.version)

    def on_install(self, context: PluginContext) -> None:
        """Called once when plugin is first installed."""
        pass

    def on_enable(self, context: PluginContext) -> None:
        """Called when plugin is enabled."""
        pass

    def on_disable(self, context: PluginContext) -> None:
        """Called when plugin is disabled."""
        pass

    def on_uninstall(self, context: PluginContext) -> None:
        """Called when plugin is uninstalled."""
        pass

    def get_blueprint(self) -> Optional[Any]:
        """Returns the Flask Blueprint providing plugin routes, if any."""
        return None

    def get_widgets(self) -> List[Any]:
        """Returns list of widget / panel classes to register with PanelRegistry."""
        return []

    def get_menu_entries(self) -> List[Dict[str, Any]]:
        """Returns menu entries defined in manifest or dynamically."""
        return self.manifest.get("menu_entries", [])

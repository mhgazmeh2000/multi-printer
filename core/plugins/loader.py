"""
PrintGuard Plugin Manager & Loader (Phase J.5)
Dynamic discovery, isolated import, blueprint mounting, widget registration,
and lifecycle management.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from core.dashboard.panel_registry import BasePanel, panel_registry
from core.plugins.base import BasePlugin, PluginContext
from core.plugins.manifest import load_manifest, PluginManifestError

log = logging.getLogger("PrinterMonitor.PluginLoader")


class PluginManager:
    """Manages the discovery, registration, and lifecycle of PrintGuard plugins."""

    def __init__(self, plugins_dir: Optional[str] = None):
        if plugins_dir is None:
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.plugins_dir = os.path.join(repo_root, "plugins")
        else:
            self.plugins_dir = plugins_dir

        self.plugins: Dict[str, Dict[str, Any]] = {}
        self.app: Optional[Any] = None
        self._enabled_by_org: Dict[str, set] = {"default": set()}

    def init_app(self, app: Any) -> None:
        """Attaches PluginManager to a Flask application instance."""
        self.app = app
        self.discover_and_load_all()

    def discover_and_load_all(self) -> None:
        """Scans plugins_dir and loads all valid plugins."""
        if not os.path.isdir(self.plugins_dir):
            log.warning(f"Plugins directory '{self.plugins_dir}' does not exist.")
            return

        # Ensure plugins directory is in sys.path
        if self.plugins_dir not in sys.path:
            sys.path.insert(0, self.plugins_dir)

        repo_root = os.path.dirname(self.plugins_dir)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        for entry in os.listdir(self.plugins_dir):
            plugin_path = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(plugin_path) or entry.startswith((".", "_")):
                continue

            manifest_file = os.path.join(plugin_path, "manifest.json")
            if os.path.isfile(manifest_file):
                self._load_single_plugin(plugin_path)

    def _load_single_plugin(self, plugin_dir: str) -> None:
        manifest_file = os.path.join(plugin_dir, "manifest.json")
        try:
            manifest = load_manifest(manifest_file)
        except Exception as e:
            log.error(f"Failed to read manifest at {manifest_file}: {e}")
            return

        name = manifest["name"]

        # Isolated import guard: a broken plugin must never crash the core application!
        try:
            entry_point = manifest["entry_point"]
            mod_name, cls_name = entry_point.split(":")
            full_module_name = f"plugins.{name}.{mod_name}"

            mod = importlib.import_module(full_module_name)
            plugin_cls = getattr(mod, cls_name)

            if not issubclass(plugin_cls, BasePlugin):
                raise TypeError(f"{plugin_cls} must subclass BasePlugin.")

            instance: BasePlugin = plugin_cls(manifest=manifest)
            context = PluginContext(app=self.app)

            # Register blueprint if Flask app is active
            if self.app is not None:
                bp = instance.get_blueprint()
                if bp is not None:
                    # Avoid double registration
                    if bp.name not in self.app.blueprints:
                        prefix = manifest.get("routes_prefix")
                        self.app.register_blueprint(bp, url_prefix=prefix)
                        log.info(f"Mounted blueprint '{bp.name}' for plugin '{name}' at '{prefix}'.")

            # Register any custom panel widgets in panel_registry (independent of app)
            for w_cls in instance.get_widgets():
                if issubclass(w_cls, BasePanel):
                    panel_registry.register(w_cls)
                    log.info(f"Registered plugin widget '{w_cls.type_name}' in PanelRegistry.")

            # Enable plugin by default unless feature flag restricts it
            instance.on_enable(context)
            self._enabled_by_org["default"].add(name)

            self.plugins[name] = {
                "instance": instance,
                "manifest": manifest,
                "status": "enabled",
                "dir": plugin_dir,
            }
            log.info(f"Plugin '{name}' v{manifest.get('version')} loaded successfully.")

        except Exception as e:
            log.error(f"Error loading plugin '{name}': {e}", exc_info=True)
            self.plugins[name] = {
                "instance": None,
                "manifest": manifest,
                "status": "error",
                "error": str(e),
                "dir": plugin_dir,
            }

    def enable_plugin(self, name: str, org_id: str = "default") -> bool:
        """Enables a plugin without restarting the application."""
        record = self.plugins.get(name)
        if not record or not record.get("instance"):
            return False

        instance: BasePlugin = record["instance"]
        context = PluginContext(app=self.app, org_id=org_id)
        try:
            instance.on_enable(context)
            # Re-register widgets
            for w_cls in instance.get_widgets():
                if issubclass(w_cls, BasePanel):
                    panel_registry.register(w_cls)

            self._enabled_by_org.setdefault(org_id, set()).add(name)
            record["status"] = "enabled"
            log.info(f"Plugin '{name}' enabled for org '{org_id}'.")
            return True
        except Exception as e:
            log.error(f"Failed to enable plugin '{name}': {e}", exc_info=True)
            return False

    def disable_plugin(self, name: str, org_id: str = "default") -> bool:
        """Disables a plugin without restarting the application."""
        record = self.plugins.get(name)
        if not record or not record.get("instance"):
            return False

        instance: BasePlugin = record["instance"]
        context = PluginContext(app=self.app, org_id=org_id)
        try:
            instance.on_disable(context)
            # Unregister widgets from Panel Registry
            for w_cls in instance.get_widgets():
                if issubclass(w_cls, BasePanel):
                    panel_registry.unregister(w_cls.type_name)

            self._enabled_by_org.get(org_id, set()).discard(name)
            record["status"] = "disabled"
            log.info(f"Plugin '{name}' disabled for org '{org_id}'.")
            return True
        except Exception as e:
            log.error(f"Failed to disable plugin '{name}': {e}", exc_info=True)
            return False

    def list_plugins(self, org_id: str = "default") -> List[Dict[str, Any]]:
        """Returns catalog of all plugins with statuses."""
        catalog = []
        for name, rec in self.plugins.items():
            m = rec["manifest"]
            is_enabled = name in self._enabled_by_org.get(org_id, set()) and rec["status"] == "enabled"
            catalog.append({
                "name": name,
                "version": m.get("version"),
                "display_name": m.get("display_name"),
                "description": m.get("description", ""),
                "author": m.get("author", ""),
                "status": "enabled" if is_enabled else rec["status"],
                "menu_entries": m.get("menu_entries", []),
                "widgets": m.get("widgets", []),
                "error": rec.get("error"),
            })
        return catalog

    def get_menu_entries(self, user_role: str = "viewer", org_id: str = "default") -> List[Dict[str, Any]]:
        """Returns navigation menu items from enabled plugins matching the user's role."""
        entries = []
        for name, rec in self.plugins.items():
            if name not in self._enabled_by_org.get(org_id, set()):
                continue
            instance: Optional[BasePlugin] = rec.get("instance")
            if not instance:
                continue

            for item in instance.get_menu_entries():
                required_role = item.get("role", "viewer")
                # Role check: admin can access everything; manager can access manager+viewer; viewer only viewer
                if user_role == "admin":
                    entries.append(item)
                elif user_role == "manager" and required_role in ("manager", "viewer"):
                    entries.append(item)
                elif user_role == "viewer" and required_role == "viewer":
                    entries.append(item)
        return entries


# Global singleton instance
plugin_manager = PluginManager()

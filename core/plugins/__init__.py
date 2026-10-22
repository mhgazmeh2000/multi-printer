"""
PrintGuard Plugin Framework package.
"""

from core.plugins.base import BasePlugin, PluginContext
from core.plugins.loader import plugin_manager

__all__ = [
    "BasePlugin",
    "PluginContext",
    "plugin_manager",
]

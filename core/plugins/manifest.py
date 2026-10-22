"""
PrintGuard Plugin Manifest Validator & Loader (Phase J.5)
Parses, validates, and standardizes plugin manifest.json files.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

PLUGIN_NAME_RE = re.compile(r"^[a-z0-9_-]{2,32}$")


class PluginManifestError(Exception):
    """Raised when manifest validation fails."""
    pass


def load_manifest(manifest_path: str) -> Dict[str, Any]:
    """Loads and validates a plugin manifest.json file."""
    if not os.path.isfile(manifest_path):
        raise PluginManifestError(f"Manifest file not found: {manifest_path}")

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise PluginManifestError(f"Failed to parse manifest JSON at {manifest_path}: {e}")

    validate_manifest(data)
    return data


def validate_manifest(manifest: Dict[str, Any]) -> None:
    """Validates the contents of a plugin manifest."""
    if not isinstance(manifest, dict):
        raise PluginManifestError("Manifest must be a JSON object.")

    name = manifest.get("name")
    if not name or not PLUGIN_NAME_RE.fullmatch(name):
        raise PluginManifestError(f"Invalid plugin name '{name}'. Must match [a-z0-9_-]{{2,32}}.")

    if not manifest.get("version"):
        raise PluginManifestError("Plugin manifest requires 'version'.")

    if not manifest.get("display_name"):
        raise PluginManifestError("Plugin manifest requires 'display_name'.")

    if not manifest.get("entry_point"):
        raise PluginManifestError("Plugin manifest requires 'entry_point' (e.g. 'plugin:AttendancePlugin').")

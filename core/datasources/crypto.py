"""
PrintGuard Data Source Credential Protection & Masking (Phase J.6)
Protects sensitive connection parameters (passwords, tokens, api keys) from leaking.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

SENSITIVE_KEYS = {
    "password",
    "token",
    "secret",
    "api_key",
    "private_key",
    "auth_token",
    "client_secret",
}


def mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a sanitized copy of configuration with sensitive values masked."""
    if not isinstance(config, dict):
        return {}

    sanitized = copy.deepcopy(config)
    for k, v in sanitized.items():
        if any(sec in k.lower() for sec in SENSITIVE_KEYS):
            if v:
                sanitized[k] = "********"
    return sanitized


def merge_masked_config(new_config: Dict[str, Any], existing_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merges updated configuration while preserving existing sensitive values
    if user submitted the masked placeholder '********'.
    """
    merged = copy.deepcopy(new_config)
    for k, v in new_config.items():
        if v == "********" and k in existing_config:
            merged[k] = existing_config[k]
    return merged

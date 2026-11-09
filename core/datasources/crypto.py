"""Recursive credential masking; never mutate or return secrets from nested configs."""
from __future__ import annotations

import copy
from typing import Any, Dict

SENSITIVE_KEYS = {
    "password", "token", "secret", "api_key", "private_key", "auth_token",
    "client_secret", "authorization", "credential", "dsn", "connection_string",
}
MASK = "********"


def mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    def mask(value):
        if isinstance(value, dict):
            return {
                key: MASK if any(word in str(key).lower() for word in SENSITIVE_KEYS) and item
                else mask(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [mask(item) for item in value]
        return copy.deepcopy(value)
    return mask(config) if isinstance(config, dict) else {}


def merge_masked_config(new_config: Dict[str, Any], existing_config: Dict[str, Any]) -> Dict[str, Any]:
    """Keep existing values only where a submitted mask has an existing counterpart."""
    def merge(new, old):
        if new == MASK:
            return copy.deepcopy(old) if old is not None else MASK
        if isinstance(new, dict):
            old = old if isinstance(old, dict) else {}
            return {**copy.deepcopy(old), **{key: merge(item, old.get(key)) for key, item in new.items()}}
        if isinstance(new, list):
            old = old if isinstance(old, list) else []
            return [merge(item, old[i] if i < len(old) else None) for i, item in enumerate(new)]
        return copy.deepcopy(new)
    return merge(new_config, existing_config)

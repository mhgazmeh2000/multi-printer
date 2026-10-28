# =============================================================================
# @file:         core/collectors/__init__.py
# @brief:        Package of per-brand data collectors.
# @details:      Responsibilities: Groups the brand collectors; no runtime logic.
#                Main API: none (no public functions or classes)
#                Side effects: none at import time
# @dependencies: internal: .base, .brother, .canon, .hp, .toshiba
# @notes:        None.
# =============================================================================

"""
Pack of data collectors for different brands
"""

from .toshiba import collect_toshiba
from .hp import collect_hp
from .canon import collect_canon
from .brother import collect_brother
from .base import _counters_event, detect_brand, si, ss, validate_counter_consistency

__all__ = [
    'collect_toshiba',
    'collect_hp',
    'collect_canon',
    'collect_brother',
    '_counters_event',
    'detect_brand',
    'si',
    'ss',
    'validate_counter_consistency',
]
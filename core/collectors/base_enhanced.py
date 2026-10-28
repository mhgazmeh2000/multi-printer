# =============================================================================
# @file:         core/collectors/base_enhanced.py
# @brief:        Stable bridge that exposes the enhanced collection logic to the collectors.
# @details:      Responsibilities: Re-export collect_enhanced so callers do not depend on the
#                enhanced module path.
#                Main API: none (no public functions or classes)
#                Side effects: none at import time
# @dependencies: internal: core.enhanced_collector
# @notes:        None.
# =============================================================================

"""Stable bridge for enhanced collection logic.

This module exists so callers can move away from the legacy
`core.enhanced_collector` import path without changing behavior.
"""

from core.enhanced_collector import collect_enhanced  # noqa: F401 - deliberate re-export, used by core/poller.py

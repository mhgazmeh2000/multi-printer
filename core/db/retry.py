"""Bounded retries for explicitly owned, replay-safe *whole transactions*.

Never put this around one statement inside an open multi-statement transaction.
No retries for disconnects/unknown commit results, timeouts or integrity errors.
"""
from __future__ import annotations

import random
import time

RETRYABLE_STATES = frozenset({'40001', '40P01'})


def raise_if_retryable(exc):
    if getattr(exc, 'sqlstate', None) in RETRYABLE_STATES:
        raise exc


def retry_transaction(operation, *, attempts=3):
    """operation must roll back on error and return only after a successful commit."""
    if attempts < 1:
        raise ValueError('attempts must be positive')
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if getattr(exc, 'sqlstate', None) not in RETRYABLE_STATES or attempt + 1 == attempts:
                raise
            time.sleep(random.uniform(.01, .03) * (2 ** attempt))

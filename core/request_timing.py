"""Opt-in, request-local wall timings; never record arguments, SQL or telemetry.

Only decorated requests exceeding five seconds produce one summary. Nested
stages overlap intentionally (e.g. yield_status includes yield_history).
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
from time import perf_counter

log = logging.getLogger("PrinterMonitor")
_current = ContextVar("slow_request_timing", default=None)
SLOW_REQUEST_SECONDS = 5.0


@contextmanager
def timing_stage(name):
    trace = _current.get()
    if trace is None:
        yield
        return
    start = perf_counter()
    try:
        yield
    finally:
        trace[name] = trace.get(name, 0.0) + perf_counter() - start


def trace_slow_request(name):
    """Use only constant operation/stage names; preserve return/exception exactly."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            stages = {}
            token = _current.set(stages)
            start = perf_counter()
            failed = False
            try:
                return fn(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                elapsed = perf_counter() - start
                _current.reset(token)
                if elapsed >= SLOW_REQUEST_SECONDS:
                    log.warning("Slow request timing: %s", json.dumps({
                        "operation": name,
                        "total_ms": round(elapsed * 1000, 1),
                        "failed": failed,
                        "stages_ms": {key: round(value * 1000, 1)
                                      for key, value in stages.items()},
                    }, sort_keys=True))
        return wrapped
    return decorate


@contextmanager
def timed_lock(lock, name):
    """Time acquisition, not protected work; preserve unbounded lock semantics."""
    with timing_stage(name):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()

"""Read-only evidence for split-counter anomalies; never used in page arithmetic."""
from core.time_utils import utc_now_iso


def timed_counter_read(readings, source, read):
    """Time one existing read, without retries or changes to its return/exception."""
    started = utc_now_iso()
    try:
        value = read()
    except Exception:
        if readings is not None:
            readings.append({"source": source, "started_at": started,
                             "finished_at": utc_now_iso(), "read_failed": True})
        raise
    if readings is not None:
        # Only counter values, never request arguments/community strings or errors.
        try:
            raw = int(value) if value is not None else None
        except Exception:  # Evidence conversion must never change the collector outcome.
            raw = None
        readings.append({"source": source, "started_at": started,
                         "finished_at": utc_now_iso(), "raw_value": raw})
    return value


def split_diagnostic(previous, current, delta_fc, delta_bw, readings, poll_timestamp,
                     previous_snapshot_at, reason):
    """Previous values are validator anchors, not necessarily the previous raw read."""
    return {
        "version": 1,
        "reason": reason,
        "previous": previous,
        "current": current,
        "delta": {"full_color": delta_fc, "black_white": delta_bw,
                  "total": current["total"] - previous["total"]},
        "previous_snapshot_at": previous_snapshot_at,
        "poll_started_at": poll_timestamp,
        "reads": list(readings or []),
    }

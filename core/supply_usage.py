"""
@file:         core/supply_usage.py
@brief:        System-counted cartridge usage since first observation or reset.
@details:      The values are counted by PrintGuard itself from its own page counters on every poll.
               Raw counters come from device telemetry; these records are computed locally.
               No SNMP/EWS or database I/O is performed by this module.
               Replacement starts a new record: a manual cartridge reset (toner_reset endpoint),
               or a change of a genuine chip/serial identity reported for that colour.
               Record functions are pure; a shared striped lock serializes caller updates.
               No I/O, Flask or store access.
@dependencies: stdlib only
@notes:        Counter basis: black uses the total page counter; cyan, magenta and yellow use the
               full-colour counter. These are device-level page counts, so the value is the number of
               pages printed while this cartridge was installed, not a toner-accurate measurement.
               Record shape: {"installed_on": iso date, "start_pages": int|None,
               "last_pages": int|None, "last_use": iso date|None, "cartridge_id": str|None}.
"""

import math
import threading

_USAGE_LOCKS = tuple(threading.RLock() for _ in range(64))


def tracking_lock(ip):
    """Serialize tracking/reset read-modify-write in this application process."""
    return _USAGE_LOCKS[hash(str(ip)) % len(_USAGE_LOCKS)]


def _page_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value) if value >= 0 else None


_COUNTER_BASIS = {"black": "total", "cyan": "full_color", "magenta": "full_color", "yellow": "full_color"}
_FALLBACK_PREFIXES = ("model:", "toner_gen:", "part:")


def counter_basis(colour, counters):
    """Page counter used for ``colour`` (validated counters dict), or None if unavailable."""
    key = _COUNTER_BASIS.get(str(colour or "").lower())
    if key is None or not isinstance(counters, dict):
        return None
    return _page_count(counters.get(key))


def genuine_cartridge_ids(cartridge_ids, identity_types):
    """Keep only chip/serial identities (fallback 'model:/toner_gen:/part:' ids are not identities)."""
    out = {}
    if not isinstance(cartridge_ids, dict):
        return out
    types = {str(k).lower(): v for k, v in identity_types.items()} if isinstance(identity_types, dict) else {}
    for colour, cid in cartridge_ids.items():
        colour = str(colour).lower()
        # Unknown provenance is NOT proof of a physical identity.
        if not isinstance(cid, str) or types.get(colour) not in ("chip_id", "serial"):
            continue
        text = cid.strip()
        if not text or text.lower().startswith(_FALLBACK_PREFIXES):
            continue
        out[colour] = text
    return out


def new_record(basis, cartridge_id, today):
    """Start tracking a cartridge on ``today`` (not proof of its physical install date)."""
    basis = _page_count(basis)
    return {
        "installed_on": today,
        "start_pages": basis,
        "last_pages": basis,
        "last_use": None,
        "cartridge_id": cartridge_id or None,
    }


def advance_record(record, basis, cartridge_id, today):
    """Return the record updated with this poll. The input dict is not modified."""
    basis = _page_count(basis)
    if not isinstance(record, dict) or not record.get("installed_on"):
        return new_record(basis, cartridge_id, today)

    old_id = record.get("cartridge_id")
    if cartridge_id and old_id and cartridge_id != old_id:
        # Genuine identity changed: the cartridge was replaced.
        return new_record(basis, cartridge_id, today)

    rec = dict(record)
    if cartridge_id and not old_id:
        rec["cartridge_id"] = cartridge_id
    if basis is None:
        return rec

    last = rec.get("last_pages")
    start = rec.get("start_pages")
    if (isinstance(last, bool) or not isinstance(last, int) or last < 0
            or isinstance(start, bool) or not isinstance(start, int) or start > last):
        rec["start_pages"] = basis
        rec["last_pages"] = basis
        return rec
    if basis < last:
        # Device counter went back (service reset of the counter): keep the pages already counted.
        used = last - start
        rec["start_pages"] = basis - used
        rec["last_pages"] = basis
        return rec
    if basis > last:
        rec["last_use"] = today
        rec["last_pages"] = basis
    return rec


def usage_view(record):
    """Values shown in the UI for one colour."""
    if not isinstance(record, dict) or not record.get("installed_on"):
        return {"pages": None, "first_install_date": None, "last_use_date": None}
    last = record.get("last_pages")
    start = record.get("start_pages")
    valid = (isinstance(last, int) and not isinstance(last, bool) and last >= 0
             and isinstance(start, int) and not isinstance(start, bool) and start <= last)
    pages = last - start if valid else None
    return {
        "pages": pages,
        "first_install_date": record.get("installed_on"),
        "last_use_date": record.get("last_use"),
    }

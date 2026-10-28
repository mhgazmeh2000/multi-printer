# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/collectors/cartridge_discovery.py
# @brief:        Discovers which OID holds each cartridge's unique identifier (chip ID / supply
#                serial).
# @details:      Responsibilities: Probe supply tables, persist learned OIDs and queue discovery
#                runs.
#                Main API: reset_stability(), load_learned_oids(), discover_for_printer(),
#                queue_discovery()
#                Side effects: network I/O, starts threads, file I/O
# @dependencies: internal: core.collectors.cartridge_id, core.snmp.protocol, core.time_utils
# @notes:        None.
# =============================================================================

"""Automatic discovery of the OID of the unique identifier of the cartridge (Chip ID / Supply Serial).

Target: without any manual setting, for all HP / Canon / Brother / Toshiba printers
The unique ID of the cartridge is found and "confirmed" to the existing engine CARTRIDGE_CHANGED
(core/collectors/base.py) can definitely detect cartridge replacement.

Three steps:

1) **discovery (walk):** Printer-MIB standard subsection
   (prtMarkerSuppliesDescription — only for marker→color mapping) and subclasses
   vendor Each brand is scrolled with limited GETNEXT (max_vars); Any string value
   that pass the strict filter of ``normalize_id()'' (from cartridge_id.py) and
   If it is "similar to the serial", the Chip ID candidate of that color will be registered.

2) Stability Gate: The candidate value is confirmed only when
   In ``STABILITY_HITS'' successive probes remain exactly the same. dynamic values (toner level,
   state, counters) are changed and deleted between probes; Chip ID
   It is fixed.

3) **Storage:** Atomically verified OID (temp + os.replace) in
   ``config/cartridge_id_map.json'' is written under the ``learned'' key.
   Manual user inputs (brands/printers keys) are never overwritten and
   have priority After verification, the runtime reader (cartridge_id.py) as
   It automatically GETs the same OIDs.

Timing: ``queue_discovery'' is called from the poll route (only when for IP
ID not yet found) and the single-threaded background worker prints the printers one by one
Interval processing time so that the fleet is not overloaded by SNMP.
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque

from core.collectors.cartridge_id import (
    normalize_id,
    load_id_config,
    CONFIG_PATH,
    clear_cache as clear_id_cache,
)
from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

# ─────────────────────────────────────────────────────────────────────────────
# Discovery settings
# ─────────────────────────────────────────────────────────────────────────────
STABILITY_HITS = 3          # Same number of consecutive probes for confirmation
PROBE_GAP_SECONDS = 20.0    # The distance between the probes of a printer
MAX_PASSES = 6              # Probe ceiling per printer per discovery session
COOLDOWN_SECONDS = 6 * 3600 # After failure, do not try again for 6 hours
WALK_TIMEOUT = 1.5
WALK_MAX_VARS = 48

# Candidate subfields. "standard" is for color mapping; The rest to find the series.
CANDIDATE_SUBTREES = {
    "standard": (
        "1.3.6.1.2.1.43.11.1.1.6",   # prtMarkerSuppliesDescription
    ),
    "hp": (
        "1.3.6.1.4.1.11.2.3.9.4.2.1.1.6",
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4",
    ),
    "brother": (
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.1.6",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5",
    ),
    "canon": (
        # 1602.1.11.1.2.0 = Firmware Model ID (07b2010100000000) — Same
        # For all Canon MFs, unchangeable, and of no diagnostic value.
        # Canon does not publish proprietary cartridge serials via SNMP.
    ),
    "toshiba": (
        # Toshiba does not publish proprietary cartridge serials via SNMP.
        # OIDs.1.2 and.1.3 only return firmware version and supply status
        # (unchangeable and non-unique). Switch detection through fallback
        # (toner level jump / reset pages) is done.
    ),
}

_COLOR_KEYWORDS = (
    ("black", ("black", " k)", " bk", "k toner", "مشکی")),
    ("cyan", ("cyan", " c)", "مشکی‌نما", "فیروزه")),
    ("magenta", ("magenta", " m)", "ارغوان")),
    ("yellow", ("yellow", " y)", "زرد")),
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────────
_stability_lock = threading.Lock()
_stability = {}          # ip -> {oid: {"value": str, "hits": int, "color": str}}
_queue = deque()
_queue_set = set()
_queue_lock = threading.Lock()
_last_attempt = {}       # ip -> monotonic ts (for cooldown)
_worker_started = False


# Non-serial related terms: model/brand name, status and maintenance messages.
# Real fleet data showed some devices strings like
# They return "TOSHIBA e-STUDIO3015AC" or "The Time for Periodic Maintenance".
# which are fixed (the stability gate does not reject them) but they are not cartridge identifiers.
_SERIAL_STOPWORDS = (
    "toshiba", "e-studio", "estudio", "hewlett", "laserjet", "jetdirect",
    "canon", "brother", "ricoh", "kyocera", "samsung", "epson", "xerox",
    "lexmark", "develop", "ineo", "sinium",
    "maintenance", "periodic", "error", "ready", "please", "call",
    "service", "replace", "paper", "jam", "door", "cover", "open",
    "online", "offline", "idle", "printing", "warming", "status",
    "message", "display",    "version", "firmware", "model", "serial no",
    "total", "count", "bw", "large", "small", "color",
    "print", "copy", "scan", "fax",
)



def _looks_like_serial(value) -> bool:
    """Serial filter such as: string, contains letters, not purely numeric, density of letters/numbers,
    Maximum 3 words, and no unrelated words (model/brand/status message)."""
    s = normalize_id(value)
    if not s:
        return False
    if s.isdigit():
        return False
    letters = sum(1 for ch in s if ch.isalpha())
    digits = sum(1 for ch in s if ch.isdigit())
    if letters == 0:
        return False
    # Firmware version rejection (V6.0.0.477.10, V13.0.0.520.905,...)
    if re.match(r'^[Vv]\d+(?:\.\d+)+$', s):
        return False
    # The actual string is usually a dense letter/number combination; Descriptive texts have a long distance
    if (letters + digits) < max(4, int(len(s) * 0.5)):
        return False
    # Reject common descriptive texts that the normalize filter passes through
    lowered = s.lower()
    if any(tok in lowered for tok in ("toner", "cartridge", "drum", "supply", "waste", "staple", "fuser", "kit")):
        return False
    # Up to 3 words — The actual serial is rarely multi-word
    if len(lowered.split()) > 3:
        return False
    # Unrelated words (brand/model/condition and maintenance message)
    for stop in _SERIAL_STOPWORDS:
        if stop in lowered:
            return False
    return True


def _color_from_text(text: str):
    """Infer the toner colour from a free-text label."""
    if not text:
        return None
    low = str(text).lower()
    for color, keywords in _COLOR_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return color
    return None


def _color_from_description_index(desc_map: dict, oid: str):
    """Mapping the OID index to the description row color of the same index in the standard table.

    Standard table: ``.6.<row>'' — is the index of the last component. But vendor OIDs
    They often end with ``.<index>.0''; So the last part and one left to the end
    Both are tested.
    """
    parts = oid.rsplit(".", 2)
    for idx in reversed(parts[1:]):
        desc = desc_map.get(idx)
        if desc:
            return _color_from_text(desc)
    return None


def _probe_pass(ip: str, brand: str, community: str, snmp_version=None) -> dict:
    """A discovery pass: walk branches and extract {color: (oid, value)} candidates.

    Output: {"candidates": {color: {"oid":…, "value":…}}, "desc_map": {index: text}}
    Injectable for testing via the walker parameter (default true snmp_walk).
    """
    from core.snmp.protocol import snmp_walk

    brand_key = (brand or "").lower()
    subtrees = list(CANDIDATE_SUBTREES["standard"])
    subtrees += list(CANDIDATE_SUBTREES.get(brand_key, ()))

    desc_map = {}
    candidates = {}
    for prefix in subtrees:
        try:
            rows = snmp_walk(ip, prefix, community, timeout=WALK_TIMEOUT,
                             max_vars=WALK_MAX_VARS, version=snmp_version)
        except Exception as exc:
            log.debug("cartridge-discovery walk %s %s failed: %s", ip, prefix, exc)
            continue
        if not rows:
            continue
        is_desc_table = prefix in CANDIDATE_SUBTREES["standard"]
        for oid, value in rows:
            if is_desc_table:
                idx = oid.rsplit(".", 1)[-1]
                if isinstance(value, str) and value.strip():
                    desc_map[idx] = value.strip()
                continue
            if not _looks_like_serial(value):
                continue
            color = _color_from_text(str(value)) or _color_from_description_index(desc_map, oid)
            if not color:
                # Canon MF/LBP are mono — if only one candidate → black
                if brand == "canon" and len(candidates) == 0:
                    color = "black"
                else:
                    continue
            prev = candidates.get(color)
            if prev is None or len(str(value)) > len(str(prev["value"])):
                candidates[color] = {"oid": oid, "value": str(value)}
    return {"candidates": candidates, "desc_map": desc_map}


# ─────────────────────────────────────────────────────────────────────────────
# The gate of stability
# ─────────────────────────────────────────────────────────────────────────────
def _update_stability(ip: str, candidates: dict) -> dict:
    """update the stability counter; The colors reached are returned to the threshold."""
    confirmed = {}
    with _stability_lock:
        state = _stability.setdefault(ip, {})
        for color, cand in candidates.items():
            oid, value = cand["oid"], cand["value"]
            entry = state.get(oid)
            if entry and entry.get("value") == value:
                entry["hits"] += 1
            else:
                entry = {"value": value, "hits": 1, "color": color}
                state[oid] = entry
            if entry["hits"] >= STABILITY_HITS:
                confirmed[color] = {"oid": oid, "value": value}
        # Clearing OIDs that are no longer candidates (value change → reset)
        live_oids = {cand["oid"] for cand in candidates.values()}
        for oid in list(state):
            if oid not in live_oids:
                state.pop(oid)
    return confirmed


def reset_stability(ip: str = None):
    """Clear the stability tracking state for one printer, or for all printers when ip is None."""

    with _stability_lock:
        if ip is None:
            _stability.clear()
        else:
            _stability.pop(ip, None)


# ─────────────────────────────────────────────────────────────────────────────
# Atomic storage in cartridge_id_map.json (learned section)
# ─────────────────────────────────────────────────────────────────────────────
def _save_learned(ip: str, confirmed: dict, brand: str = "", model: str = "") -> bool:
    """Write verified OIDs below learned.printers.<ip>.snmp — without touching
    Manual inputs (brands/printers). Atomic with os.replace."""
    try:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        learned = data.get("learned")
        if not isinstance(learned, dict):
            learned = {}
        printers = learned.get("printers")
        if not isinstance(printers, dict):
            printers = {}
        entry = printers.get(ip)
        if not isinstance(entry, dict):
            entry = {}
        snmp_map = entry.get("snmp")
        if not isinstance(snmp_map, dict):
            snmp_map = {}
        meta = entry.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        for color, cand in confirmed.items():
            snmp_map[color] = cand["oid"]
            meta[color] = {
                "value_at_confirm": cand["value"],
                "confirmed_at": utc_now_iso(),
                "brand": brand or None,
                "model": model or None,
            }
        entry["snmp"] = snmp_map
        entry["meta"] = meta
        printers[ip] = entry
        learned["printers"] = printers
        data["learned"] = learned

        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
        log.info("  [%s] OID شناسه‌ی کارتریج تأیید و ذخیره شد: %s", ip,
                 {c: c_["oid"] for c, c_ in confirmed.items()})
        return True
    except Exception as exc:
        log.warning("  [%s] ذخیره‌ی learned cartridge-id map ناموفق: %s", ip, exc)
        return False


def load_learned_oids(ip: str) -> dict:
    """Validated OIDs of an IP from configuration file ({color: oid}) — for UI/diagnose."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        entry = ((data.get("learned") or {}).get("printers") or {}).get(ip) or {}
        return {str(k).lower(): str(v) for k, v in (entry.get("snmp") or {}).items() if v}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# A printer discovery session (spacing probes to confirmation)
# ─────────────────────────────────────────────────────────────────────────────
def discover_for_printer(ip: str, brand: str, community: str = "public",
                         snmp_version=None, passes: int = STABILITY_HITS,
                         probe_gap: float = PROBE_GAP_SECONDS,
                         probe_fn=_probe_pass) -> dict:
    """Full discovery of a printer: maximum ``passes'' with space; Confirm → Save.

    Output: {"confirmed": {color: oid}, "candidates": {color: value}}
    Probe_fn injection for testing (no real sleep).
    """
    if not ip or brand not in ("hp", "canon", "brother", "toshiba"):
        return {"confirmed": {}, "candidates": {}}
    confirmed = {}
    last_candidates = {}
    effective_passes = max(1, passes) if probe_fn is not _probe_pass else MAX_PASSES
    for i in range(effective_passes):
        if i:
            time.sleep(probe_gap)
        try:
            res = probe_fn(ip, brand, community, snmp_version) or {}
        except Exception as exc:
            log.debug("cartridge-discovery probe %s failed: %s", ip, exc)
            continue
        candidates = res.get("candidates") or {}
        last_candidates = {c: cand["value"] for c, cand in candidates.items()}
        if not candidates:
            continue
        confirmed = _update_stability(ip, candidates)
        if confirmed:
            break
    if confirmed:
        if _save_learned(ip, confirmed, brand=brand):
            clear_id_cache()
            try:
                load_id_config(force=True)
            except Exception:
                pass
    else:
        with _stability_lock:
            _stability.pop(ip, None)
    return {"confirmed": {c: cand["oid"] for c, cand in confirmed.items()},
            "candidates": last_candidates}


# ─────────────────────────────────────────────────────────────────────────────
# background queue (throttled)
# ─────────────────────────────────────────────────────────────────────────────
def queue_discovery(ip: str, brand: str, community: str = "public", snmp_version=None):
    """adding the printer to the discovery queue; dedup with IP and cooldown."""
    global _worker_started
    if not ip or brand not in ("hp", "canon", "brother", "toshiba"):
        return False
    now = time.monotonic()
    with _queue_lock:
        if ip in _queue_set:
            return False
        last = _last_attempt.get(ip)
        if last is not None and (now - last) < COOLDOWN_SECONDS:
            return False
        _queue_set.add(ip)
        _queue.append((ip, brand, community, snmp_version))
        _last_attempt[ip] = now
        if not _worker_started:
            _worker_started = True
            t = threading.Thread(target=_worker, daemon=True, name="cartridge-discovery")
            t.start()
    return True


def _worker():
    """Background loop that processes queued cartridge discovery jobs."""
    while True:
        with _queue_lock:
            item = _queue.popleft() if _queue else None
            if item is None:
                _queue_set.clear()
        if item is None:
            time.sleep(5.0)
            continue
        ip, brand, community, snmp_version = item
        try:
            log.info("🔍 کشف OID شناسه‌ی کارتریج برای %s (%s) …", ip, brand)
            result = discover_for_printer(ip, brand, community, snmp_version)
            if result.get("confirmed"):
                log.info("  [%s] ✅ کشف موفق: %s", ip, result["confirmed"])
            else:
                log.info("  [%s] کاندیدای پایدار پیدا نشد (candidates=%s) — بعداً دوباره", ip,
                         result.get("candidates") or {})
        except Exception as exc:
            log.debug("cartridge-discovery worker error for %s: %s", ip, exc)
        # The distance between printers to SNMP fleet is not overloaded
        time.sleep(3.0)

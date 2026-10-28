# =============================================================================
# @file:         core/collectors/exporter_adapter.py
# @brief:        Converts snmp_exporter output into the collector data shape (phase G.5).
# @details:      Responsibilities: Map exporter metrics to collector fields and apply the HTTP
#                toner fallback.
#                Main API: apply_toner_http_fallback(), exporter_to_collector_shape(),
#                collect_via_exporter()
#                Side effects: network I/O
# @dependencies: internal: core.collectors.base, core.collectors.canon, core.collectors.hp,
#                core.exporter_client, core.time_utils
# @notes:        None.
# =============================================================================

"""Convert the output of `snmp_exporter` to the form of collectors - phase G, step G.5.3 (+ step H.1).

    shape = exporter_to_collector_shape("172.16.0.5", "hp", metrics)   # metrics = dict
    shape = collect_via_exporter(client, ip, name, brand, start) # or None

The output is the same dict that `core/collectors/hp.py` and `canon.py` return (the same keys and
the same default values), because the poller's fallback path already uses the same form and
UI/DB work with it.

**What this route doesn't do (and why the default flag is off):** Event logging.
`_counters_event`/YieldEngine/cartridge-change live in the collectors code and according to
The ``collectors/'' phase decision remains intact; So the constructed form is "displayable snapshot",
Not the "full cycle of poll". For this reason `USE_SNMP_EXPORTER` default is 0 and until step G.6
Enabling it is not recommended (§9 `docs/snmp-exporter-setup.md`).

Four decisions, with reasons and rejected alternatives:

1. **Formula mirroring, not data path rewriting.** Toner percentage thresholds, status
   Tray, model/serial substitution string and color condition, line by line from `collect_hp`/
   `collect_canon` are taken and `si`/`ss`/`_hp_toner_key` are imported to drift
   not possible Rejected alternative: Exporter's independent formula - in G.6 gave a "difference" number.
   Two intentional cases are not mirrored because they cannot be scraped: "total=0 suspicious" retry
   (requires SNMP rereading) and "rechargeable cartridge/`_used`" folder + EWS serial/pages
   In `collect_enhanced` (we don't mirror `cartridge_*' keys — the ``deferred'' clause in
   `docs/phase-h-decisions.md`). **Step H.1:** Toner web scrap is active in this path, but with
   import from `core.collectors.{hp,canon}` and **zero copies of scraper** (owner's decision) — scraper there
   It stays and the mirror just calls. merge comes from the single `_merge_scraped`, which is *just deleg* and
   The trigger for both brands is the same: "SNMP has no number for any real toner"; The divergence of the mirror with
   `collect_canon:325` (which always scrips) has an explicit test: §H.1.
2. **`ignore_values` means "the device will not report this", not "zero".** Sentinels
   `-2`/`-3` are only discarded in the same metrics as `monitoring/snmp.yml` for them
   ``# ignore_values:'' and the ``default'' of the collector sits in their place; For example
   `toner_remain_2 = -2` → `level=None/status=unknown`, just like the direct path.
   The uncommented metric remains intact even with the value `-2'. List of 31 metrics in
   is `IGNORED_METRICS` and `tests/test_exporter_adapter.py` matches it with `snmp.yml` itself
   compares Additional aware divergence: `prtInputCurrent = -2` in HP path
   is `no_sensor`; Here the sentinel is discarded and becomes ``empty'' - it has an explicit test.
3. **Unmirrored brands → `None`.** `SUPPORTED_BRANDS = ("hp", "canon")`; for
   The `toshiba`/`brother`/`sensor` data exporter does not give the complete form of the collector
   (`printer_toshiba' has 63 metrics with text warning messages and tray 3/6 that in
   ``printer_std'' is not) and guessing the rest means fake data. poller in this case to the path
   It comes straight back.

4. **From step H.2, ``ExporterCircuitOpen'' at *debug** level (other errors are warning): with flag on
   An otherwise off printer would generate a new alert every 60 seconds, as the failure count and "how much
   wait" is in the client itself (`core/exporter_client.py`) and `query_brand` before each
   The request says "no turn". This file does not count state; It only means the same as "zero":
   `return None' to make the poller write the full direct path.

No numbers here come from real metrics: the fixtures are handwritten and the exporter is in this sandbox
Not executed (`load_test_sandbox: not_attempted` in `monitoring/snmp-manifest.json`).
"""
from __future__ import annotations

import logging
import re
import time

from core.collectors.base import si, ss
from core.collectors.canon import _scrape_canon_toners
from core.collectors.hp import _hp_toner_key, _scrape_hp_toners
from core.exporter_client import (DEFAULT_AUTH, ExporterCircuitOpen, ExporterClient, ExporterError,
                             label_value, metric_value)
from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

#: Brands whose output shape is mirrored (the rest → None to go straight).
SUPPORTED_BRANDS = ("hp", "canon")

#: Metrics that have the comment `# ignore_values: ["-2", "-3"]` in `monitoring/snmp.yml`.
#: 8 (printer_std) + 3 (printer_hp) + 2 (printer_canon) + 2 (printer_brother)
#: + 16 (printer_toshiba) = 31 — the test compares the same file with the file.
IGNORED_METRICS = frozenset({
    "toner_max_1", "toner_max_2", "toner_remain_1", "toner_remain_2",
    "tray1_cap", "tray2_cap", "tray1_level", "tray2_level",
    "general_oids", "general_oids_2", "hp_npcl_pct_oid",
    "canon_alternate_oids", "canon_alternate_oids_2",
    "brother_black", "brother_drum_oid",
    "toner_cyan_usage", "toner_magenta_usage", "toner_yellow_usage", "toner_black_usage",
    "toner_magenta_status", "toner_cyan_status", "toner_yellow_status", "toner_black_status",
    "tsh_t1_level", "tsh_t2_level", "tray3_level", "tray6_level",
    "tsh_cyan_st", "tsh_mag_st", "tsh_yel_st", "tsh_blk_st",
})
IGNORED_SENTINELS = (-2.0, -3.0)


# ── Reading a metric from parsed dict ─────────────────────────────────────
def _num(metrics: dict, name: str, default):
    """metric number; If `default` was not there, it was unlimited, or had a sentinel of ignore."""
    raw = metric_value(metrics, name)
    if raw is None:
        return default
    if name in IGNORED_METRICS and raw in IGNORED_SENTINELS:
        return default
    return raw


def _int(metrics: dict, name: str, default):
    """Integer number; The `si()` of the collector itself does the same conversion (and the error log)."""
    raw = _num(metrics, name, None)
    if raw is None:
        return default
    return si(raw, default)


def _text(metrics: dict, name: str, default: str = "") -> str:
    """Text metric: the value of the label of the same name (collector.go behavior), normalized by `ss()`."""
    return ss(label_value(metrics, name, None), default)


def _uptime_str(ticks) -> str:
    """collector uptime string: sysUptime in 1/100 second → "1d 01:40"."""
    us = int(ticks or 0) // 100
    return f"{us // 86400}d {(us % 86400) // 3600:02d}:{(us % 3600) // 60:02d}"


# Components of the output shape
def _device(brand: str, metrics: dict) -> dict:
    """chain model/serial/firmware, mirror `collect_hp`/`collect_canon`."""
    if brand == "hp":
        model = _text(metrics, "hp_model", "")
        if not model:
            model = _text(metrics, "model_mib", "")
        if not model:
            match = re.search(r"PID:([^,]+)", _text(metrics, "sys_descr", ""))
            model = match.group(1).strip() if match else "Unknown"
        serial = _text(metrics, "hp_serial", "") or _text(metrics, "serial_mib", "N/A")
        firmware = _text(metrics, "hp_firmware", "N/A")
    else:  # canon
        model = _text(metrics, "canon_model", "")
        if not model:
            model = _text(metrics, "model_mib", "")
        if not model:
            model = _text(metrics, "sys_descr", "N/A")
        serial = _text(metrics, "canon_serial", "") or _text(metrics, "serial_mib", "N/A")
        firmware = _text(metrics, "canon_firmware", "N/A")
    return {
        "model": ss(model, "Unknown"),
        "serial": ss(serial, "N/A"),
        "firmware": ss(firmware, "N/A"),
        "uptime_str": _uptime_str(_int(metrics, "sys_uptime", 0)),
    }


def _toners(brand: str, metrics: dict) -> dict:
    """toners from `prtMarkerSuppliesTable`; The branch of each brand is exactly its own code.

    `printer_canon' does not have any toner metrics in the spec, so it is the only source for canon
    MIB is standard; ``canon.py'' HTTP-scrapes are not performed here (§2 above).
    """
    toners: dict = {}
    if brand == "hp":
        for idx in (1, 2, 3, 4):
            t_name = _text(metrics, f"toner_name_{idx}", "")
            t_max = _int(metrics, f"toner_max_{idx}", -1)
            t_rem = _int(metrics, f"toner_remain_{idx}", -2)
            if t_max == -1 and t_rem == -2:
                continue
            if not t_name:
                t_name = "Black Toner" if idx == 1 else f"Toner {idx}"
            key = _hp_toner_key(t_name) or ("black" if idx == 1 else f"toner_{idx}")
            if t_rem == -2 or t_max <= 0:
                pct, status = None, "unknown"
            elif t_rem <= 0:
                pct, status = 0, "empty"
            else:
                pct = round(t_rem / t_max * 100)
                status = "ok" if pct > 25 else ("low" if pct > 10 else "critical")
            toners[key] = {"level": pct, "status": status, "name": t_name,
                           "remaining": t_rem, "max": t_max}
        if not toners:
            toners["black"] = {"level": None, "status": "unknown",
                               "name": "Black Toner", "remaining": -1, "max": -1}
        return toners

    for idx, key, default_name in ((1, "black", "Black Toner"), (2, "cyan", "Cyan Toner"),
                                   (3, "magenta", "Magenta Toner"), (4, "yellow", "Yellow Toner")):
        t_name = _text(metrics, f"toner_name_{idx}", default_name)
        t_max = _int(metrics, f"toner_max_{idx}", -1)
        t_rem = _int(metrics, f"toner_remain_{idx}", -1)
        if t_max == -1 and t_rem == -1:
            toners[key] = {"level": None, "status": "unknown", "name": default_name,
                           "remaining": -1, "max": -1, "source": "snmp_unavailable"}
            continue
        if t_rem < 0 or t_max <= 0:
            pct, status = None, "unknown"
        else:
            pct = round(t_rem / t_max * 100)
            status = ("empty" if pct == 0 else "critical" if pct <= 10
                      else "low" if pct <= 25 else "ok")
        toners[key] = {"level": pct, "status": status, "name": t_name or default_name,
                       "remaining": t_rem, "max": t_max, "source": "snmp"}
    return toners


# ── H.1 — Toner web scrap in exporter path (fallback/priority, same as collectors) ─────
#: legacy timeouts: `collect_canon` gives explicit 3.0; ``collect_enhanced'' for HP
#: calls `_scrape_hp_toners(ip)` with a default of 4.0. These are the two numbers here.
CANON_HTTP_TIMEOUT = 3.0
HP_HTTP_TIMEOUT = 4.0

#: canon's four CMYK slots — same as `collect_canon''s `toner_map' (for HTTP priority).
_CANON_TONER_MAP = ((1, "black", "Black Toner"), (2, "cyan", "Cyan Toner"),
                    (3, "magenta", "Magenta Toner"), (4, "yellow", "Yellow Toner"))


def _toner_level_missing(toners: dict) -> bool:
    """"We don't have any numbers for any actual toners?" — rule `core/enhanced_collector.py:148`.

    A necessary and intentional adaptation: there is the raw number in `raw_level` (the form of `collect_enhanced`) and
    Here in `level` (the form `collect_hp` has no `raw_level` - its addition means the plus key
    in the snapshot and a dummy `only_in_exporter' line in comparison G.6). "drum/opc counted
    They will not" comes from the same place: a healthy drama should not prevent fallback.
    """
    toner_keys = [k for k in toners.keys() if k not in ("drum", "opc")]
    if not toner_keys:
        return True
    return not any(toners[k].get("level") is not None for k in toner_keys)


def _merge_scraped_toners(toners: dict, scraped: dict, source: str) -> int:
    """Merging web scraping result into `toners` — rule `core/enhanced_collector.py:159`.

    The two `raw_level' lines are intentionally omitted (explanation above). The rest of the rule is the same: "_" keys are given
    and are rejected, bool is not a counter, and the "new toner" branch is the same as name/remaining/max and
    Creates empty/critical/low/ok thresholds. Return count = number of toners that
    updated (the same thing that legacy logs).
    """
    merged = 0
    for color_key, pct in scraped.items():
        if color_key.startswith("_") or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        pct = int(pct)
        t = toners.get(color_key)
        if not t:
            toners[color_key] = {
                "level": pct, "status": "unknown",
                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                "source": source,
            }
            merged += 1
            continue
        if t.get("level") is None:
            t["level"] = pct
            t["source"] = source
            if pct == 0:
                t["status"] = "empty"
            elif pct <= 10:
                t["status"] = "critical"
            elif pct <= 25:
                t["status"] = "low"
            else:
                t["status"] = "ok"
            merged += 1
    return merged


def _prefer_scraped_canon_toners(toners: dict, scraped: dict) -> int:
    """HTTP priority over SNMP, line by line from `collect_canon:329-347`.

    In the code we have two canon rules: `collect_canon:325` ("Always scrape, HTTP wins",
    `source="http_scrape"`) and `enhanced_collector:1199` (``only when we have no `raw_level''');
    `source="canon_remote_ui"`). The mirror takes the first one, because the shape of this file is from
    `collect_canon' is derived and `source' must be the same as the UI/DB for that figure
    they know

    canon is not a "fallback": `collect_canon' will **always** call the script and if for a key
    number, same key with `remaining=None`/`max=100`/`source="http_scrape"` and
    Overwrites the default brand name. The mirror does the same thing; The "only when SNMP
    is incomplete" would diverge for the canon — but the H.1 step program called for the same uniform rule,
    So the mirror first asks `_toner_level_missing' and *then* does this rewriting; The difference
    Remaining (when we script) in `apply_toner_http_fallback` and `docs/phase-h-decisions.md`
    §H.1 is registered and locked with testing.
    """
    merged = 0
    for _idx, key, default_name in _CANON_TONER_MAP:
        if key not in scraped:
            continue
        level = scraped[key]
        if level is None:
            continue
        if level == 0:
            status = "empty"
        elif level <= 10:
            status = "critical"
        elif level <= 25:
            status = "low"
        else:
            status = "ok"
        toners[key] = {"level": level, "status": status, "name": default_name,
                       "remaining": None, "max": 100, "source": "http_scrape"}
        merged += 1
    return merged


def _merge_scraped(toners: dict, brand: str, scraped: dict) -> int:
    """The only merge in this file — **only delegates** (decision of step H.1, paragraph 1-3).

    `collect_via_exporter`/`apply_toner_http_fallback` never direct `_merge_scraped_toners`
    or not calling `_prefer_scraped_canon_toners`; Choosing the rule here and just by looking
    It is done to ``brand''. The following two rules are still **faithful copy** (why copy and not import):
    `docs/phase-h-decisions.md` §H.1) and their text with
    Locked `tests/test_exporter_adapter.py::test_merge_functions_are_faithful_copies'.

    HP: Filling empty fields + ladder of `0/10/25` (`source="hp_web"`).
    canon: embed full HTTP record (`remaining=None`/`max=100`/`source="http_scrape"`),
    which is line by line `collect_canon:329-347`.
    """
    if brand == "canon":
        return _prefer_scraped_canon_toners(toners, scraped)
    return _merge_scraped_toners(toners, scraped, "hp_web")


def apply_toner_http_fallback(shape: dict, brand: str, ip: str) -> int:
    """Completion of `shape["toners"]` with the device's own web scrap. Returns the number of updates.

    - **Where called:** Only `collect_via_exporter` (runtime path). self
      `exporter_to_collector_shape` remains pure — otherwise `tools/compare_snmp_sources.py`
      which uses the same function for offline comparison, it suddenly hit the network.
    - **no exception thrown:** legacy also has `try/except' here (web error only
      is debug and the SNMP numbers remain in place).
    - **merge:** only through `_merge_scraped(...)`; This function does not overwrite anything.
    - **trigger, both brands:** only when `_toner_level_missing(toners)` — uniform rule
      Program H.1. ``collect_canon`' but it **always** hits the script (`canon.py:335`), so on
      The healthy canon has two different paths; Pinned Divergence:
      `test_canon_divergence_from_the_collector_is_pinned`.
    - EWS `_used`/serial is not mirrored (its keys are not in G.5 12-bit contract - TD-017) and
      Toshiba/Brother's mirror is also TD-018. The thing that differs between the two brands is the shape
      Rewrite* is: canon embeds the HTTP record, HP fills in the empty fields.
    """
    toners = shape.get("toners")
    if not isinstance(toners, dict):
        return 0
    if not _toner_level_missing(toners):    # H.1 uniform rule: both brands only when SNMP
        return 0                            # No number for any actual toner (proprietary program)
    try:
        if brand == "canon":
            scraped = _scrape_canon_toners(ip, timeout=CANON_HTTP_TIMEOUT)
        else:
            scraped = _scrape_hp_toners(ip, timeout=HP_HTTP_TIMEOUT)
        if not scraped:
            return 0
        return _merge_scraped(toners, brand, scraped)
    except Exception as exc:            # The same obsession with legacy: no matter what the web wants, snapshot will not die
        log.debug("exporter: وب‌اسکرپِ تونرِ %s انجام نشد (%s) — اعدادِ SNMP دست‌نخورده", ip, exc)
        return 0


def _trays(brand: str, metrics: dict) -> list[dict]:
    """trays from `prtInputTable`; The default label of each brand from its own code."""
    slots = ((1, "Tray 1"), (2, "Tray 2")) if brand == "hp" else ((1, "Bypass"), (2, "Tray 1"))
    trays = []
    for idx, label in slots:
        cap = _int(metrics, f"tray{idx}_cap", 0)
        lvl = _int(metrics, f"tray{idx}_level", -9)
        nm = _text(metrics, f"tray{idx}_name", label) or label
        if cap == 0 and lvl == -9:
            continue
        if lvl == -2:
            status = "no_sensor"
        elif brand == "canon":
            status = "empty" if lvl <= 0 else None
        elif lvl == -3 or lvl <= 0:
            status = "empty"
        else:
            status = None
        if status is None:
            if cap > 0:
                pct = round(lvl / cap * 100)
                status = "low" if pct <= 25 else ("medium" if pct <= 75 else "ok")
            else:
                status = "unknown"
        trays.append({"name": nm, "level": lvl, "capacity": cap, "status": status})
    return trays


def _alerts(metrics: dict) -> list[dict]:
    """The only warning the current spec gives: open door (`prtCoverStatus …1.1`).

    `printer_canon' also has no metric, so the same rule is common for both brands.
    """
    cover = _int(metrics, "cover_status", 4)
    return [] if cover == 4 else [{"message": "درب پرینتر باز است", "code": cover}]


def _counters(brand: str, metrics: dict, toners: dict) -> dict:
    """Build the counter values for a brand from exporter metrics and toner values."""
    if brand == "hp":
        return _counters_hp(metrics, toners)
    return _counters_canon(metrics)


def _counters_hp(metrics: dict, toners: dict) -> dict:
    """Build HP counter values from exporter metrics."""
    total = _int(metrics, "bro_total", 0)
    color_print = _int(metrics, "hp_print_color", -1)
    copy_mono = _int(metrics, "hp_copy_mono", -1)
    copy_color = _int(metrics, "hp_copy_color", -1)
    fax = _int(metrics, "hp_fax", -1)
    copy_total = max(copy_mono, 0) + max(copy_color, 0)
    has_color_toner = any(key in ("cyan", "magenta", "yellow") for key in toners)
    if color_print >= 0:
        full_color, black_white = color_print, max(0, total - color_print)
    elif has_color_toner:
        full_color = black_white = None
    else:
        full_color, black_white = None, total
    return {
        "total": total,
        "full_color": full_color,
        "black_white": black_white,
        "printer": total,
        "copy": copy_total if copy_total > 0 else None,
        "fax": fax if fax >= 0 else None,
    }


def _counters_canon(metrics: dict) -> dict:
    """Build Canon counter values from exporter metrics."""
    total_std = _int(metrics, "bro_total", -1)
    color_std = _int(metrics, "bro_color", -1)
    bw_std = _int(metrics, "bw_std", -1)
    total_mf = _int(metrics, "canon_total", -1)
    copy_mf = _int(metrics, "canon_copy", -1)
    printer_mf = _int(metrics, "canon_print", -1)
    total_lbp = _int(metrics, "canon_lbp_total", -1)
    color_lbp = _int(metrics, "canon_lbp_color", -1)
    alt_total = _int(metrics, "alt_total", -1)
    alt_copy = _int(metrics, "alt_copy", -1)
    alt_print = _int(metrics, "alt_print", -1)

    if color_std >= 0 and total_std >= 0 and color_std >= total_std:
        color_std = -1  # canon.py: color ≥ total ⇒ invalid color data
    is_mf = total_mf >= 0 or alt_total >= 0
    if not is_mf and "MF" in _text(metrics, "canon_model", "").upper():
        is_mf = True  # canon.py: Detect MF from model name when it has no MF counter

    if is_mf:
        if total_mf < 0 and alt_total >= 0:
            total_mf, copy_mf, printer_mf = alt_total, alt_copy, alt_print
        total = total_mf if total_mf >= 0 else (total_std if total_std >= 0 else 0)
        copy_ = copy_mf if copy_mf >= 0 else -1
        printer = printer_mf if printer_mf >= 0 else -1
        if copy_ < 0 and printer < 0 and total > 0:
            printer, copy_ = total, 0
    else:
        total = total_lbp if total_lbp >= 0 else (total_std if total_std >= 0 else 0)
        if total == 0:
            alt_lbp = _int(metrics, "alt_lbp_total", -1)
            if alt_lbp >= 0:
                total = alt_lbp
        copy_, printer = -1, total
    if total < 0:
        total = 0

    full_color = black_white = None
    if is_mf:
        if 0 <= color_std < total:
            full_color = color_std
        if 0 <= bw_std <= total:
            black_white = bw_std
    else:
        if 0 <= color_lbp < total:
            full_color = color_lbp
        elif 0 <= color_std < total:
            full_color = color_std
        if 0 <= bw_std <= total:
            black_white = bw_std
    if black_white is None and full_color is not None and total > 0:
        black_white = max(0, total - full_color)
    if full_color is None and black_white is None and total > 0:
        black_white = total

    return {
        "total": total,
        "full_color": full_color,
        "black_white": black_white,
        "printer": printer if printer >= 0 else None,
        "copy": copy_ if (is_mf and copy_ >= 0) else None,
        "fax": None,
    }


# ── Gate "Is the data meaningful?" ───────────────────────────┄
_ESSENTIAL = ("bro_total", "sys_uptime", "sys_descr", "model_mib")
_VENDOR_MODEL = {"hp": ("hp_model", "hp_print_color"), "canon": ("canon_model", "canon_total")}


def _has_signal(brand: str, metrics: dict) -> bool:
    """If it is neither a standard metric nor a vendor, the exporter's response is useless."""
    if not metrics:
        return False
    names = set(metrics)
    return bool(names & set(_ESSENTIAL)) or bool(names & set(_VENDOR_MODEL.get(brand, ())))


def exporter_to_collector_shape(ip: str, brand: str, metrics: dict, *,
                                name: str = "", start: float | None = None) -> dict | None:
    """`dict` of the form `collect_{hp,canon}`, or `None` if not constructable.

    The output keys are exactly the return keys of `collect_hp` (`ip`, `name`, `brand`,
    `online`, `last_poll`, `poll_ms`, `device`, `counters`, `paper_sizes`, `trays`,
    `toners`, `alerts`) — no new key, so consumers have something for ``exporter mode''
    do not have to
    """
    brand = (brand or "").strip().lower()
    if brand not in SUPPORTED_BRANDS or not _has_signal(brand, metrics):
        return None
    toners = _toners(brand, metrics)
    elapsed = int((time.time() - start) * 1000) if start is not None else 0
    return {
        "ip": ip,
        "name": name or ip,
        "brand": brand,
        "online": True,
        "last_poll": utc_now_iso(),
        "poll_ms": elapsed,
        "device": _device(brand, metrics),
        "counters": _counters(brand, metrics, toners),
        "paper_sizes": {},
        "trays": _trays(brand, metrics),
        "toners": toners,
        "alerts": _alerts(metrics),
    }


#: Non-mirrored brands we've warned about once — one per poll cycle
#: Repeated warning is not new news (the legacy route is healthy for these brands) and the flood of logs.
#: makes; H.2 takes the per-device state here.
_WARNED_UNSUPPORTED: set = set()


def collect_via_exporter(client: ExporterClient, ip: str, name: str, brand: str,
                         start: float | None = None, auth: str | None = None) -> dict | None:
    """It reads the modules of the brand and creates the shape of the collector. ``None'' ⇒ "Go back to the direct path".

    No exception is thrown: `ExporterError` (and any other unexpected errors) to `None`
    It is converted and logged, because the exporter is a **possibility** in this phase, not a dependency of poll.
    """
    norm = (brand or "").strip().lower()
    if norm not in SUPPORTED_BRANDS:
        key = (norm or "?", ip)
        if key not in _WARNED_UNSUPPORTED:   # One alert per (brand, ip) — not every 60 seconds
            _WARNED_UNSUPPORTED.add(key)
            log.warning("exporter: برندِ «%s» آینه‌سازی نشده ⇒ هیچ کوئری‌ای به exporter "
                        "نمی‌رود و مسیرِ مستقیم می‌نویسد (محدودهٔ H.1 = hp/canon؛ "
                        "mirrorِ toshiba/brother = TD-018)", key[0])
        return None
    try:
        metrics = client.query_brand(ip, brand, auth or DEFAULT_AUTH)
    except ExporterCircuitOpen as exc:      # Step H.2 — "It's not our turn" not "fault": quietly
        log.debug("exporter: %s در backoff است (%s) — این چرخه مسیرِ مستقیم می‌نویسد", ip, exc)
        return None
    except ExporterError as exc:
        log.warning("exporter: خواندنِ %s نشد (%s) — مسیرِ مستقیم", ip, exc)
        return None
    except Exception as exc:  # anything unexpected; poller must not die
        log.warning("exporter: خطای غیرمنتظره برای %s: %s — مسیرِ مستقیم", ip, exc)
        return None
    shape = exporter_to_collector_shape(ip, brand, metrics, name=name, start=start)
    if shape is None:
        log.warning("exporter: پاسخِ %s برای شکلِ «%s» کافی نبود — مسیرِ مستقیم", ip, brand)
        return None
    merged = apply_toner_http_fallback(shape, shape["brand"], ip)
    if merged:
        log.info("exporter: %s تونرِ %s از وب‌اسکرپِ دستگاه تکمیل/تصحیح شد (brand=%s)",
                 merged, ip, shape["brand"])
    return shape

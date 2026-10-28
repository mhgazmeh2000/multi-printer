# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/collectors/cartridge_id.py
# @brief:        Reads the unique cartridge ID to detect definitive cartridge replacement.
# @details:      Responsibilities: Normalise cartridge IDs, load the ID-OID map and cache per-
#                printer identity data.
#                Main API: normalize_id(), load_id_config(), get_cartridge_identity_data(),
#                clear_cache()
#                Side effects: network I/O, file I/O
# @dependencies: internal: core.collectors.base, core.snmp.protocol
# @notes:        None.
# =============================================================================

"""Reading "Unique Cartridge ID" to definitively identify cartridge replacement (CARTRIDGE_CHANGED).

Three sources of evidence, in order of strength:

1) **Chip ID / Cartridge serial number** (Chip ID / Supply Serial) — definite:
   Each cartridge has a different ID; Its change means that the cartridge has been changed.
   Sources:
     - Unique SNMP/OID of each model: read from `config/cartridge_id_map.json`
       can be (config-driven pattern) without changing the code and by confirming diagnose on
       Activate real fleet; Example: cartridge_id_map.example.json).
     - HP Web Panel (EWS): Supplies/Status pages of LaserJet models usually row
       "Serial Number" of the cartridge is displayed for original cartridges.

2) "Pages printed with this cartridge" counter
   supply) — quasi-definite: this number is only bullish; Significant drop (for example, 32,000 → 15)
   It means that the cartridge has been changed. Available on EWS for HP models and even for cartridges
   Compatible (no valid serial) works.

3) nothing found → empty dictionary; The event engine goes to the level jump fallback
   (The same existing route, intact).

Honest tip: Many devices (especially Canon i-SENSYS / Brother / Toshiba
e-STUDIO) do not publish the chip ID anywhere publicly. for those
`tools/diagnose_printer.py` explores the candidate cases and after verification on
The actual device OID is enabled in the configuration file — without changing the code.

Performance: Result is cached per IP with TTL (success 10 minutes / failure 60 minutes)
so that the fleet's 60-second pacing overhead remains close to zero.
"""
import json
import logging
import os
import re
import time


log = logging.getLogger("PrinterMonitor")

_TTL_FOUND = 180.0    # ID found: 3 minutes cache (maximum delay of switching detection from ID path)
_TTL_MISS = 900.0     # Nothing found: 15 minutes back-off
_cache = {}           # ip -> (expire_ts, result_dict)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "cartridge_id_map.json")


# ─────────────────────────────────────────────────────────────────────────────
# ID validation/normalization tools
# ─────────────────────────────────────────────────────────────────────────────
_BAD_TOKENS = {
    "", "-", "--", "0", "00", "000", "0000", "000000", "n/a", "na", "none",
    "null", "unknown", "not available", "notAvailable".lower(), "(not available)",
    "serial", "sn", "s/n", "serialnumber", "number", "no", "num", "id",
}


def normalize_id(value):
    """Converts the SNMP/Web value to a trusted ID; Invalid → None.

    Rules: 4 to 48 characters, at least one letter/number, allowed characters, and not in the list
    Meaningless tokens. Many devices have strings like "Unknown" in the absence of serial numbers.
    or return "0" which should not be used as chip ID.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("latin-1", "replace")
        except Exception:
            return None
    s = str(value).strip().strip("\x00").strip()
    if not (4 <= len(s) <= 48):
        return None
    if s.lower() in _BAD_TOKENS:
        return None
    if s.lower().startswith(("unknown", "not avail")):
        return None
    if not re.search(r"[A-Za-z0-9]", s):
        return None
    if not re.fullmatch(r"[A-Za-z0-9" + re.escape("-_/:.,#()[] ") + r"]+", s):
        return None
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Settings (config-driven OID map)
# ─────────────────────────────────────────────────────────────────────────────
_config_cache = {"ts": 0.0, "data": None}
_CONFIG_TTL = 300.0


def load_id_config(force: bool = False):
    """Reads a map of verified OIDs; The file is optional.

    Structure (example in cartridge_id_map.example.json):
      {
        "brands":   {"hp": {"snmp": {"black": "1.3.6.1...."}}, ...},
        "printers": {"172.16.8.52": {"snmp": {"black": "1.3.6.1...."}}},
        "learned":  {"printers": {"10.0.0.5": {"snmp": {"black": "1.3.6.1...."},
                                                 "meta": {...}}}},
        "hp_web": true
      }
    The ``learned'' section is automatically populated by cartridge_discovery.py;
    Final priority: printers (manual) > brands (manual) > learned (automatic).
    """
    now = time.time()
    if not force and _config_cache["data"] is not None and now - _config_cache["ts"] < _CONFIG_TTL:
        return _config_cache["data"]
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except FileNotFoundError:
        data = {}
    except Exception as exc:
        log.warning("cartridge_id_map.json خوانده نشد: %s", exc)
        data = {}
    _config_cache["ts"] = now
    _config_cache["data"] = data
    return data


def _snmp_oids_for(ip: str, brand: str, cfg: dict):
    """Combination of brand map + device override + automatic learned part.

    Priority (the last one sits and wins): learned < brands < printers.
    That is, user's manual input always takes precedence over the auto-discovered OID.
    """
    out = {}
    try:
        learned = (((cfg.get("learned") or {}).get("printers") or {}).get(ip) or {}).get("snmp") or {}
        out.update({str(k).lower(): str(v) for k, v in learned.items() if v})
        out.update(((cfg.get("brands") or {}).get(brand or "") or {}).get("snmp") or {})
        out.update(((cfg.get("printers") or {}).get(ip) or {}).get("snmp") or {})
    except Exception:
        pass
    return {str(k).lower(): str(v) for k, v in out.items() if v}


def _read_snmp_ids(ip: str, oids_by_color: dict, community: str, snmp_version=None, timeout: float = 1.2) -> dict:
    """Reading identifiers from SNMP according to configuration map; Blank spaces are omitted."""
    ids = {}
    if not oids_by_color:
        return ids
    import core.snmp.protocol as _p
    g = (lambda oid: _p.snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout))
    for color, oid in oids_by_color.items():
        try:
            sid = normalize_id(g(oid))
        except Exception:
            sid = None
        if sid:
            ids[color] = sid
            log.info("  [%s] شناسه کارتریج %s از SNMP (%s): %s", ip, color, oid, sid)
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Read from HP web panel (EWS): Cartridge serial + "Pages printed with this cartridge"
# ─────────────────────────────────────────────────────────────────────────────
_HP_URLS = (
    "http://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
    "https://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
    "http://{ip}/DevMgmt/ConsumableConfigDyn.xml",
    "https://{ip}/DevMgmt/ConsumableConfigDyn.xml",
    "http://{ip}/hp/device/info_suppliesStatus.html",
    "http://{ip}/info_suppliesStatus.html",
)

# Serial: near the Serial/Serial Number tag (in HTML and XML)
# Note: The value may come after a few closing/opening tags: <td>Serial:</td><td>VAL</td>
# And in some EWS the label is split: Serial</span><span>Number</span> — so after
# Skipping tags also skips an optional token Number/No/# so that the tag itself is not taken
# (Actual fleet bug: "Number" value was being recorded as serial).
_HP_SERIAL_PATTERNS = (
    re.compile(
        r"Serial\s*(?:Number|No\.?|#)?\s*[:：]?\s*(?:</\w+>\s*)?(?:<[^>]*>\s*){0,3}"
        r"(?:Number|No\.?|#)?\s*[:：]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9\-]{3,30})", re.IGNORECASE),
    re.compile(r"<[A-Za-z0-9:]*SerialNumber>\s*([A-Za-z0-9][A-Za-z0-9\-]{3,30})\s*<", re.IGNORECASE),
)
# "Pages printed with this cartridge" (English EWS; XML equivalent)
_HP_SUPPLY_PAGES_PATTERNS = (
    re.compile(r"Pages\s+printed\s+with\s+this\s+supply[^0-9]{0,40}([0-9][0-9,]{0,12})", re.IGNORECASE),
    re.compile(r"PagesWithSupply[^0-9]{0,20}([0-9][0-9,]{0,12})", re.IGNORECASE),
    re.compile(r"<[A-Za-z0-9:]*PagesPrintedWithSupply>\s*([0-9][0-9,]{0,12})\s*<", re.IGNORECASE),
)


def _first_int(text: str):
    """Return the first integer found in a text, or None."""
    if not text:
        return None
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _search_serial(text: str):
    """Serial search with patterns; The first valid value (passing normalize) is returned."""
    for pat in _HP_SERIAL_PATTERNS:
        m = pat.search(text)
        if m:
            sid = normalize_id(m.group(1))
            if sid:
                return sid
    return None


def _read_hp_web_ids(ip: str, timeout: float = 3.5):
    """Read HP cartridge serial/counter from EWS; There was no → empty dictionary.

    Note: The current fleet of HPs is mono; The result sits on the ``black'' key. for
    Color models (if added later) should be added per-color segmentation.

    The first series is searched on "plain text" (tags → spaces) — on raw HTML,
    The pattern backtracking caused the word "Number" itself from the label to split
    Serial</span><span>Number to be taken as serial (actual fleet bug).
    The XML template is applied directly to the raw text.
    """
    from core.collectors.base import fetch_first_web_page

    used, html = fetch_first_web_page(ip, [u.format(ip=ip) for u in _HP_URLS], timeout)
    if not html:
        return {}
    ids, pages = {}, {}
    plain = re.sub(r"<[^>]+>", " ", html)
    sid = _search_serial(plain) or _search_serial(html)
    if sid:
        ids["black"] = sid
    for pat in _HP_SUPPLY_PAGES_PATTERNS:
        m = pat.search(html)
        if m:
            val = _first_int(m.group(1))
            if val is not None:
                pages["black"] = val
                break
    if ids or pages:
        log.info("  [%s] شواهد EWS کارتریج HP: سریال=%s صفحات‌باکارتریج=%s",
                 ip, ids.get("black"), pages.get("black"))
        return {"ids": ids, "supply_pages": pages,
            "identity_type": {color: "serial" for color in ids}} if (ids or pages) else {}


# ─────────────────────────────────────────────────────────────────────────────
# Reading from Canon / Brother / Toshiba web panel
# ─────────────────────────────────────────────────────────────────────────────
def _read_other_web_ids(ip: str, brand: str, timeout: float = 3.0):
    """Canon/Brother/Toshiba: The chip serial is usually not on public pages.

    Most i-SENSYS remote UIs and Brother status pages only show the toner level
    show This reader is deliberately conservative: just the obvious example
    Removes "Serial Number"; In its absence, it does not return anything (level fallback
    still covers). Actual verification is done with diagnose.
    """
    from core.collectors.base import fetch_first_web_page

    urls = []
    if brand == "canon":
        urls = [f"http://{ip}/", f"https://{ip}/"]
    elif brand == "brother":
        urls = [f"http://{ip}/general/status.html", f"http://{ip}/"]
    elif brand in ("toshiba", "toshiba_tec"):
        urls = [f"http://{ip}/?MAIN=DEVICE", f"http://{ip}/"]
    if not urls:
        return {}
    used, html = fetch_first_web_page(ip, urls, timeout)
    if not html:
        return {}
    ids = {}
    m = re.search(
        r"Serial\s*(?:Number|No\.?|#)?\s*[:：]?\s*(?:</\w+>\s*)?(?:<[^>]*>\s*){0,3}"
        r"([A-Za-z0-9][A-Za-z0-9\-]{3,30})", html, re.IGNORECASE)
    if m:
        sid = normalize_id(m.group(1))
        if sid:
            ids["black"] = sid
            log.info("  [%s] شناسه کارتریج (%s web): %s", ip, brand, sid)
    return {"ids": ids, "supply_pages": {}} if ids else {}


# ─────────────────────────────────────────────────────────────────────────────
# Alternative ID for printers without chip ID
# ─────────────────────────────────────────────────────────────────────────────
def _read_fallback_identity(ip: str, brand: str, community: str = "public",
                            snmp_version=None) -> dict:
    """Returns an alternative signal for printers that do not have a dedicated chip ID.

    Brothr: Toner Replace Count (+1 each time)
    Old HP: Cartridge part number from Printer-MIB (static but useful)
    Canon LBP: Cartridge model from Printer-MIB

    Output similar to get_cartridge_identity_data: {"ids": {}, "supply_pages": {}, "source": str}
    """
    ids, pages, source = {}, {}, ""
    # Read from the module (not local import) so that monkeypatch works in tests
    import core.snmp.protocol as _snmp_mod
    _sg = _snmp_mod.snmp_get_with_fallback

    if brand == "brother":
        # Toner Replace Count: +1 each time toner is replaced
        # OID: 1.3.6.1.4.1.2435.2.3.9.4.2.1.5.1.1.6.0
        val = _sg(ip, "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.1.1.6.0", community,
                  version=snmp_version, timeout=2.0)
        if val is not None:
            try:
                count = int(str(val).strip())
                # Save as generation counter — motor CARTRIDGE_CHANGED
                # It detects by increasing this value
                ids["black"] = f"toner_gen:{count}"
                source = "brother_snmp_gen"
                log.info("  [%s] Brother toner generation count: %s", ip, count)
            except (ValueError, TypeError):
                pass

    elif brand == "hp":
        # Cartridge part number from Printer-MIB (for old models without EWS)
        # OID: 1.3.6.1.2.1.43.11.1.1.6.1.1 (prtMarkerSuppliesDescription)
        val = _sg(ip, "1.3.6.1.2.1.43.11.1.1.6.1.1", community,
                  version=snmp_version, timeout=2.0)
        if val:
            v = str(val).strip()
            # Part numbers only (eg CC388A) — skip pure numeric serials
            if v and not v.isdigit() and len(v) >= 4:
                ids["black"] = f"part:{v}"
                source = "hp_mib_part"
                log.info("  [%s] HP toner part number: %s", ip, v)

    elif brand == "canon":
        # Cartridge model from Printer-MIB (only if there is no serial number)
        # OID: 1.3.6.1.2.1.43.11.1.1.6.1.1
        val = _sg(ip, "1.3.6.1.2.1.43.11.1.1.6.1.1", community,
                  version=snmp_version, timeout=2.0)
        if val:
            v = str(val).strip()
            if v and not v.isdigit() and len(v) >= 5:
                ids["black"] = f"model:{v}"
                source = "canon_mib_model"
                log.info("  [%s] Canon cartridge model: %s", ip, v)

    if ids:
        # Signal quality: genuine=unique per cartridge, generation=counter, static=model/part
        quality = {}
        for ck, val in ids.items():
            if val.startswith("toner_gen:"):
                quality[ck] = "generation"
            elif val.startswith(("part:", "model:")):
                quality[ck] = "static"
            else:
                quality[ck] = "genuine"
        return {"ids": ids, "supply_pages": pages, "source": source,
            "signal_quality": quality,
            "identity_type": {color: "fallback" for color in ids}}
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation point (with TTL cache)
# ─────────────────────────────────────────────────────────────────────────────
def get_cartridge_identity_data(ip: str, brand: str = None, community: str = "public",
                                snmp_version=None, force_refresh: bool = False) -> dict:
    """Returns the cartridge IDs and counters of a device.

    Output: {"ids": {color: serial}, "supply_pages": {color: pages}, "source": str}
    In the absence of evidence: {"ids": {}, "supply_pages": {}, "source": "none"} (cached).
    """
    if not ip:
        return {"ids": {}, "supply_pages": {}, "source": "none"}
    now = time.time()
    cached = _cache.get(ip)
    if cached and not force_refresh and cached[0] > now:
        return cached[1]

    cfg = load_id_config()
    brand_key = (brand or "").lower()
    ids, pages, sources, signal_quality, identity_type = {}, {}, [], {}, {}

    # 1) SNMP based on the approved map (printer-specific or brand-generic)
    oids = _snmp_oids_for(ip, brand_key, cfg)
    if oids:
        try:
            snmp_ids = _read_snmp_ids(ip, oids, community, snmp_version)
        except Exception as exc:
            log.debug("cartridge-id snmp read failed for %s: %s", ip, exc)
            snmp_ids = {}
        if snmp_ids:
            ids.update(snmp_ids)
            for ck in snmp_ids:
                signal_quality[ck] = "genuine"
                identity_type[ck] = "chip_id"
            sources.append("snmp")

    # 2) Web panel (HP always try; others if web=true in settings or limited by default)
    web = {}
    try:
        if brand_key == "hp" and cfg.get("hp_web", True):
            web = _read_hp_web_ids(ip)
        elif cfg.get("other_web", True):
            web = _read_other_web_ids(ip, brand_key)
    except Exception as exc:
        log.debug("cartridge-id web read failed for %s: %s", ip, exc)
        web = {}
    if web.get("ids"):
        for ck, sid in web["ids"].items():
            if ck not in ids:
                ids[ck] = sid
                signal_quality[ck] = "genuine"
                identity_type[ck] = web.get("identity_type", {}).get(ck, "serial")
        sources.append(f"{brand_key}_web")
    if web.get("supply_pages"):
        pages.update(web["supply_pages"])

    # 3) SNMP alternatives for printers that do not have chip ID:
    # a) Brother: Toner Replace Count — +1 each time replaced
    # b) Old HP: Cartridge part number from Printer-MIB
    # c) Canon LBP: Cartridge model from Printer-MIB
    if not ids and brand_key in ("brother", "hp", "canon"):
        try:
            _fallback = _read_fallback_identity(ip, brand_key, community, snmp_version)
            if _fallback:
                if _fallback.get("ids"):
                    ids.update(_fallback["ids"])
                if _fallback.get("supply_pages"):
                    pages.update(_fallback["supply_pages"])
                if _fallback.get("source"):
                    sources.append(_fallback["source"])
                if _fallback.get("signal_quality"):
                    signal_quality.update(_fallback["signal_quality"])
                if _fallback.get("identity_type"):
                    identity_type.update(_fallback["identity_type"])
        except Exception as exc:
            log.debug("cartridge-id fallback read failed for %s: %s", ip, exc)

    result = {"ids": ids, "supply_pages": pages,
              "source": "+".join(sources) if sources else "none",
              "signal_quality": signal_quality or None,
              "identity_type": identity_type or None}
    ttl = _TTL_FOUND if (ids or pages) else _TTL_MISS
    _cache[ip] = (now + ttl, result)
    return result


def clear_cache():
    """Empty the in-memory cartridge-ID cache."""

    _cache.clear()

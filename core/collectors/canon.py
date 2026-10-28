# =============================================================================
# @file:         core/collectors/canon.py
# @brief:        Collects status and counters from Canon MF/LBP printers with SNMP v1/v2c detection.
# @details:      Responsibilities: Probe SNMP version, read Canon OIDs and report network errors.
#                Main API: collect_canon()
#                Side effects: network I/O
# @dependencies: internal: core, core.collectors.base, core.database, core.snmp.protocol,
#                core.time_utils | external: requests
# @notes:        None.
# =============================================================================

"""Data collection from Canon (MF and LBP) with automatic SNMPv1/v2c detection and network error reporting"""

import errno
import logging
import re
import socket
import time
from datetime import datetime, timezone

from core.snmp.protocol import snmp_get_with_fallback
from core.collectors.base import si, ss, _counters_event
from core import store
from core.time_utils import to_utc_z, utc_now_iso

log = logging.getLogger("PrinterMonitor")


def _scrape_canon_toners(ip: str, timeout: float = 4.0):
    """
    Extracting the toner percentage from the Canon Remote UI page (HTTP)
    Return a dictionary containing color -> level (0-100) or None if unsuccessful
    """
    html = None
    # i-SENSYS family LBP (such as LBP233dw) has a separate consumer panel; Some common names
    urls = [f"http://{ip}/", f"http://{ip}/Status.html", f"http://{ip}/status.html",
            f"http://{ip}/index.html", f"http://{ip}/supply.html", f"http://{ip}/Supply.html",
            f"http://{ip}/consumables.html"]

    try:
        import requests as _req
        for url in urls:
            try:
                resp = _req.get(url, timeout=timeout,
                                headers={"User-Agent": "PrinterMonitor/1.0"},
                                allow_redirects=True)
                if resp.status_code == 200:
                    html = resp.text
                    log.debug(f"Canon scrape {ip}: fetched from {url}")
                    break
            except Exception as e:
                log.debug(f"Canon scrape {ip}: requests error fetching {url}: {e}", exc_info=True)
                continue
    except ImportError:
        pass

    if not html:
        try:
            import urllib.request as _ur
            for url in urls:
                try:
                    req = _ur.Request(url, headers={"User-Agent": "PrinterMonitor/1.0"})
                    with _ur.urlopen(req, timeout=timeout) as resp:
                        html = resp.read(200_000).decode("utf-8", errors="replace")
                        if html:
                            log.debug(f"Canon scrape {ip}: fetched from {url} (urllib)")
                            break
                except Exception as e:
                    log.debug(f"Canon scrape {ip}: urllib error fetching {url}: {e}", exc_info=True)
                    continue
        except Exception as e:
            log.debug(f"Canon scrape {ip} urllib error: {e}")

    if not html:
        return None

    result = {}
    patterns = {
        "black":   r"Black\s*[:：]?\s*(\d{1,3})%",
        "cyan":    r"Cyan\s*[:：]?\s*(\d{1,3})%",
        "magenta": r"Magenta\s*[:：]?\s*(\d{1,3})%",
        "yellow":  r"Yellow\s*[:：]?\s*(\d{1,3})%",
    }
    for color, pat in patterns.items():
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                result[color] = val

    if not result:
        # Some pages use a different GIF file name (eg ink_bk12.gif or ink_c05.gif).
        # We let the color code be 1-3 letters and the value 1-3 digits and determine the color based on the first letter.
        gif_pattern = r'ink_([a-z]{1,3})(\d{1,3})\.gif'
        for match in re.finditer(gif_pattern, html, re.IGNORECASE):
            color_code = (match.group(1) or '').lower()
            try:
                level = int(match.group(2))
            except Exception:
                continue
            if not (0 <= level <= 100):
                continue
            first = color_code[0] if color_code else ''
            if first in ('b', 'k'):
                result['black'] = level
            elif first == 'c':
                result['cyan'] = level
            elif first == 'm':
                result['magenta'] = level
            elif first == 'y':
                result['yellow'] = level

    if not result:
        # ✅ Tolerant pattern for new Remote UIs (those that exclude the percentage
        # Label Toner/Cartridge/Black — like LBP233dw)
        generic = re.search(
            r"(?:toner|cartridge|black)[^0-9%]{0,40}?(\d{1,3})\s*%",
            html, re.IGNORECASE)
        if generic:
            val = int(generic.group(1))
            if 0 <= val <= 100:
                result["black"] = val

    if result:
        log.info(f"Canon {ip} toner scrape result: {result}")
    else:
        log.debug(f"Canon {ip} toner scrape: no data found")

    return result if result else None


def collect_canon(ip: str, name: str, community: str, start: float) -> dict:
    """Collect status, counters and toner levels from a Canon MF or LBP printer."""

    log.debug(f"=== Starting Canon collection for {ip} ===")
    try:
        BASE = "1.3.6.1.4.1.1602"

        def g(oid, timeout=5):
            """Read one SNMP OID with a 30-second time budget; returns None when it is exceeded."""
            if time.time() - start > 30:
                return None
            return snmp_get_with_fallback(ip, oid, community, timeout=timeout)

        def gc(s, timeout=5.0):
            """Read one Canon SNMP OID below the Canon base OID."""
            return g(f"{BASE}.{s}", timeout=timeout)

        # ─── Offline diagnostics with detailed error reporting ───
        ut_raw = None
        error_type = "unknown"
        error_details = "Unknown error"
        try:
            ut_raw = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.3.0", community, timeout=2.0)
            if ut_raw is None:
                error_type = "no_response"
                error_details = "No SNMP response (timeout or error)"
        except socket.timeout:
            error_type = "timeout"
            error_details = "SNMP timeout (no reply)"
        except ConnectionRefusedError:
            error_type = "refused"
            error_details = "Connection refused (SNMP port closed)"
        except OSError as e:
            if e.errno == errno.ECONNREFUSED:
                error_type = "refused"
                error_details = "Connection refused"
            else:
                error_type = "network_error"
                error_details = f"Network error: {e}"
        except Exception as e:
            error_type = "exception"
            error_details = str(e)

        if ut_raw is None:
            elapsed = int((time.time() - start) * 1000)
            return {
                "ip": ip, "name": name, "brand": "canon",
                "online": False,
                "last_poll": utc_now_iso(),
                "poll_ms": elapsed,
                "error": error_details,
                "error_type": error_type,
            }

        # The device is online
        ut = si(ut_raw)
        us = ut // 100
        uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}"

        # Model, serial, firmware
        model = ss(gc("1.1.1.1.0"), "N/A")
        if model == "N/A":
            model = ss(g("1.3.6.1.2.1.43.5.1.1.16.1"), "N/A")
        if model == "N/A":
            model = ss(g("1.3.6.1.2.1.1.1.0"), "N/A")
        serial = ss(gc("1.2.1.4.0"), "N/A")
        if serial == "N/A":
            serial = ss(g("1.3.6.1.2.1.43.5.1.1.17.1"), "N/A")
        firmware = ss(gc("1.1.1.4.0"), "N/A")

        # Last print user (if any)
        last_user = None
        for user_oid in (
            f"{BASE}.1.11.2.3.1.2.1",
            f"{BASE}.1.11.2.3.1.3.1",
            f"{BASE}.1.11.1.3.1.2.1",
        ):
            val = ss(g(user_oid, timeout=3.0), "")
            if val and val != "N/A":
                last_user = val
                log.debug(f"Canon {ip} last_user={last_user} (OID {user_oid})")
                break

        # ─── counters ───
        total_std  = si(g("1.3.6.1.2.1.43.10.2.1.4.1.1"), -1)
        color_std  = si(g("1.3.6.1.2.1.43.10.2.1.4.1.2"), -1)
        bw_std     = si(g("1.3.6.1.2.1.43.10.2.1.4.1.3"), -1)
        if color_std >= 0 and total_std >= 0 and color_std >= total_std:
            log.debug(f"Canon {ip}: color_std={color_std} >= total_std={total_std}, discarding color_std")
            color_std = -1

        total_mf   = si(gc("1.11.2.1.1.3.1"), -1)
        copy_mf    = si(gc("1.11.2.1.1.3.6"), -1)
        printer_mf = si(gc("1.11.2.1.1.3.9"), -1)
        scan_mf    = si(gc("1.11.2.1.1.3.12"), -1)

        total_lbp  = si(gc("1.11.1.1.1.3.1"), -1)
        color_lbp  = si(gc("1.11.1.1.1.3.4"), -1)

        alt_total  = si(gc("1.11.2.2.1.3.1"), -1)
        alt_copy   = si(gc("1.11.2.2.1.3.6"), -1)
        alt_print  = si(gc("1.11.2.2.1.3.9"), -1)
        alt_scan   = si(gc("1.11.2.2.1.3.12"), -1)

        log.debug(f"Canon {ip} total_std={total_std}, total_mf={total_mf}, total_lbp={total_lbp}")
        log.debug(f"Canon {ip} copy_mf={copy_mf}, printer_mf={printer_mf}, scan_mf={scan_mf}")
        log.debug(f"Canon {ip} alt_total={alt_total}, alt_copy={alt_copy}, alt_print={alt_print}")

        model_upper = model.upper() if model else ""
        is_mf = total_mf >= 0
        if not is_mf and alt_total >= 0:
            is_mf = True
            total_mf = alt_total
            copy_mf = alt_copy
            printer_mf = alt_print
            scan_mf = alt_scan
            log.info(f"Canon {ip} MF detected via alternate OIDs: total={total_mf}")
        if not is_mf and "MF" in model_upper:
            is_mf = True
            log.info(f"Canon {ip} MF detected by model name: {model}")

        if is_mf:
            if total_mf >= 0:
                total = total_mf
            elif total_std >= 0:
                total = total_std
                log.info(f"Canon {ip} MF using total_std={total_std} as fallback")
            else:
                total = 0
                log.warning(f"Canon {ip} MF no counter available, setting total=0")
            copy_ = copy_mf if copy_mf >= 0 else -1
            printer = printer_mf if printer_mf >= 0 else -1
            if copy_ < 0 and printer < 0 and total > 0:
                printer = total
                copy_ = 0
                log.info(f"Canon {ip} MF: no copy/printer OIDs, assigning total to printer")
            if "MF210" in model_upper or "MF220" in model_upper:
                if total_mf < 0 and total_std >= 0:
                    total = total_std
                    copy_ = 0
                    printer = total
                    log.info(f"Canon {ip} MF210/220 profile: using standard MIB, total={total}")
            log.debug(f"Canon {ip} detected as MF: total={total}, copy={copy_}, printer={printer}")
        else:
            total = total_lbp if total_lbp >= 0 else (total_std if total_std >= 0 else 0)
            if total == 0:
                alt_lbp_total = si(g("1.3.6.1.2.1.43.10.2.1.4.1.1"), -1)
                if alt_lbp_total < 0:
                    alt_lbp_total = si(gc("1.11.1.1.1.3.3"), -1)
                if alt_lbp_total >= 0:
                    total = alt_lbp_total
                    log.info(f"Canon {ip} LBP fallback counter: total={total}")
            copy_ = -1
            printer = total
            log.debug(f"Canon {ip} detected as LBP or fallback: total={total}")

        if total < 0:
            total = 0
            log.warning(f"Canon {ip} no counter received, setting total to 0")

        # ═══ Check and retry for total=0 suspicious ═══
        prev_data = store._prev.get(ip) or {}
        prev_total = prev_data.get("print_total") if prev_data else None
        if total == 0 and prev_total is not None and prev_total > 1000:
            log.warning(f"Canon {ip}: total=0 but prev={prev_total}, retrying with longer timeout...")
            retry_val = snmp_get_with_fallback(ip, "1.3.6.1.2.1.43.10.2.1.4.1.1", community, timeout=5.0)
            retry_total = si(retry_val)
            if retry_total > 0:
                log.warning(f"Canon {ip}: retry successful → total={retry_total}")
                total = retry_total
            else:
                # ✅ Bug: error registration and maintaining total=0 (not prev_total)
                log.error(f"Canon {ip}: retry also failed, recording SNMP error")
                from core.database import add_event
                add_event(ip, "SNMP_ERROR", {
                    "message": f"SNMP total=0 and retry failed for {ip}",
                    "severity": "error",
                    "prev_total": prev_total,
                })

        # Calculation of full_color and black_white
        full_color = None
        black_white = None
        if is_mf:
            if 0 <= color_std < total:
                full_color = color_std
            if 0 <= bw_std <= total:
                black_white = bw_std
            if black_white is None and full_color is not None and total > 0:
                black_white = max(0, total - full_color)
            if full_color is None and black_white is None and total > 0:
                black_white = total
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

        # ─── Extract toner from HTTP (always try, return None if failed) ───
        scraped_toners = _scrape_canon_toners(ip, timeout=3.0)

        # ─── CMYK toner (priority with scraped, otherwise SNMP) ───
        toner_map = [(1, "black", "Black Toner"),
                     (2, "cyan", "Cyan Toner"),
                     (3, "magenta", "Magenta Toner"),
                     (4, "yellow", "Yellow Toner")]

        toners = {}
        for idx, key, default_name in toner_map:
            if scraped_toners and key in scraped_toners:
                level = scraped_toners[key]
                if level is not None:
                    toner_st = ("empty" if level == 0 else
                                "critical" if level <= 10 else
                                "low" if level <= 25 else "ok")
                    toners[key] = {
                        "level": level,
                        "status": toner_st,
                        "name": default_name,
                        "remaining": None,
                        "max": 100,
                        "source": "http_scrape"
                    }
                    continue

            t_name = ss(g(f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"), default_name)
            t_max  = si(g(f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"), -1)
            t_rem  = si(g(f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"), -1)
            if t_max == -1 and t_rem == -1:
                if key not in toners:
                    toners[key] = {"level": None, "status": "unknown", "name": default_name,
                                   "remaining": -1, "max": -1, "source": "snmp_unavailable"}
                continue
            if t_rem < 0 or t_max <= 0:
                toner_pct, toner_st = None, "unknown"
            else:
                toner_pct = round(t_rem / t_max * 100)
                toner_st  = ("empty" if toner_pct == 0 else
                             "critical" if toner_pct <= 10 else
                             "low" if toner_pct <= 25 else "ok")
            toners[key] = {"level": toner_pct, "status": toner_st, "name": t_name,
                           "remaining": t_rem, "max": t_max, "source": "snmp"}

        if not toners:
            toners["black"] = {"level": None, "status": "unknown", "name": "Black Toner",
                               "remaining": -1, "max": -1, "source": "none"}

        # ─── trays ───
        trays = []
        for idx, label in [(1, "Bypass"), (2, "Tray 1")]:
            cap = si(g(f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"), 0)
            lvl = si(g(f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"), -9)
            nm  = ss(g(f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"), label)
            if cap == 0 and lvl == -9:
                continue
            if lvl == -2:
                st = "no_sensor"
            elif lvl <= 0:
                st = "empty"
            elif cap > 0:
                pct = round(lvl / cap * 100)
                st = "low" if pct <= 25 else ("medium" if pct <= 75 else "ok")
            else:
                st = "unknown"
            trays.append({"name": nm, "level": lvl, "capacity": cap, "status": st})

        alerts = []
        cover = si(g("1.3.6.1.2.1.43.6.1.1.3.1.1"), 4)
        if cover != 4:
            alerts.append({"message": "درب پرینتر باز است", "code": cover})

        elapsed = int((time.time() - start) * 1000)
        prev = store._prev.get(ip) or {}
        black_level = None
        if toners.get("black", {}).get("level") is not None:
            black_level = toners["black"]["level"]
        else:
            for t in toners.values():
                if t.get("level") is not None:
                    black_level = t["level"]
                    break
        prev_toner = prev.get("toner_level")
        counters_data = {"total": total, "full_color": full_color, "black_white": black_white,
                 "printer": printer if printer >= 0 else None,
                 "copy": copy_ if (is_mf and copy_ >= 0) else None,
                 "fax": None}
        _counters_event(ip, total, prev, alerts, [a["code"] for a in alerts],
                full_color=full_color, black_white=black_white,
                paper_size=None, username=last_user,
                current_toner_level=black_level, prev_toner_level=prev_toner,
                uptime=ut, poll_timestamp=to_utc_z(datetime.fromtimestamp(start, tz=timezone.utc)))

        brand_tag = "canon-mf" if is_mf else "canon-lbp"
        log.info(f"  ✓ {name} [{brand_tag}] total={total:,} {elapsed}ms")
        return {
            "ip": ip, "name": name, "brand": "canon",
            "online": True, "last_poll": utc_now_iso(), "poll_ms": elapsed,
            "device": {"model": model, "serial": serial, "firmware": firmware, "uptime_str": uptime_str},
            "counters": counters_data,
            "paper_sizes": {}, "trays": trays, "toners": toners, "alerts": alerts,
        }
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        log.error(f"  ✗ {name} [canon] error: {e}", exc_info=True)
        return {
            "ip": ip, "name": name, "brand": "canon",
            "online": False, "last_poll": utc_now_iso(), "poll_ms": elapsed,
            "device": {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {"total": 0, "full_color": None, "black_white": None,
                         "printer": 0, "copy": None, "fax": None},
            "paper_sizes": {}, "trays": [],
            "toners": {"black": {"level": None, "status": "unknown"}},
            "alerts": [{"message": f"Collection error: {e}", "code": 9999}],
            "error": str(e),
            "error_type": "exception",
        }
# =============================================================================
# @file:         core/collectors/hp.py
# @brief:        Collects status and toner data from HP LaserJet printers.
# @details:      Responsibilities: Read HP OIDs with an increased timeout and detect colour toners
#                reliably.
#                Main API: collect_hp()
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.database, core.time_utils
# @notes:        None.
# =============================================================================

"""Data collection from HP LaserJet with increased timeout and better color detection"""
import re, time, logging
from datetime import datetime, timezone

from core.collectors.base import si, ss, _g, _counters_event, fetch_first_web_page
from core import store
from core.time_utils import to_utc_z, utc_now_iso

log = logging.getLogger("PrinterMonitor")


def _scrape_hp_toners(ip: str, timeout: float = 4.0):
    """Web fallback for HP toner level when SNMP does not return a number.

    Important discovery on real devices (E52645 with W9008MC and LaserJet 400 M401dn with
    CE505A — both with level=-2 in SNMP): cause of level "masking", **Cartridge
    Charged/Used** — EWS itself is explicitly "Used Black Cartridge"
    It says that neither SNMP gives a number nor the web. In this case, instead of the fake number, the state
    Returns ``_used'' to the actual state UI ("Cartridge Charged/Used - Unspecified Level")
    show

    Verified builds on real device:
    - New generation (FutureSmart / onehp):
        Supplies Status page:
          <h2 id="BlackCartridge1-Header">…</h2><p class="data percentage">67%*</p>
          <strong id="BlackCartridge1-SupplyState">Used|OK</strong>
        Device Status page (root):
          <h2 id="SupplyName0" …>…</h2> … <span id="SupplyPLR0" class="plr">67%*</span>
          Note: The gauge always shows width:0% when the chip is unknown — never from
          We don't get that number; The only valid source is PLR text / data-percentage.
    - Old generation (classic EWS like M401dn): supplies pages with direct names
      (info_suppliesStatus.html / supplies_status.html) and the text "Used... Cartridge"/
      Anticounterfeit message for refilled/used cartridge.

    Output: {"<color>": 0-100, "_used": ["<color>", …]} — "_" keys are given
    and are rejected in merge. None is returned if there is no usable data.
    """
    urls = [
        f"http://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
        f"https://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
        f"http://{ip}/DevMgmt/ConsumableConfigDyn.xml",
        f"https://{ip}/DevMgmt/ConsumableConfigDyn.xml",
        f"http://{ip}/hp/device/info_suppliesStatus.html",
        f"http://{ip}/hp/device/supplies_status.html",
        f"http://{ip}/info_suppliesStatus.html",
        f"http://{ip}/hp/info/suppliesStatus.html",
        f"http://{ip}/", f"https://{ip}/",
    ]
    used, html = fetch_first_web_page(ip, urls, timeout)
    if not html:
        log.debug(f"HP scrape {ip}: web panel unreachable (http+https)")
        return None
    log.debug(f"HP scrape {ip}: fetched {used} ({len(html)} chars)")

    result = {}
    used_colors = set()

    def _key(name: str):
        # Color recognition with a word border — such as "Document Feeder Kit" which has the letter k should not
        # be mistakenly black; Only black/cyan/magenta/yellow in full word form.
        """Map a supply name to its colour key (black, cyan, magenta or yellow)."""
        n = (name or "").lower()
        for color in ("black", "cyan", "magenta", "yellow"):
            if re.search(r"\b%s\b" % color, n):
                return color
        return None

    def _mark_used(name: str, state_text: str = ""):
        """Mark a supply as used when its name or state text mentions 'used'."""
        if "used" in (name or "").lower() or "used" in (state_text or "").lower():
            k = _key(name)
            if k:
                used_colors.add(k)

    # 1) New generation — SupplyName<n>/SupplyPLR<n> pairs on Device Status screen
    names = dict(re.findall(r'id="SupplyName(\d+)"[^>]*>\s*([^<]{2,80}?)\s*</h2>', html))
    for idx, plr_html in re.findall(r'id="SupplyPLR(\d+)"[^>]*>\s*([^<]{0,40}?)\s*</span>', html):
        name = names.get(idx, "")
        key = _key(name)
        _mark_used(name)
        if not key or key in result:
            continue
        m = re.search(r"(\d{1,3})\s*%", plr_html)
        if m and 0 <= int(m.group(1)) <= 100:
            result[key] = int(m.group(1))

    # 2) New generation - Supplies Status page:
    #    <h2 id="BlackCartridge1-Header">NAME</h2><p class="data percentage">VAL</p>
    for _id, name, pct_html in re.findall(
            r'<h2 id="(\w+)-Header">\s*([^<]*?)\s*</h2>\s*'
            r'<p class="data percentage">\s*([^<]{0,40}?)\s*</p>', html):
        key = _key(name)
        _mark_used(name)
        if not key or key in result:
            continue
        m = re.search(r"(\d{1,3})\s*%", pct_html)
        if m and 0 <= int(m.group(1)) <= 100:
            result[key] = int(m.group(1))

    # 3) Detection of "Used" from SupplyState and page text (both generations)
    for sid, stext in re.findall(r'id="(\w+)-SupplyState">\s*([^<]{1,40}?)\s*</strong>', html):
        # The corresponding name of the header of the same block
        m = re.search(r'id="%s-Header">\s*([^<]*?)\s*</h2>' % re.escape(sid), html)
        _mark_used(m.group(1) if m else sid, stext)
    if re.search(r"Used\s+[A-Za-z]+\s+Cartridge|used supply has been installed|anticounterfeit",
                 html, re.IGNORECASE):
        # Generic form old/new generation — at least recharged/used black cartridge detected
        m = re.search(r"Used\s+([A-Za-z]+)\s+Cartridge", html, re.IGNORECASE)
        k = _hp_toner_key(m.group(1)) if m else "black"
        if k:
            used_colors.add(k)

    # 4) DevMgmt consumption XML (Web Jetadmin family)
    if "black" not in result:
        for pat in (
            r"(?:PercentageLevelRemaining|LevelRemaining|PercentRemaining|percentageRemaining)[^0-9]{0,12}(\d{1,3})",
            r"ConsumablePercentageLevel[^0-9]{0,12}(\d{1,3})",
        ):
            m = re.search(pat, html, re.IGNORECASE)
            if m and 0 <= int(m.group(1)) <= 100:
                result["black"] = int(m.group(1))
                break

    # 5) data-* features in modern FutureSmart pages
    if "black" not in result:
        m = re.search(r"data-(?:percent|value|level)\s*=\s*[\"'](\d{1,3})[\"']", html, re.IGNORECASE)
        if m and 0 <= int(m.group(1)) <= 100:
            result["black"] = int(m.group(1))

    # Output only when we actually have valid data or state
    if result or used_colors:
        if used_colors:
            result["_used"] = sorted(used_colors)
        log.info(f"HP {ip} toner scrape result: {result} (source: {used})")
        return result
    log.debug(f"HP scrape {ip}: no percent/state found in page")
    return None


_HP_TONER_COLOR_MAP = {
    "black": "black", "k": "black",
    "cyan": "cyan", "c": "cyan",
    "magenta": "magenta", "m": "magenta",
    "yellow": "yellow", "y": "yellow",
}

def _hp_toner_key(name: str) -> str:
    """Cartridge color key from SNMP name (prtMarkerSuppliesDescription).

    match **word boundary** — same as `_key()` in `_scrape_hp_toners` for
    It already had a web path. Reason (TD-016, Criterion G.5): match substring "k/c/m/y" too
    eats and closes the colorless names to the wrong color:
        'Imaging Drum' → magenta (the letter m in 'iMaging')
        'CF470A Imaging Drum' → cyan (letter c in 'CF470A')
        'Fuser Kit' / 'Maintenance Kit' → black (letter k in "Kit")
    Its effect on monochrome printers: `has_color_toner` false True ⇒ `full_color=None` and
    `black_white=None` (instead of `black_white=total`) and driver/drum as color toner in UI.
    """
    n = (name or "").lower()
    for kw, key in _HP_TONER_COLOR_MAP.items():
        if re.search(rf"\b{re.escape(kw)}\b", n):
            return key
    return None

def collect_hp(ip: str, name: str, community: str, start: float) -> dict:
    """Collect status, counters and toner levels from an HP LaserJet printer."""

    try:
        # Increase the total timeout to 25 seconds (similar to Canon)
        def g(oid, timeout=5.0):
            """Read one SNMP OID with a 25-second time budget; returns None when it is exceeded."""
            if time.time() - start > 25.0:
                return None
            val = _g(ip, oid, community, timeout=timeout)
            log.debug(f"HP {ip} OID {oid} -> {val} (took {time.time()-start:.2f}s)")
            return val

        ut = si(g("1.3.6.1.2.1.1.3.0", timeout=5.0))
        us = ut // 100
        uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}"

        model = ss(g("1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0"), "N/A")
        if model == "N/A":
            model = ss(g("1.3.6.1.2.1.43.5.1.1.16.1"), "N/A")
        if model == "N/A":
            desc = ss(g("1.3.6.1.2.1.1.1.0"), "")
            m = re.search(r'PID:([^,]+)', desc)
            model = m.group(1).strip() if m else "Unknown"
        serial = ss(g("1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0"), "N/A")
        if serial == "N/A":
            serial = ss(g("1.3.6.1.2.1.43.5.1.1.17.1"), "N/A")
        firmware = ss(g("1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.7.0"), "N/A")

        total = si(g("1.3.6.1.2.1.43.10.2.1.4.1.1", timeout=5.0))

        # ═══ Check and retry for total=0 suspicious ═══
        prev_data = store._prev.get(ip) or {}
        prev_total = prev_data.get("print_total") if prev_data else None
        if total == 0 and prev_total is not None and prev_total > 1000:
            log.warning(f"HP {ip}: total=0 but prev={prev_total}, retrying with longer timeout...")
            retry_val = _g(ip, "1.3.6.1.2.1.43.10.2.1.4.1.1", community, timeout=5.0)
            retry_total = si(retry_val)
            if retry_total > 0:
                log.warning(f"HP {ip}: retry successful → total={retry_total}")
                total = retry_total
            else:
                # ✅ Bug: error registration and maintaining total=0 (not prev_total)
                log.error(f"HP {ip}: retry also failed, recording SNMP error")
                from core.database import add_event
                add_event(ip, "SNMP_ERROR", {
                    "message": f"SNMP total=0 and retry failed for {ip}",
                    "severity": "error",
                    "prev_total": prev_total,
                })

        color_print = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.5.1"), -1)
        copy_mono   = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.9.1"), -1)
        copy_color  = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.6.1"), -1)
        scan_mono   = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.10.1"), -1)  # noqa: F841 - SNMP read kept on purpose (poll side effect)
        scan_color  = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.3.1"), -1)  # noqa: F841 - SNMP read kept on purpose (poll side effect)
        fax_count   = si(g("1.3.6.1.4.1.11.2.3.9.6.1.1.11.1"), -1)
        copy_total  = max(copy_mono, 0) + max(copy_color, 0)

        # ── Reading toners to detect the presence of color ──
        toners = {}
        has_color_toner = False
        for idx in range(1, 5):
            t_name = ss(g(f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"), "")
            t_max  = si(g(f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"), -1)
            t_rem  = si(g(f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"), -2)
            if t_max == -1 and t_rem == -2:
                break
            if not t_name:
                t_name = "Black Toner" if idx == 1 else f"Toner {idx}"
            toner_key = _hp_toner_key(t_name) or ("black" if idx == 1 else f"toner_{idx}")
            if toner_key in ("cyan", "magenta", "yellow"):
                has_color_toner = True
            if t_rem == -2 or t_max <= 0:
                toner_pct, toner_st = None, "unknown"
            elif t_rem <= 0:
                toner_pct, toner_st = 0, "empty"
            else:
                toner_pct = round(t_rem / t_max * 100)
                toner_st = "ok" if toner_pct > 25 else ("low" if toner_pct > 10 else "critical")
            toners[toner_key] = {"level": toner_pct, "status": toner_st,
                                 "name": t_name, "remaining": t_rem, "max": t_max}

        if not toners:
            toners["black"] = {"level": None, "status": "unknown",
                               "name": "Black Toner", "remaining": -1, "max": -1}

        # ── color recognition ──
        if color_print >= 0:
            full_color = color_print
            bw = max(0, total - color_print)
        elif has_color_toner:
            # Printer is color but color OID is not available → color/black and white resolution is unknown
            full_color = None
            bw = None
            log.warning(f"HP {ip} has color toners but no color counter, cannot split color vs BW")
        else:
            # Monochrome printer
            full_color = None
            bw = total

        # ── Trays ──
        trays = []
        for idx, label in [(1,"Tray 1"),(2,"Tray 2")]:
            cap = si(g(f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"), 0)
            lvl = si(g(f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"), -9)
            nm  = ss(g(f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"), label)
            if cap == 0 and lvl == -9:
                continue
            if lvl == -2:
                st = "no_sensor"
            elif lvl == -3 or lvl <= 0:
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
        _counters_event(ip, total, prev, alerts, [a["code"] for a in alerts],
                full_color=full_color, black_white=bw, paper_size=None,
                current_toner_level=black_level, prev_toner_level=prev_toner,
                uptime=ut, poll_timestamp=to_utc_z(datetime.fromtimestamp(start, tz=timezone.utc)))

        color_info = f"color={color_print}" if color_print >= 0 else ("has_color_toner" if has_color_toner else "mono")
        copies_str = f"copy={copy_total:,}" if copy_total > 0 else "nodata"
        toner_pct_log = next(iter(toners.values()), {}).get("level")
        bw_display = f"{bw:,}" if isinstance(bw, int) else "unknown"
        log.info(f"  ✓ {name} [hp] total={total:,} bw={bw_display} {color_info} {copies_str} "
             f"toner={toner_pct_log}% {elapsed}ms")
        return {
            "ip": ip, "name": name, "brand": "hp",
            "online": True, "last_poll": utc_now_iso(), "poll_ms": elapsed,
            "device": {"model": model, "serial": serial, "firmware": firmware, "uptime_str": uptime_str},
            "counters": {"total": total, "full_color": full_color, "black_white": bw,
                         "printer": total, "copy": copy_total if copy_total > 0 else None,
                         "fax": fax_count if fax_count >= 0 else None},
            "paper_sizes": {}, "trays": trays, "toners": toners, "alerts": alerts,
        }
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        log.exception(f"  ✗ {name} [hp] error: {e} {elapsed}ms")
        return {
            "ip": ip, "name": name, "brand": "hp",
            "online": True, "last_poll": utc_now_iso(), "poll_ms": elapsed,
            "device": {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {"total": 0, "full_color": None, "black_white": 0,
                         "printer": 0, "copy": None, "fax": None},
            "paper_sizes": {}, "trays": [],
            "toners": {"black": {"level": None, "status": "unknown"}},
            "alerts": [{"message": f"Collection error: {e}", "code": 9999}],
        }
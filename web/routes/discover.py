# =============================================================================
# @file:         web/routes/discover.py
# @brief:        API endpoint that discovers printers on the network.
# @details:      Responsibilities: Run discovery and return the found devices.
#                Main API: api_discover()
#                Side effects: network I/O, starts threads, defines HTTP routes
# @dependencies: internal: core.snmp.protocol | external: flask
# @notes:        None.
# =============================================================================

"""Network discovery API routes."""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request

from core.snmp.protocol import snmp_get

bp = Blueprint("discover", __name__)
log = logging.getLogger("PrinterMonitor")

_SUBNET_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
_MAX_TOTAL_IPS = 512          # Total limit of scan addresses in one request
_MAX_RANGE_SIZE = 254         # The ceiling of each span

_FULL_IP_RE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$")


def _normalize_subnet(raw: str) -> str:
    """Normalization of user input before validation (Fix common 400 in UI):
    - The distance and the last possible point are removed ("172.16.25" → "172.16.25")
    - If the user enters a full 4-part IP ("172.16.25.54"), it will be converted to a 3-part subnet (/24)
    The security validation (_is_valid_subnet) is applied unchanged after normalization."""
    s = str(raw or "").strip().rstrip(".")
    m = _FULL_IP_RE.match(s)
    if m:
        s = m.group(1)
    return s


def _is_valid_subnet(subnet: str) -> bool:
    """3-part subnet validation (eg 172.16.25) — prevent arbitrary/malicious scanning."""
    if not subnet or not _SUBNET_RE.match(subnet):
        return False
    try:
        return all(0 <= int(part) <= 255 for part in subnet.split("."))
    except (TypeError, ValueError):
        return False

PRIMARY_OID = "1.3.6.1.2.1.1.1.0"  # sysDescr

SECONDARY_OIDS = [
    "1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1",       # Toshiba
    "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0",       # HP
    "1.3.6.1.4.1.1602.1.1.1.1.0",                     # Canon
    "1.3.6.1.4.1.2435.2.3.9.1.1.3.1.1.1.1.2.0",     # Brother
    "1.3.6.1.4.1.47206.1.0",                            # ECS100G model
    "1.3.6.1.4.1.47206.110.1.2.0",                      # ECS100G temp1
    "1.3.6.1.4.1.47206.111.1.2.0",                      # ECS100G hum1
]

FALLBACK_COMMUNITIES = ["public", "private", "TOSHIBA", "toshiba"]


def _try_snmp(ip, oid, comm, timeout=0.5):
    """Try with v2c, retry with v1 if failed and only for PRIMARY_OID"""
    # 1. v2c first
    val = snmp_get(ip, oid, comm, timeout=timeout, version=2)
    if val and str(val) not in ("N/A", "None", ""):
        return val, "v2c"

    # 2. If there is no response and only for the original OID, fallback to v1
    if oid == PRIMARY_OID:
        val = snmp_get(ip, oid, comm, timeout=timeout, version=1)
        if val and str(val) not in ("N/A", "None", ""):
            return val, "v1"

    return None, None


@bp.route('/api/printers/discover', methods=['POST'])
def api_discover():
    """Probe the requested addresses for SNMP printers and return the ones that answer."""

    body      = request.get_json() or {}
    community = str(body.get("community", "public") or "public")[:32]
    ranges    = body.get("ranges")
    if ranges is None:
        subnet = body.get("subnet", "172.16.25")
        try:
            s_i = int(body.get("start", 1)); e_i = int(body.get("end", 254))
        except (TypeError, ValueError):
            return jsonify({"error": "مقادیر start/end نامعتبر است"}), 400
        ranges = [{"subnet": subnet, "start": s_i, "end": e_i}]

    if not isinstance(ranges, list) or not ranges:
        return jsonify({"error": "ranges نامعتبر است"}), 400

    # 🔒 Validation of entries + limit on the number of addresses that can be scanned (to prevent abuse)
    normalized_ranges = []
    total_ips = 0
    for rng in ranges:
        if not isinstance(rng, dict):
            return jsonify({"error": "فرمت ranges نامعتبر است"}), 400
        subnet = _normalize_subnet(rng.get("subnet", ""))
        if not _is_valid_subnet(subnet):
            return jsonify({"error": f"subnet نامعتبر است: {subnet!r}"}), 400
        try:
            s = int(rng.get("start", 1)); e = int(rng.get("end", 254))
        except (TypeError, ValueError):
            return jsonify({"error": "مقادیر start/end نامعتبر است"}), 400
        s = max(1, min(254, s))
        e = max(1, min(254, e))
        if e < s:
            return jsonify({"error": "بازه IP نامعتبر است (end باید >= start باشد)"}), 400
        if (e - s + 1) > _MAX_RANGE_SIZE:
            return jsonify({"error": f"اندازه هر بازه حداکثر {_MAX_RANGE_SIZE} آدرس است"}), 400
        total_ips += (e - s + 1)
        normalized_ranges.append({"subnet": subnet, "start": s, "end": e})
    if total_ips > _MAX_TOTAL_IPS:
        return jsonify({"error": f"حداکثر {_MAX_TOTAL_IPS} آدرس در هر درخواست مجاز است"}), 400
    ranges = normalized_ranges

    found = []
    lock  = threading.Lock()

    def probe(ip):
        """Probe one IP with the SNMP communities until one of them responds."""
        communities = [community] + [c for c in FALLBACK_COMMUNITIES if c != community]

        for comm in communities:
            val, ver = _try_snmp(ip, PRIMARY_OID, comm, timeout=0.5)
            if val:
                log.info(f"  ✔ Discovery: {ip} پاسخ داد ({ver}, community='{comm}')")
                is_sensor = "ECS100G" in str(val).upper()
                with lock:
                    found.append({
                        "ip": ip,
                        "model": str(val)[:50],
                        "community": comm,
                        "snmp_version": ver,
                        "brand": "sensor" if is_sensor else "unknown",
                        "device_type": "sensor" if is_sensor else "unknown",
                    })
                return

        # Step 2: Brand Specific OIDs (v2c only)
        for oid in SECONDARY_OIDS:
            for version in (2, 1):
                val = snmp_get(ip, oid, community, timeout=0.5, version=version)
                if val and str(val) not in ("N/A", "None", ""):
                    is_sensor = oid.startswith("1.3.6.1.4.1.47206")
                    log.info(f"  ✔ Discovery: {ip} پاسخ داد (v{version}, brand-specific OID)")
                    with lock:
                        found.append({
                            "ip": ip,
                            "model": str(val)[:50] if not is_sensor else "ECS100G",
                            "community": community,
                            "snmp_version": f"v{version}",
                            "brand": "sensor" if is_sensor else "unknown",
                            "device_type": "sensor" if is_sensor else "unknown",
                        })
                    return

    all_ips = []
    for rng in ranges:
        subnet = rng["subnet"]
        s, e   = int(rng["start"]), int(rng["end"])
        for i in range(s, e + 1):
            all_ips.append(f"{subnet}.{i}")

    with ThreadPoolExecutor(max_workers=100) as ex:
        for _ in as_completed([ex.submit(probe, ip) for ip in all_ips]):
            pass

    return jsonify({"found": found, "scanned": len(all_ips)})
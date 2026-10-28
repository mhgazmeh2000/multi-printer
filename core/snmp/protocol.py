# =============================================================================
# @file:         core/snmp/protocol.py
# @brief:        Pure-Python SNMP v1 and v2c implementation without external dependencies.
# @details:      Responsibilities: Encode and decode BER/OID values, build GET requests and
#                perform SNMP GETs with fallback.
#                Main API: encode_oid(), encode_length(), decode_oid_bytes(), build_snmp_get_v1(),
#                build_snmp_get_v2c(), build_snmp_getnext_v1(), build_snmp_getnext_v2c(),
#                parse_snmp_response_debug() (+7 more)
#                Side effects: network I/O, starts threads
# @dependencies: stdlib only
# @notes:        No third-party SNMP library: packets are built by hand, so keep the encoders in
#                sync with the tests.
# =============================================================================

"""
Manual implementation of SNMPv1 and v2c without external dependencies:
- encode_oid / encode_length
- build_snmp_get_v1, build_snmp_get_v2c
- parse_snmp_response
- SNMP version cache for each IP (SNMPv1/v2c auto-detection)
- snmp_get (prescriptive)
- snmp_get_with_fallback (with cache and smart test)
- snmp_get_bulk
"""

import logging
import socket
import threading
import time

log = logging.getLogger("PrinterMonitor")

SNMP_ERROR_NAMES = {
    0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue",
    4: "readOnly", 5: "genErr", 6: "noAccess", 7: "wrongType",
    8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
    12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
    15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName"
}

# SNMP version cache for each IP/community
# Each entry is saved as follows:
#   {"version": 1|2|None, "expires_at": monotonic_ts|None}
# We keep negative-cache with short TTL for failed versions (offline / timeout).
# so that it is not probed again in every pull.
_SNMP_VERSION_CACHE = {}
_version_cache_lock = threading.Lock()
_NEGATIVE_CACHE_TTL = 30.0
_POSITIVE_CACHE_TTL = 3600.0


def _version_cache_key(ip: str, community: str) -> str:
    """Build the cache key for a printer and community pair."""
    return f"{ip}|{community}"



def _get_cached_version(ip: str, community: str):
    """Return the cached SNMP version for a printer and community, or None."""
    key = _version_cache_key(ip, community)
    with _version_cache_lock:
        entry = _SNMP_VERSION_CACHE.get(key)
        if entry is None and ip in _SNMP_VERSION_CACHE:
            # backward compatibility with old caches only based on IP
            legacy = _SNMP_VERSION_CACHE.get(ip)
            if legacy in (1, 2, None):
                entry = {"version": legacy, "expires_at": None if legacy in (1, 2) else time.monotonic() + _NEGATIVE_CACHE_TTL}
                _SNMP_VERSION_CACHE[key] = entry
                _SNMP_VERSION_CACHE.pop(ip, None)
        if not isinstance(entry, dict):
            return False, None
        expires_at = entry.get("expires_at")
        if expires_at is not None and time.monotonic() >= expires_at:
            _SNMP_VERSION_CACHE.pop(key, None)
            return False, None
        return True, entry.get("version")



def _set_cached_version(ip: str, community: str, version: int | None, ttl: float | None):
    """Store the SNMP version for a printer and community with a time-to-live."""
    key = _version_cache_key(ip, community)
    with _version_cache_lock:
        _SNMP_VERSION_CACHE[key] = {
            "version": version,
            "expires_at": (time.monotonic() + ttl) if ttl is not None else None,
        }



def _detect_snmp_version(ip: str, community: str, port: int = 161, probe_timeout: float = 1.5) -> int | None:
    """
    SNMP version detection with several common and vendor-specific OIDs.

    Some devices do not respond to the standard sysUpTime or sysDescr, but to
    sysName, Printer-MIB model, or vendor-specific OIDs provide a valid response.
    Therefore, for backward compatibility and supporting incompatible models,
    We try several OIDs in sequence.
    """
    found, cached_version = _get_cached_version(ip, community)
    if found:
        return cached_version

    probe_oids = [
        "1.3.6.1.2.1.1.3.0",                      # sysUpTime
        "1.3.6.1.2.1.1.1.0",                      # sysDescr
        "1.3.6.1.2.1.1.5.0",                      # sysName
        "1.3.6.1.2.1.43.5.1.1.16.1",              # Printer-MIB model
        "1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1",  # Toshiba model
        "1.3.6.1.4.1.47206.1.0",                    # ECS100G model
        "1.3.6.1.4.1.47206.110.1.2.0",              # ECS100G temp1
    ]

    # ✅ Fake offline fix: We pass twice. first pass with normal timeout; if none
    # No response, second pass with larger timeout on key OIDs — some models
    # Old Toshiba (like e-STUDIO 2505/257/3540 and old Ethernet boards) especially
    # After idle, they respond to the first requests slowly (2-4 seconds) and the first pass
    # lose The same single line timeout used to cause false offline.
    slow_probe_timeout = max(3.5, float(probe_timeout) * 2.3)
    for attempt_timeout, oid_subset in (
        (probe_timeout, probe_oids),
        (slow_probe_timeout, probe_oids[:3] + [probe_oids[4]]),
    ):
        for oid in oid_subset:
            # 🔥 Previous change: v1 is tested first (because it is more common in real networks)
            res_v1 = snmp_get(ip, oid, community, port, attempt_timeout, version=1)
            if res_v1 is not None:
                _set_cached_version(ip, community, 1, _POSITIVE_CACHE_TTL)
                log.debug(f"SNMP cache: {ip} -> v1 using {oid} (timeout={attempt_timeout}s)")
                return 1

            # Then v2c
            res_v2 = snmp_get(ip, oid, community, port, attempt_timeout, version=2)
            if res_v2 is not None:
                _set_cached_version(ip, community, 2, _POSITIVE_CACHE_TTL)
                log.info(f"SNMP cache: {ip} -> v2c using {oid} (v1 failed, timeout={attempt_timeout}s)")
                return 2

    # None of them answered; Probably the device is offline or SNMP blocked.
    log.debug(f"SNMP cache: {ip} unreachable (all probe OIDs failed in 2 passes, cached for {_NEGATIVE_CACHE_TTL:.0f}s)")
    _set_cached_version(ip, community, None, _NEGATIVE_CACHE_TTL)
    return None


# ─── SNMP encoding/decoding functions (unchanged)
def encode_oid(oid_str: str) -> bytes:
    """Encode a dotted OID string into BER bytes."""

    parts = [int(x) for x in oid_str.strip(".").split(".")]
    encoded = [40 * parts[0] + parts[1]]
    for part in parts[2:]:
        if part == 0:
            encoded.append(0)
        else:
            subids = []
            while part > 0:
                subids.append(part & 0x7F)
                part >>= 7
            subids.reverse()
            for i, s in enumerate(subids):
                encoded.append(s | 0x80 if i < len(subids) - 1 else s)
    return bytes(encoded)


def encode_length(n: int) -> bytes:
    """Encode a BER length field."""

    if n < 128:
        return bytes([n])
    elif n < 256:
        return bytes([0x81, n])
    else:
        return bytes([0x82, (n >> 8) & 0xFF, n & 0xFF])


def decode_oid_bytes(data: bytes) -> str:
    """Decode OID bytes to dot string (for walk and getnext)."""
    if not data:
        return ""
    try:
        first = data[0]
        parts = [str(first // 40), str(first % 40)]
        val = 0
        for b in data[1:]:
            val = (val << 7) | (b & 0x7F)
            if not (b & 0x80):
                parts.append(str(val))
                val = 0
        return ".".join(parts)
    except Exception:
        return ""


def _build_snmp_pdu(community: str, oid: str, request_id: int, version: int, pdu_tag: int) -> bytes:
    """Making public PDU (GET with tag 0xa0 / GETNEXT with tag 0xa1)."""
    ob = encode_oid(oid)
    oid_tlv = b'\x06' + encode_length(len(ob)) + ob
    vb = b'\x30' + encode_length(len(oid_tlv) + 2) + oid_tlv + b'\x05\x00'
    vbl = b'\x30' + encode_length(len(vb)) + vb
    rid = request_id & 0x7FFFFFFF
    rb = rid.to_bytes((rid.bit_length() + 8) // 8, 'big')
    rid_tlv = b'\x02' + encode_length(len(rb)) + rb
    pdu_body = rid_tlv + b'\x02\x01\x00\x02\x01\x00' + vbl
    pdu = bytes([pdu_tag]) + encode_length(len(pdu_body)) + pdu_body
    cb = community.encode()
    comm_tlv = b'\x04' + encode_length(len(cb)) + cb
    msg_body = b'\x02\x01\x00' + comm_tlv + pdu
    return b'\x30' + encode_length(len(msg_body)) + msg_body


def build_snmp_get_v1(community: str, oid: str, request_id: int = 1) -> bytes:
    """Build an SNMP v1 GET request packet."""

    return _build_snmp_pdu(community, oid, request_id, 1, 0xa0)


def build_snmp_get_v2c(community: str, oid: str, request_id: int = 1) -> bytes:
    """Build an SNMP v2c GET request packet."""

    return _build_snmp_pdu(community, oid, request_id, 2, 0xa0)


def build_snmp_getnext_v1(community: str, oid: str, request_id: int = 1) -> bytes:
    """Build an SNMP v1 GETNEXT request packet."""

    return _build_snmp_pdu(community, oid, request_id, 1, 0xa1)


def build_snmp_getnext_v2c(community: str, oid: str, request_id: int = 1) -> bytes:
    """Build an SNMP v2c GETNEXT request packet."""

    return _build_snmp_pdu(community, oid, request_id, 2, 0xa1)


def parse_snmp_response_debug(data: bytes, expected_request_id=None):
    """A diagnostic version of the parser that returns the SNMP response type with details."""
    try:
        pos = 0

        def rl(d, p):
            """Read a BER length field and return (length, next position)."""
            if d[p] & 0x80 == 0:
                return d[p], p + 1
            n = d[p] & 0x7F
            return int.from_bytes(d[p + 1:p + 1 + n], 'big'), p + 1 + n

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "invalid_sequence"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_version"}
        pos += 1
        version_len, pos = rl(data, pos)
        version_int = int.from_bytes(data[pos:pos + version_len], 'big')
        pos += version_len

        if data[pos] != 0x04:
            return {"status": "parse_error", "reason": "missing_community"}
        pos += 1
        comm_len, pos = rl(data, pos)
        community = data[pos:pos + comm_len].decode('utf-8', errors='ignore')
        pos += comm_len

        pdu_type = data[pos]
        if pdu_type != 0xa2:
            return {"status": "parse_error", "reason": f"unexpected_pdu_type_{hex(pdu_type)}"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_request_id"}
        pos += 1
        rid_len, pos = rl(data, pos)
        rid = int.from_bytes(data[pos:pos + rid_len], 'big')
        pos += rid_len
        if expected_request_id is not None and rid != expected_request_id:
            return {
                "status": "request_id_mismatch",
                "request_id": rid,
                "expected_request_id": expected_request_id,
                "version": version_int,
                "community": community,
            }

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_error_status"}
        pos += 1
        err_len, pos = rl(data, pos)
        error_status = int.from_bytes(data[pos:pos + err_len], 'big')
        pos += err_len

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_error_index"}
        pos += 1
        idx_len, pos = rl(data, pos)
        error_index = int.from_bytes(data[pos:pos + idx_len], 'big') if idx_len else 0
        pos += idx_len

        if error_status != 0:
            return {
                "status": "error_status",
                "error_status": error_status,
                "error_name": SNMP_ERROR_NAMES.get(error_status, f"err#{error_status}"),
                "error_index": error_index,
                "request_id": rid,
                "version": version_int,
                "community": community,
            }

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "missing_varbind_list"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "missing_varbind"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x06:
            return {"status": "parse_error", "reason": "missing_oid"}
        pos += 1
        oid_len, pos = rl(data, pos)
        oid_bytes = data[pos:pos + oid_len]
        pos += oid_len

        vt = data[pos]
        pos += 1
        vl, pos = rl(data, pos)
        vb = data[pos:pos + vl]

        result = {
            "status": "ok",
            "request_id": rid,
            "version": version_int,
            "community": community,
            "value_tag": hex(vt),
            "raw_value_hex": vb.hex(),
            "raw_oid_hex": oid_bytes.hex(),
        }

        # Response OID (for walk/getnext) — extracted before value is read
        result["response_oid"] = decode_oid_bytes(oid_bytes)

        if vt == 0x02:
            result["value"] = 0 if vl == 0 else int.from_bytes(vb, 'big', signed=True)
            result["value_type"] = "integer"
            return result
        elif vt == 0x04:
            try:
                result["value"] = vb.decode('utf-8').strip('\x00').strip()
            except Exception:
                result["value"] = vb.hex()
            result["value_type"] = "string"
            return result
        elif vt in (0x41, 0x42, 0x43, 0x44):
            result["value"] = int.from_bytes(vb, 'big')
            result["value_type"] = "unsigned"
            return result
        elif vt == 0x40:
            result["value"] = ".".join(str(b) for b in vb)
            result["value_type"] = "ipaddress"
            return result
        elif vt == 0x80:
            return {**result, "status": "no_such_object", "value": None, "value_type": "exception"}
        elif vt == 0x81:
            return {**result, "status": "no_such_instance", "value": None, "value_type": "exception"}
        elif vt == 0x82:
            return {**result, "status": "end_of_mib_view", "value": None, "value_type": "exception"}
        return {**result, "status": "unsupported_value_type", "value": None}
    except Exception as e:
        log.debug(f"SNMP parse error: {e}")
        return {"status": "parse_error", "reason": str(e)}


def snmp_getnext(ip: str, oid: str, community: str = "public",
                 port: int = 161, timeout: float = 3.0, request_id: int = 1, version: int = 2):
    """GETNEXT request; output (oid_next, value) or None."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        pkt = build_snmp_getnext_v1(community, oid, request_id) if version == 1 \
            else build_snmp_getnext_v2c(community, oid, request_id)
        s.sendto(pkt, (ip, port))
        resp, _ = s.recvfrom(65535)
        parsed = parse_snmp_response_debug(resp, expected_request_id=request_id)
        if parsed.get("status") != "ok":
            return None
        next_oid = parsed.get("response_oid")
        value = parsed.get("value")
        if not next_oid or value is None:
            return None
        return next_oid, value
    except Exception:
        return None
    finally:
        if s:
            s.close()


def snmp_walk(ip: str, prefix: str, community: str = "public",
              port: int = 161, timeout: float = 2.0, max_vars: int = 48,
              version: int | None = None):
    """walk limited a subbranch with GETNEXT; Output [(oid, value),...].

    The max_vars limit ensures that large vendor branches cause an infinite loop
    or network overhead. SNMP version comes from cache/autodetect if not present.
    """
    prefix = prefix.strip(".")
    if not prefix:
        return []
    if version not in (1, 2):
        version = _detect_snmp_version(ip, community, port, probe_timeout=1.5)
        if version is None:
            return []
    results = []
    current = prefix
    for i in range(max_vars):
        nxt = snmp_getnext(ip, current, community, port, timeout,
                           request_id=i + 1, version=version)
        if not nxt:
            break
        nxt_oid, value = nxt
        # Exit from the branch
        if nxt_oid != prefix and not nxt_oid.startswith(prefix + "."):
            break
        results.append((nxt_oid, value))
        current = nxt_oid
    return results


def snmp_get(ip: str, oid: str, community: str = "public",
             port: int = 161, timeout: float = 3.0, request_id: int = 1, version: int = 2):
    """Send one SNMP GET request and return the decoded value, or None on failure."""

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if version == 1:
            pkt = build_snmp_get_v1(community, oid, request_id)
        else:
            pkt = build_snmp_get_v2c(community, oid, request_id)
        s.sendto(pkt, (ip, port))
        resp, _ = s.recvfrom(65535)
        parsed = parse_snmp_response_debug(resp, expected_request_id=request_id)
        # ✅ Compatibility fix: some print servers/old firmwares (old models
        # (Toshiba, old JetDirect and some clones) return wrong request-id.
        # Because the UDP socket is specific to this request (ephemeral port), the received response
        # It's practically the same request — accept the value if only the RID was inconsistent.
        if parsed.get("status") == "request_id_mismatch":
            log.debug(
                "snmp_get %s %s v%s: accepting response with mismatched request id (%s→%s)",
                ip, oid, version, parsed.get("request_id"), request_id,
            )
            parsed = parse_snmp_response_debug(resp, expected_request_id=None)
        return parsed.get("value") if parsed.get("status") == "ok" else None
    except socket.timeout:
        return None
    except Exception as e:
        log.debug(f"snmp_get {ip} {oid} version {version}: {e}")
        return None
    finally:
        if s:
            s.close()



def snmp_debug_get(ip: str, oid: str, community: str = "public",
                   port: int = 161, timeout: float = 3.0, request_id: int = 1, version: int = 2):
    """SNMP diagnostic request that returns response type details."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if version == 1:
            pkt = build_snmp_get_v1(community, oid, request_id)
        else:
            pkt = build_snmp_get_v2c(community, oid, request_id)
        s.sendto(pkt, (ip, port))
        resp, _ = s.recvfrom(65535)
        parsed = parse_snmp_response_debug(resp, expected_request_id=request_id)
        parsed.update({
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
        })
        return parsed
    except socket.timeout:
        return {
            "status": "timeout",
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
        }
    except Exception as e:
        log.debug(f"snmp_debug_get {ip} {oid} version {version}: {e}")
        return {
            "status": "exception",
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
            "error": str(e),
        }
    finally:
        if s:
            s.close()


def snmp_get_with_fallback(ip: str, oid: str, community: str = "public",
                           port: int = 161, timeout: float = 3.0, request_id: int = 1,
                           version: int | None = None):
    """
    Receive SNMP with automatic detection and version cache.
    First, it determines the appropriate version (1 or 2) based on the previous test or the new test.
    """
    if version in (1, 2):
        return snmp_get(ip, oid, community, port, timeout, request_id, version=version)

    # We check the cache; If it is not available, identify it (with a short timeout)
    version = _detect_snmp_version(ip, community, port, probe_timeout=1.5)
    # If the version is not detected (None) → the device seems to be offline
    if version is None:
        return None

    # Request with preferred version
    result = snmp_get(ip, oid, community, port, timeout, request_id, version=version)
    if result is not None:
        return result

    # If the preferred version does not work (for example, the device is rebooted and v1/v2 is changed),
    # Try another version
    other_version = 2 if version == 1 else 1
    result_other = snmp_get(ip, oid, community, port, timeout, request_id, version=other_version)
    if result_other is not None:
        _set_cached_version(ip, community, other_version, _POSITIVE_CACHE_TTL)
        log.info(f"SNMP cache updated: {ip} -> v{other_version} (v{version} failed)")
        return result_other

    # ✅ Important fix: failure to read "this particular OID" should not clear or negative version cache
    # Slow! Previously a slow/protected OID (whose response was silently dropped — common behavior
    # Older Toshiba firmwares) would make the entire device offline for subsequent cycles
    # be announced, while other OIDs were responding. Version cache only by
    # _detect_snmp_version (full probe) is managed.
    log.debug(
        "snmp_get_with_fallback: OID %s on %s unreadable via v%s and v%s; version cache kept (v%s)",
        oid, ip, version, other_version, version,
    )
    return None


def snmp_get_first(ip: str, oids: list[str], community: str = "public",
                   port: int = 161, timeout: float = 3.0, version: int | None = None):
    """Returns the first OID of the responder from a list.

    Output:
      (value, oid)
    If no OID responds:
      (None, None)
    """
    for idx, oid in enumerate(oids, 1):
        value = snmp_get_with_fallback(
            ip,
            oid,
            community,
            port=port,
            timeout=timeout,
            request_id=idx,
            version=version,
        )
        if value is not None:
            return value, oid
    return None, None


def snmp_get_bulk(ip: str, oids: dict, community: str = "public") -> dict:
    """Reads several OIDs one by one (sequential GET) with automatic fallback"""
    result = {}
    for i, (key, oid) in enumerate(oids.items()):
        result[key] = snmp_get_with_fallback(ip, oid, community, request_id=i + 1)
    return result
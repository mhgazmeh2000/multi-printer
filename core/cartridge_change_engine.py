"""Generic cartridge-change evidence engine.

This module intentionally avoids model-specific assumptions. It evaluates a
set of generic cartridge signals, scores them by confidence, and emits a
CARTRIDGE_CHANGED only when the evidence is sufficient.

Event contract:
    type: CARTRIDGE_CHANGED
    details.detection_method: primary evidence method (for example ``chip_id``)
    details.detection_methods: all contributing evidence methods
    details.evidence: complete structured evidence list
    details.confidence: UNKNOWN, POSSIBLE, PROBABLE, or CONFIRMED

Legacy aliases such as ``prev_cartridge_id`` and ``cartridge_id`` are allowed
at the event boundary, but they are derived from the same evidence and never
replace the generic evidence payload.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

CONFIDENCE_LEVELS = ("UNKNOWN", "POSSIBLE", "PROBABLE", "CONFIRMED")
CONFIDENCE_SCORE = {name: idx for idx, name in enumerate(CONFIDENCE_LEVELS)}


def _normalize(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"unknown", "n/a", "na", "none", "null", "not available", "notavailable"}:
        return None
    return text


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _identity_from_record(record: Optional[Dict[str, Any]]) -> Optional[str]:
    if not record:
        return None
    for key in ("chip_id", "serial", "cartridge_model", "supply_description", "supply_counter"):
        value = _normalize(record.get(key))
        if value is not None:
            return value
    return None


def _hash_event(printer_ip: str, old_identity: str, new_identity: str, timestamp: str) -> str:
    digest = hashlib.sha256()
    digest.update(str(printer_ip).encode("utf-8"))
    digest.update(b"|")
    digest.update(str(old_identity).encode("utf-8"))
    digest.update(b"|")
    digest.update(str(new_identity).encode("utf-8"))
    digest.update(b"|")
    digest.update(str(timestamp).encode("utf-8"))
    return digest.hexdigest()


def _record_evidence(name: str, prev_value: Any, curr_value: Any, confidence: str, source: str, reason: str) -> Dict[str, Any]:
    return {
        "name": name,
        "prev": prev_value,
        "curr": curr_value,
        "confidence": confidence,
        "source": source,
        "reason": reason,
    }


def evaluate_cartridge_change(prev: Optional[Dict[str, Any]], curr: Optional[Dict[str, Any]], *,
                              reboot_detected: bool = False, poll_gap_ok: bool = True) -> Dict[str, Any]:
    """Evaluate whether a cartridge change is likely using multiple evidence signals.

    Returns a structured result that includes:
      - should_emit: bool
      - confidence: UNKNOWN | POSSIBLE | PROBABLE | CONFIRMED
      - evidence: list of evidence objects
      - detection_method: comma-separated list of names
      - identity information for event storage
    """
    prev = prev or {}
    curr = curr or {}

    result = {
        "should_emit": False,
        "confidence": "UNKNOWN",
        "evidence": [],
        "detection_method": "",
        "old_identity": None,
        "new_identity": None,
        "event": None,
    }

    if not poll_gap_ok:
        result["confidence"] = "UNKNOWN"
        result["evidence"] = [{
            "name": "poll_gap",
            "prev": prev,
            "curr": curr,
            "confidence": "UNKNOWN",
            "source": "poll",
            "reason": "SNMP poll gap or missing sample; no confident cartridge change decision",
        }]
        return result

    if prev == curr:
        result["confidence"] = "UNKNOWN"
        return result

    evidence: List[Dict[str, Any]] = []

    def add_evidence(name: str, prev_value: Any, curr_value: Any, confidence: str, source: str, reason: str):
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "UNKNOWN"
        evidence.append(_record_evidence(name, prev_value, curr_value, confidence, source, reason))

    prev_chip = _normalize(prev.get("chip_id"))
    curr_chip = _normalize(curr.get("chip_id"))
    identity_is_static = str(curr.get("signal_quality") or "").strip().lower() == "static"
    if prev_chip and curr_chip and prev_chip != curr_chip and not identity_is_static:
        add_evidence("chip_id", prev_chip, curr_chip, "CONFIRMED", "chip_id", "Chip ID changed between samples")

    prev_serial = _normalize(prev.get("serial"))
    curr_serial = _normalize(curr.get("serial"))
    if prev_serial and curr_serial and prev_serial != curr_serial and not identity_is_static:
        add_evidence("serial", prev_serial, curr_serial, "PROBABLE", "serial", "Serial changed between samples")

    prev_model = _normalize(prev.get("cartridge_model"))
    curr_model = _normalize(curr.get("cartridge_model"))
    if prev_model and curr_model and prev_model != curr_model:
        add_evidence("cartridge_model", prev_model, curr_model, "POSSIBLE", "model", "Cartridge model changed")

    prev_desc = _normalize(prev.get("supply_description"))
    curr_desc = _normalize(curr.get("supply_description"))
    if prev_desc and curr_desc and prev_desc != curr_desc:
        add_evidence("supply_description", prev_desc, curr_desc, "POSSIBLE", "description", "Supply description changed")

    prev_gen = _as_int(prev.get("generation"))
    curr_gen = _as_int(curr.get("generation"))
    if prev_gen is not None and curr_gen is not None and prev_gen != curr_gen:
        add_evidence("generation", prev_gen, curr_gen, "POSSIBLE", "generation", "Generation counter changed")

    prev_counter = _as_int(prev.get("supply_counter"))
    curr_counter = _as_int(curr.get("supply_counter"))
    if prev_counter is not None and curr_counter is not None and prev_counter > 0 and curr_counter < prev_counter:
        if not reboot_detected:
            add_evidence(
                "supply_counter",
                prev_counter,
                curr_counter,
                "POSSIBLE",
                "supply_counter",
                "Supply/replacement counter reset or rolled over",
            )

    prev_pages = _as_int(prev.get("page_counter"))
    curr_pages = _as_int(curr.get("page_counter"))
    if prev_pages is not None and curr_pages is not None and prev_pages > 0 and curr_pages < prev_pages:
        if not reboot_detected:
            add_evidence(
                "page_counter",
                prev_pages,
                curr_pages,
                "POSSIBLE",
                "page_counter",
                "Page counter dropped below previous observed value",
            )

    prev_level = _as_int(prev.get("remaining_level"))
    curr_level = _as_int(curr.get("remaining_level"))
    if prev_level is not None and curr_level is not None:
        level_jump = curr_level - prev_level
        if prev_level <= 35 and curr_level >= 80 and level_jump >= 30:
            add_evidence(
                "remaining_level",
                prev_level,
                curr_level,
                "POSSIBLE",
                "remaining_level",
                "Remaining level jumped sharply in one poll",
            )

    prev_life = _as_int(prev.get("supply_life"))
    curr_life = _as_int(curr.get("supply_life"))
    if prev_life is not None and curr_life is not None:
        life_jump = curr_life - prev_life
        if life_jump >= 30:
            add_evidence(
                "supply_life",
                prev_life,
                curr_life,
                "POSSIBLE",
                "supply_life",
                "Supply life jumped sharply after replacement",
            )

    if reboot_detected:
        for ev in evidence:
            if ev["name"] in {"supply_counter", "page_counter", "remaining_level", "supply_life", "generation"}:
                ev["confidence"] = "UNKNOWN"
                ev["reason"] = "Reboot detected; reset-like values are ignored as cartridge-change evidence"

    # High-confidence identity wins.
    if any(ev["name"] == "chip_id" and ev["confidence"] == "CONFIRMED" for ev in evidence):
        result["confidence"] = "CONFIRMED"
        result["should_emit"] = True
    elif any(ev["name"] == "serial" and ev["confidence"] == "PROBABLE" for ev in evidence) and not reboot_detected:
        result["confidence"] = "PROBABLE"
        result["should_emit"] = True
    elif (
        not reboot_detected
        and any(ev["name"] == "supply_counter" and ev["confidence"] == "POSSIBLE" for ev in evidence)
        and any(ev["name"] == "remaining_level" and ev["confidence"] == "POSSIBLE" for ev in evidence)
        and any(ev["name"] == "supply_life" and ev["confidence"] == "POSSIBLE" for ev in evidence)
    ):
        result["confidence"] = "PROBABLE"
        result["should_emit"] = True
    elif (
        not reboot_detected
        and (
            any(ev["name"] in {"supply_counter", "page_counter"} for ev in evidence)
            or (
                any(ev["name"] == "remaining_level" for ev in evidence)
                and any(ev["name"] == "supply_life" for ev in evidence)
                and any(ev["name"] == "supply_counter" for ev in evidence)
            )
        )
        and sum(1 for ev in evidence if ev["confidence"] in {"POSSIBLE", "PROBABLE"}) >= 2
    ):
        result["confidence"] = "POSSIBLE"
        result["should_emit"] = True
    else:
        result["confidence"] = "UNKNOWN"
        result["should_emit"] = False

    if reboot_detected and not any(ev["name"] == "chip_id" and ev["confidence"] == "CONFIRMED" for ev in evidence):
        result["should_emit"] = False
        result["confidence"] = "UNKNOWN"

    result["evidence"] = evidence
    detection_methods = sorted({ev["name"] for ev in evidence if ev["confidence"] != "UNKNOWN"})
    primary_order = ("chip_id", "serial", "supply_counter", "page_counter",
                     "remaining_level", "supply_life", "generation",
                     "cartridge_model", "supply_description")
    primary_method = next((name for name in primary_order if name in detection_methods), "")
    result["detection_methods"] = detection_methods
    result["detection_method"] = primary_method

    # Only emit if overall confidence is non-unknown.
    if result["confidence"] == "UNKNOWN":
        result["should_emit"] = False

    old_identity = _identity_from_record(prev)
    new_identity = _identity_from_record(curr)
    result["old_identity"] = old_identity
    result["new_identity"] = new_identity

    if result["should_emit"] and result["old_identity"] and result["new_identity"]:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
        result["event"] = {
            "printer_ip": (prev.get("printer_ip") or curr.get("printer_ip") or "unknown"),
            "brand": prev.get("brand") or curr.get("brand") or "",
            "model": prev.get("model") or curr.get("model") or "",
            "color": prev.get("color") or curr.get("color") or "",
            "old_identity": result["old_identity"],
            "new_identity": result["new_identity"],
            "evidence": result["evidence"],
            "evidence_source": ",".join(sorted({ev["source"] for ev in result["evidence"] if ev["confidence"] != "UNKNOWN"})),
            "confidence": result["confidence"],
            "detection_method": result["detection_method"],
            "detection_methods": result.get("detection_methods", []),
            "timestamp": timestamp,
            "event_id": _hash_event(
                (prev.get("printer_ip") or curr.get("printer_ip") or "unknown"),
                result["old_identity"],
                result["new_identity"],
                timestamp,
            ),
        }

    return result

    old_identity = _identity_from_record(prev)
    new_identity = _identity_from_record(curr)
    if old_identity is not None and new_identity is not None and old_identity != new_identity:
        result["old_identity"] = old_identity
        result["new_identity"] = new_identity
    else:
        result["old_identity"] = old_identity
        result["new_identity"] = new_identity

    if result["should_emit"] and result["old_identity"] and result["new_identity"]:
        timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        result["event"] = {
            "printer_ip": (prev.get("printer_ip") or curr.get("printer_ip") or "unknown"),
            "brand": prev.get("brand") or curr.get("brand") or "",
            "model": prev.get("model") or curr.get("model") or "",
            "color": prev.get("color") or curr.get("color") or "",
            "old_identity": result["old_identity"],
            "new_identity": result["new_identity"],
            "evidence": evidence,
            "evidence_source": ",".join(sorted({ev["source"] for ev in evidence if ev["confidence"] != "UNKNOWN"})),
            "confidence": result["confidence"],
            "detection_method": result["detection_method"],
            "detection_methods": result.get("detection_methods", []),
            "timestamp": timestamp,
            "event_id": _hash_event(
                (prev.get("printer_ip") or curr.get("printer_ip") or "unknown"),
                result["old_identity"],
                result["new_identity"],
                timestamp,
            ),
        }

    return result

"""
Yield Engine
============

Principled calculation of cartridge capacity (yield_per_page), per cartridge and per color.

Features:
- anchor-based learning for low-usage printers
- support for color cartridges
- confidence/source for each cartridge
- auditable snapshot and sample
- publishes a valid yield to cartridges with the same model and the same name

This module is intentionally kept independent of the legacy logic in `printer_counters.yield_per_page`
so that the migration can be done without risk and the previous data is not corrupted.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from core.database import db_connection
from core.db import BACKEND_POSTGRESQL, UndefinedTable
from core.db import upsert_sql as _upsert_sql
from core.db.schema import YIELD_TABLES as _PG_YIELD_TABLES, verify_postgres_tables
from core.time_utils import as_utc_datetime, utc_now_iso
from core.yield_estimator import (
    PosteriorYieldDecision,
    SupplyReading,
    estimate_supply_series,
    get_brand_spec,
    snapshot_is_usable,
)

log = logging.getLogger("PrinterMonitor")

YIELD_ESTIMATOR_ENV_VAR = "YIELD_ESTIMATOR"


def _is_v2_estimator_enabled() -> bool:
    """Return True when interval-censored v2 estimator drives cartridge_state persistence."""
    return os.environ.get(YIELD_ESTIMATOR_ENV_VAR, "").strip().lower() == "v2"

DEFAULT_YIELD_PER_PAGE = 2000
MIN_PAGES_FOR_SAMPLE = 10
MIN_ESTIMATED_YIELD = 300
MAX_ESTIMATED_YIELD = 600000
REFILL_JUMP_PERCENT = 20
SMALL_LEVEL_BOUNCE_PERCENT = 5
SNAPSHOT_MIN_SECONDS = 6 * 3600
CATALOG_ENV_VAR = "CARTRIDGE_YIELD_CATALOG"
DEFAULT_CATALOG_FILE = Path(__file__).resolve().parents[1] / "cartridge_yield_catalog.json"

# ─── Anti-noise rules and principled capacity normalization (real data: 6-day fleet log) ──────
# A single poll that lowers the level by more than this value while almost no pages
# were printed is physically impossible (multi-thousand-page cartridges) and is read noise.
# Real case: TOSHIBA e-STUDIO306 with 100→8→100 blips that poisoned
# cycle_learn with yields of 300-400 pages.
LEVEL_NOISE_DROP_PERCENT = 25
LEVEL_NOISE_MAX_PAGES = 5
# A quantized 1% step with few pages makes the yield estimate error explode
# (real case: Δp=11 / Δt=1 → yield=1100 for Cartridge 737 with a real capacity of 2400).
QUANT_MIN_PAGES_1PCT = 150
# When we have a "reference" capacity (catalog/declared by the device), the learned value is only shown
# within this band unless a multi-sample low-variance regime shift (e.g. non-OEM/XL cartridge) is proven.
LEARNED_DEVIATION_LIMIT = 0.40
CYCLE_RATED_BAND = (0.30, 2.5)
REGIME_SHIFT_MIN_SAMPLES = 6
REGIME_SHIFT_MIN_DROP_PCT = 25
MIN_PARTIAL_CYCLE_DROP_PCT = 20
MIN_ZERO_CYCLE_DROP_PCT = 15
COARSE_STEP_MEDIUM_DROP_PCT = 10
COARSE_STEP_MEDIUM_MIN_PAGES = 250

# ─── Fix 2: reference-scaled 1% gate ─────────────────────────────────────────
# A 1% step needs at least 1.5% of the reference capacity in pages. 1.5 is a margin chosen for this fix,
# not a derived constant. QUANT_MIN_PAGES_1PCT (150) stays the floor, so small cartridges are unchanged.
QUANT_1PCT_REFERENCE_FACTOR = 1.5
# Without any reference capacity a 1% step needs 300 pages (the value requested for this fix; not derived).
QUANT_1PCT_NO_REFERENCE_PAGES = 300

# ─── Fix 1: variance caps on confidence (coefficient of variation of recent accepted estimates) ─────────
CONFIDENCE_CV_MEDIUM = 0.20    # CV above this caps confidence at medium
CONFIDENCE_CV_LOW = 0.40       # CV above this caps confidence at low
CONFIDENCE_CV_MIN_SAMPLES = 3  # with fewer estimates no variance cap is applied

# ─── Frozen-chip detection, chip quarantine, overrun alert & replacement reasons ───
FROZEN_CHIP_FINE_PCT = 0.20
FROZEN_CHIP_FINE_MIN_PAGES = 250
FROZEN_CHIP_COARSE_PCT = 0.35
FROZEN_CHIP_COARSE_MIN_PAGES = 400
OVERRUN_ALERT_PCT = 0.15
OVERRUN_ALERT_MIN_PAGES = 150
DIRTY_PARTIAL_MIN_DROP_PCT = 25

_REJECT_QUARANTINED_CHIP = "quarantined_chip"
_REJECT_FROZEN_CHIP = "frozen_chip"
_CYCLE_SOURCES_FOR_MEDIAN = ("cycle_learn", "cycle_manual_empty", "cycle_dirty_partial")

_COLOR_ORDER = ("black", "cyan", "magenta", "yellow", "drum")


def _frozen_chip_page_threshold(printer_model: str, ref_capacity: Optional[int]) -> int:
    """Pages without any raw_level change above 0% before a chip is flagged as frozen."""
    spec = get_brand_spec(printer_model or "")
    cap = max(int(ref_capacity or DEFAULT_YIELD_PER_PAGE), MIN_ESTIMATED_YIELD)
    if spec.typical_step_pct >= 5.0:
        return max(FROZEN_CHIP_COARSE_MIN_PAGES, int(round(FROZEN_CHIP_COARSE_PCT * cap)))
    return max(FROZEN_CHIP_FINE_MIN_PAGES, int(round(FROZEN_CHIP_FINE_PCT * cap)))


def _normalize_replacement_reason(reason: Optional[str]) -> Optional[str]:
    """Normalize manual cartridge reset/replacement reason ('empty' | 'dirty' | None)."""
    if reason is None:
        return None
    r = str(reason).strip().lower()
    if not r or r == "auto":
        return None
    if r in ("empty", "depleted", "low_toner", "faded"):
        return "empty"
    if r in ("dirty", "defect", "streaks", "smudge", "drum", "preventive", "partial"):
        return "dirty"
    return None

# Sub-percent anchor cache for devices reporting high-resolution supply capacities (max > 100):
# maps (ip, color) -> (anchor_counter, exact_level_pct, cartridge_key)
_EXACT_ANCHOR_CACHE: Dict[Tuple[str, str], Tuple[int, float, str]] = {}


def _now() -> str:
    return utc_now_iso()


def _norm_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9آ-یءئؤإأۀة،._\- ]+", "", value)
    return value or "unknown"


def build_cartridge_key(printer_model: str, cartridge_name: str, color: str) -> str:
    """Yield sharing key: device model, cartridge name and color."""
    return f"{_norm_text(printer_model)}|{_norm_text(cartridge_name)}|{_norm_text(color)}"


def _to_int(value, default=None):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _valid_level(value) -> Optional[int]:
    level = _to_int(value)
    if level is None or level < 0 or level > 100:
        return None
    return level


def _valid_device_capacity(value) -> Optional[int]:
    """Capacity declared by the device; we only accept logical values that are larger than a percentage."""
    capacity = _to_int(value)
    if capacity is None:
        return None
    # max=100 usually means a percentage, not a page capacity. Very large values are also suspicious.
    if 101 <= capacity <= MAX_ESTIMATED_YIELD:
        return capacity
    return None


def _parse_dt(value: str):
    """
    Stored timestamp string (UTC/Z) ⇒ timezone-aware datetime; empty/invalid ⇒ ``None``.

    Phase E.3: this used to be a raw ``datetime.fromisoformat`` (it returned a naive value and was not
    comparable with aware values). This function currently has no consumer in the code, but
    since its name is "database timestamp parsing", it was aligned with the other readers.
    """
    if not value:
        return None
    try:
        return as_utc_datetime(value)
    except ValueError:
        return None


def _catalog_path() -> Path:
    override = os.getenv(CATALOG_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_CATALOG_FILE


def _load_yield_catalog() -> list:
    """Reads the local cartridge-capacity catalog. A corrupted file must not stop polling."""
    path = _catalog_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", data if isinstance(data, list) else [])
        return entries if isinstance(entries, list) else []
    except Exception as exc:
        log.warning("Failed to load cartridge yield catalog %s: %s", path, exc)
        return []


# Needles this short that contain a digit (e.g. "87a", "83a", "737") must match whole tokens.
# As plain substrings, "87a" is found inside "cf387a", so an unrelated cartridge could select
# the CF287A entry. Longer needles keep plain substring matching.
_SHORT_DIGIT_NEEDLE_MAX_LEN = 3


def _needle_found(haystack: str, needle_norm: str) -> bool:
    if len(needle_norm) <= _SHORT_DIGIT_NEEDLE_MAX_LEN and any(ch.isdigit() for ch in needle_norm):
        return re.search(r"(?<!\w)" + re.escape(needle_norm) + r"(?!\w)", haystack) is not None
    return needle_norm in haystack


def _contains_any(haystack: str, needles) -> bool:
    if not needles:
        return True
    haystack = _norm_text(haystack)
    if isinstance(needles, str):
        needles = [needles]
    for needle in needles:
        needle_norm = _norm_text(str(needle))
        if needle_norm and needle_norm != "unknown" and _needle_found(haystack, needle_norm):
            return True
    return False


def _catalog_entry_yield(entry: dict) -> Optional[int]:
    """Selects the yield value from the catalog based on market/consumption conditions.

    Modes are configurable via ENV:
      CARTRIDGE_YIELD_MODE=oem|compatible|refill|local
      CARTRIDGE_YIELD_FACTOR=0.70  # applies a conservative factor to the catalog value

    If the selected mode field does not exist, it falls back to `yield_per_page`.
    """
    mode = os.getenv("CARTRIDGE_YIELD_MODE", "local").strip().lower() or "local"
    mode_keys = {
        "oem": ("yield_per_page_oem", "yield_per_page"),
        "compatible": ("yield_per_page_compatible", "yield_per_page_local", "yield_per_page"),
        "refill": ("yield_per_page_refill", "yield_per_page_local", "yield_per_page_compatible", "yield_per_page"),
        "local": ("yield_per_page_local", "yield_per_page_iran", "yield_per_page_refill", "yield_per_page_compatible", "yield_per_page"),
    }.get(mode, ("yield_per_page_local", "yield_per_page",))

    ypp = None
    for key in mode_keys:
        ypp = _valid_device_capacity(entry.get(key))
        if ypp:
            break
    if not ypp:
        return None

    try:
        factor = float(os.getenv("CARTRIDGE_YIELD_FACTOR", "1") or "1")
    except (TypeError, ValueError):
        factor = 1.0
    if factor > 0 and factor != 1:
        ypp = int(round(ypp * factor))
    return _valid_device_capacity(ypp)


def _catalog_priority(entry: dict) -> int:
    """Optional explicit tie-break for equal-score catalog matches; a higher value wins. Defaults to 0."""
    try:
        return int(entry.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def _catalog_lookup(printer_model: str, cartridge_name: str, color: str) -> Optional[dict]:
    """Finds the best match in the catalog.

    Conservative matching: if the model/cartridge/color condition is present in the entry, it must match.
    Ranking: score first, then the optional "priority" field. A tie on both keeps the entry that
    comes first in the catalog file (logged at DEBUG).
    """
    model_norm = _norm_text(printer_model)
    cart_norm = _norm_text(cartridge_name)
    color_norm = _norm_text(color)
    best = None
    best_key = None
    for entry in _load_yield_catalog():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        ypp = _catalog_entry_yield(entry)
        if not ypp:
            continue
        match = entry.get("match") or {}
        entry_color = _norm_text(match.get("color") or entry.get("color") or "")
        if entry_color and entry_color != "unknown" and entry_color != color_norm:
            continue
        if not _contains_any(model_norm, match.get("model_contains")):
            continue
        if not _contains_any(cart_norm, match.get("cartridge_contains")):
            continue
        score = 0
        if entry_color and entry_color != "unknown":
            score += 2
        if match.get("model_contains"):
            score += 3
        if match.get("cartridge_contains"):
            score += 5
        # Model plus cartridge is the most specific rule: the bonus keeps it ahead of any single-field rule.
        if match.get("model_contains") and match.get("cartridge_contains"):
            score += 10
        key = (score, _catalog_priority(entry))
        if best is None or key > best_key:
            best_key = key
            best = {
                "yield_per_page": ypp,
                "confidence": entry.get("confidence") or "medium",
                "catalog_id": entry.get("id") or entry.get("cartridge") or "catalog",
            }
        elif key == best_key:
            log.debug("catalog tie for %s|%s|%s: keeping %s over %s (score=%s, priority=%s)",
                      model_norm, cart_norm, color_norm, best["catalog_id"], entry.get("id"),
                      score, key[1])
    return best


def _confidence_rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get((conf or "low").lower(), 0)


def ensure_yield_tables() -> None:
    """Creates the Yield Engine tables. Can be run repeatedly and safely.

    On PostgreSQL (phase E.4.5) this function is a **no-op**: the schema comes from Alembic and
    two DDL versions would diverge. It only verifies that the four tables exist.
    """
    with db_connection(commit=True) as conn:
        if conn.backend == BACKEND_POSTGRESQL:
            verify_postgres_tables(conn, _PG_YIELD_TABLES, context="ensure_yield_tables")
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cartridge_state (
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                current_level INTEGER,
                last_valid_level INTEGER,
                last_counter INTEGER,
                anchor_counter INTEGER,
                anchor_level INTEGER,
                anchor_timestamp TEXT,
                yield_per_page INTEGER DEFAULT 2000,
                yield_source TEXT DEFAULT 'default',
                confidence TEXT DEFAULT 'low',
                sample_count INTEGER DEFAULT 0,
                total_weight REAL DEFAULT 0,
                learned_at TEXT,
                last_refill_at TEXT,
                last_refill_counter INTEGER,
                pending_refill_level INTEGER,
                pending_refill_counter INTEGER,
                pending_refill_timestamp TEXT,
                cycle_start_counter INTEGER,
                cycle_start_level INTEGER,
                cycle_start_timestamp TEXT,
                zero_reached_counter INTEGER,
                zero_reached_timestamp TEXT,
                pages_after_zero INTEGER DEFAULT 0,
                cycle_status TEXT DEFAULT 'active',
                force_estimate INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (printer_ip, color)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yield_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                start_counter INTEGER,
                end_counter INTEGER,
                start_level INTEGER,
                end_level INTEGER,
                pages_delta INTEGER,
                toner_drop INTEGER,
                estimated_yield INTEGER,
                counter_basis TEXT,
                confidence_weight REAL,
                accepted INTEGER DEFAULT 1,
                reject_reason TEXT,
                source TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toner_snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                timestamp TEXT NOT NULL,
                print_total INTEGER,
                full_color INTEGER,
                black_white INTEGER,
                usage_counter INTEGER,
                toner_level INTEGER,
                raw_level INTEGER,
                source TEXT,
                valid INTEGER DEFAULT 1,
                reject_reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cartridge_yield_profiles (
                cartridge_key TEXT PRIMARY KEY,
                printer_model TEXT,
                cartridge_name TEXT,
                color TEXT,
                yield_per_page INTEGER NOT NULL,
                confidence TEXT NOT NULL DEFAULT 'high',
                sample_count INTEGER DEFAULT 0,
                total_weight REAL DEFAULT 0,
                source_printer_ip TEXT,
                updated_at TEXT
            )
            """
        )
        # safe migration for previous databases
        for column_sql in (
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_counter INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_level INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_timestamp TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN zero_reached_counter INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN zero_reached_timestamp TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN pages_after_zero INTEGER DEFAULT 0",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_status TEXT DEFAULT 'active'",
        ):
            try:
                conn.execute(column_sql)
            except Exception:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cartridge_state_key ON cartridge_state(cartridge_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yield_samples_key ON yield_samples(cartridge_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toner_snapshots_v2_ip_color_ts ON toner_snapshots_v2(printer_ip, color, timestamp)")


def _row_to_state(row) -> Optional[dict]:
    if not row:
        return None
    cols = [
        "printer_ip", "color", "printer_model", "cartridge_name", "cartridge_key",
        "current_level", "last_valid_level", "last_counter", "anchor_counter", "anchor_level",
        "anchor_timestamp", "yield_per_page", "yield_source", "confidence", "sample_count",
        "total_weight", "learned_at", "last_refill_at", "last_refill_counter",
        "pending_refill_level", "pending_refill_counter", "pending_refill_timestamp",
        "cycle_start_counter", "cycle_start_level", "cycle_start_timestamp",
        "zero_reached_counter", "zero_reached_timestamp", "pages_after_zero", "cycle_status",
        "force_estimate", "updated_at",
    ]
    return dict(zip(cols, row))


def _load_state(conn, ip: str, color: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
               current_level, last_valid_level, last_counter, anchor_counter, anchor_level,
               anchor_timestamp, yield_per_page, yield_source, confidence, sample_count,
               total_weight, learned_at, last_refill_at, last_refill_counter,
               pending_refill_level, pending_refill_counter, pending_refill_timestamp,
               cycle_start_counter, cycle_start_level, cycle_start_timestamp,
               zero_reached_counter, zero_reached_timestamp, pages_after_zero, cycle_status,
               force_estimate, updated_at
        FROM cartridge_state
        WHERE printer_ip = ? AND color = ?
        """,
        (ip, color),
    ).fetchone()
    return _row_to_state(row)


def _load_profile(conn, cartridge_key: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT cartridge_key, printer_model, cartridge_name, color, yield_per_page,
               confidence, sample_count, total_weight, source_printer_ip, updated_at
        FROM cartridge_yield_profiles
        WHERE cartridge_key = ?
        """,
        (cartridge_key,),
    ).fetchone()
    if not row:
        return None
    return {
        "cartridge_key": row[0],
        "printer_model": row[1],
        "cartridge_name": row[2],
        "color": row[3],
        "yield_per_page": row[4],
        "confidence": row[5],
        "sample_count": row[6],
        "total_weight": row[7],
        "source_printer_ip": row[8],
        "updated_at": row[9],
    }


#: ``cartridge_state`` columns written by ``_upsert_state`` — **all 31 columns**
#: of the table (parity lock in tests/test_upsert_cross_backend.py). "INSERT OR REPLACE"
#: is equivalent to "ON CONFLICT DO UPDATE" only when all columns are written.
_STATE_UPSERT_COLUMNS = (
    "printer_ip", "color", "printer_model", "cartridge_name", "cartridge_key",
    "current_level", "last_valid_level", "last_counter", "anchor_counter", "anchor_level",
    "anchor_timestamp", "yield_per_page", "yield_source", "confidence", "sample_count",
    "total_weight", "learned_at", "last_refill_at", "last_refill_counter",
    "pending_refill_level", "pending_refill_counter", "pending_refill_timestamp",
    "cycle_start_counter", "cycle_start_level", "cycle_start_timestamp",
    "zero_reached_counter", "zero_reached_timestamp", "pages_after_zero", "cycle_status",
    "force_estimate", "updated_at",
)


def _upsert_state(conn, state: dict) -> None:
    # Phase E.5: the statement comes from the cross-backend helper (on PG, "INSERT OR REPLACE" failed with 42601;
    # 20 failures in the E.5 measurement came from this one pattern alone).
    conn.execute(
        _upsert_sql("cartridge_state", _STATE_UPSERT_COLUMNS, ("printer_ip", "color"),
                    backend=conn.backend),
        _upsert_state_params(state),
    )


def _upsert_state_params(state: dict) -> tuple:
    """Values of ``_STATE_UPSERT_COLUMNS``, in the same order (defaults are exactly as before)."""
    return (
        state.get("printer_ip"), state.get("color"), state.get("printer_model"),
        state.get("cartridge_name"), state.get("cartridge_key"), state.get("current_level"),
        state.get("last_valid_level"), state.get("last_counter"), state.get("anchor_counter"),
        state.get("anchor_level"), state.get("anchor_timestamp"), state.get("yield_per_page", DEFAULT_YIELD_PER_PAGE),
        state.get("yield_source", "default"), state.get("confidence", "low"), state.get("sample_count", 0),
        state.get("total_weight", 0.0), state.get("learned_at"), state.get("last_refill_at"),
        state.get("last_refill_counter"), state.get("pending_refill_level"), state.get("pending_refill_counter"),
        state.get("pending_refill_timestamp"), state.get("cycle_start_counter"), state.get("cycle_start_level"),
        state.get("cycle_start_timestamp"), state.get("zero_reached_counter"), state.get("zero_reached_timestamp"),
        state.get("pages_after_zero", 0), state.get("cycle_status", "active"),
        state.get("force_estimate", 0), state.get("updated_at"),
    )


def _record_snapshot(conn, ip: str, color: str, printer_model: str, cartridge_name: str,
                     cartridge_key: str, timestamp: str, counters: dict,
                     usage_counter: Optional[int], level: Optional[int], raw_level,
                     source: str, valid: bool = True, reject_reason: str = None) -> None:
    conn.execute(
        """
        INSERT INTO toner_snapshots_v2 (
            printer_ip, color, printer_model, cartridge_name, cartridge_key, timestamp,
            print_total, full_color, black_white, usage_counter, toner_level, raw_level,
            source, valid, reject_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ip, color, printer_model, cartridge_name, cartridge_key, timestamp,
            counters.get("total"), counters.get("full_color"), counters.get("black_white"),
            usage_counter, level, raw_level, source, 1 if valid else 0, reject_reason,
        ),
    )


def _record_sample(conn, *, ip: str, color: str, printer_model: str, cartridge_name: str,
                   cartridge_key: str, start_counter, end_counter, start_level, end_level,
                   pages_delta, toner_drop, estimated_yield, counter_basis: str,
                   weight: float, accepted: bool, reject_reason: str = None,
                   source: str = "anchor") -> None:
    conn.execute(
        """
        INSERT INTO yield_samples (
            printer_ip, color, printer_model, cartridge_name, cartridge_key,
            start_counter, end_counter, start_level, end_level, pages_delta,
            toner_drop, estimated_yield, counter_basis, confidence_weight,
            accepted, reject_reason, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ip, color, printer_model, cartridge_name, cartridge_key,
            start_counter, end_counter, start_level, end_level, pages_delta,
            toner_drop, estimated_yield, counter_basis, weight,
            # Phase E.4.1: accepted is BOOLEAN on PG ⇒ bool, not int.
            bool(accepted), reject_reason, source, _now(),
        ),
    )


def _compute_usage_counter(color: str, counters: dict, device_type: str) -> Tuple[Optional[int], str, float]:
    """Chooses the right counter for each color + confidence factor, validating counter consistency."""
    raw_total = _to_int(counters.get("total"))
    raw_fc = _to_int(counters.get("full_color"))
    raw_bw = _to_int(counters.get("black_white"))
    total = raw_total if raw_total is not None and raw_total >= 0 else None
    full_color = raw_fc if raw_fc is not None and raw_fc >= 0 else None
    black_white = raw_bw if raw_bw is not None and raw_bw >= 0 else None
    color = (color or "black").lower()
    device_type = (device_type or "unknown").lower()

    # Guard against corrupted full_color counter exceeding total counter on color MFPs
    if full_color is not None and total is not None and full_color > int(total * 1.02) + 25:
        log.debug("counter consistency guard: full_color=%s exceeds total=%s; falling back", full_color, total)
        full_color = None

    if color in ("cyan", "magenta", "yellow"):
        if full_color is not None:
            return full_color, "full_color", 1.0
        if total is not None:
            return total, "total_for_color", 0.45
        return None, "missing", 0.0

    if color == "black":
        # For black, total is exact on mono printers and conservative on color printers.
        if total is not None:
            quality = 1.0 if device_type != "color" else 0.75
            return total, "total_for_black", quality
        if black_white is not None:
            quality = 1.0 if device_type != "color" else 0.9
            return black_white, "black_white", quality
        return None, "missing", 0.0

    # drum/opc/imaging/developer and other long-life consumables: tracked via total counter.
    if total is not None:
        quality = 0.65 if device_type != "color" else 0.55
        return total, "total_for_supply", quality
    if black_white is not None:
        return black_white, "total_for_supply", 0.5
    return None, "missing", 0.0


def _sample_weight(pages_delta: int, toner_drop: float, counter_quality: float) -> float:
    if pages_delta <= 0 or toner_drop <= 0 or counter_quality <= 0:
        return 0.0
    page_factor = min(1.0, max(0.2, pages_delta / 100.0))
    drop_factor = min(5.0, float(toner_drop))
    return round(page_factor * drop_factor * counter_quality, 3)


def _min_samples_for_high(color) -> int:
    """Accepted samples needed before a learned value may be labelled high: 4 for black, 8 for CMY."""
    return 8 if str(color or "").lower() in ("cyan", "magenta", "yellow") else 4


def _profile_is_trusted(profile: dict, color) -> bool:
    """A shared profile can only be inherited when it was learned from enough samples (Fix 1)."""
    return int(profile.get("sample_count") or 0) >= _min_samples_for_high(color)


def _sample_cv(estimates) -> Optional[float]:
    """Coefficient of variation (sample std / mean) of positive estimates; None with too few values."""
    vals = [float(v) for v in estimates if v and float(v) > 0]
    n = len(vals)
    if n < CONFIDENCE_CV_MIN_SAMPLES:
        return None
    mean = sum(vals) / n
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return math.sqrt(var) / mean


_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _cap_confidence_by_variance(confidence: str, estimates) -> str:
    """Lowers confidence when the recent accepted samples disagree with each other (Fix 1)."""
    cv = _sample_cv(estimates)
    if cv is None:
        return confidence
    if cv > CONFIDENCE_CV_LOW:
        cap = "low"
    elif cv > CONFIDENCE_CV_MEDIUM:
        cap = "medium"
    else:
        return confidence
    return cap if _CONFIDENCE_ORDER.get(confidence, 0) > _CONFIDENCE_ORDER[cap] else confidence


def _quant_min_pages_1pct(rated_capacity) -> int:
    """Pages needed before a 1% step is trusted: scaled by the reference capacity (Fix 2)."""
    if rated_capacity and rated_capacity > 0:
        scaled = round(rated_capacity * 0.01 * QUANT_1PCT_REFERENCE_FACTOR, 6)  # round first: avoid float ceil drift
        return max(QUANT_MIN_PAGES_1PCT, int(math.ceil(scaled)))
    return QUANT_1PCT_NO_REFERENCE_PAGES


def _derive_confidence(sample_count: int, total_weight: float, counter_basis: str = "",
                       *, max_single_drop: float = 0.0, max_single_pages: int = 0) -> str:
    # Low-usage printers may have each sample drop only 1% and about 50 pages;
    # so for mono/black with an acceptable counter, 4 independent samples are enough for high.
    # For CMY colors when we only have total_for_color, we do not give high because the counter is not dedicated.
    basis = str(counter_basis or "")
    if basis == "full_color":
        # For CMY, even with a color counter, page coverage varies; we give high later.
        if sample_count >= 8 and total_weight >= 5.0:
            return "high"
    elif sample_count >= 4 and total_weight >= 2.0 and not basis.startswith("total_for_color"):
        return "high"
    if sample_count >= 2 and total_weight >= 1.0:
        return "medium"
    # Coarse-step printers (10%/20%/25% increments on Kyocera/Brother/Lexmark/Ricoh/Canon) or closed cycles:
    # a single large, high-page drop has low quantization error (<5%) and qualifies for medium (never high).
    if (sample_count >= 1 and total_weight >= 2.5
            and max_single_drop >= COARSE_STEP_MEDIUM_DROP_PCT
            and max_single_pages >= COARSE_STEP_MEDIUM_MIN_PAGES):
        return "medium"
    return "low"


def _blend_yield(old_yield: int, old_weight: float, estimated_yield: int, sample_weight: float) -> int:
    if old_yield is None or old_yield <= 0 or old_yield == DEFAULT_YIELD_PER_PAGE and old_weight <= 0:
        return int(estimated_yield)
    total_w = max(0.0, old_weight) + max(0.1, sample_weight)
    return int(round(((old_yield * max(0.0, old_weight)) + (estimated_yield * max(0.1, sample_weight))) / total_w))


#: (ip, color) slots for which a legacy sample without cartridge_key was used (warned once per process).
_LEGACY_FALLBACK_WARNED: set = set()


def _is_placeholder_model(model) -> bool:
    """True when a printer model is missing or a placeholder ('', None, 'Unknown')."""
    norm = _norm_text(model)
    # HP JetDirect network names are not model identities (production NPI9E8319 case).
    return norm == "unknown" or bool(re.fullmatch(r"npi[0-9a-f]{6}", norm))


def _is_placeholder_name(name, color) -> bool:
    """Exact generic names carry no part identity; never match a part-name prefix.

    SNMP/EWS fallbacks can alternate between e.g. ``Black Toner`` and the real
    HP part name. Only the current slot's color is generic (cyan is not black).
    """
    norm = _norm_text(name)
    color_norm = _norm_text(color)
    generic = {"unknown", "toner", "cartridge", "toner cartridge", color_norm}
    if color_norm in _COLOR_ORDER:
        for noun in ("toner", "cartridge"):
            generic.update((f"{color_norm} {noun}", f"{noun} {color_norm}"))
    return norm in generic


def _key_has_placeholder(cartridge_key: str) -> bool:
    """True when a built cartridge_key ('model|name|color') has a placeholder model or name part."""
    parts = (cartridge_key or "").split("|")
    model_part = parts[0] if len(parts) > 0 else ""
    name_part = parts[1] if len(parts) > 1 else ""
    color_part = parts[2] if len(parts) > 2 else ""
    return _is_placeholder_model(model_part) or _is_placeholder_name(name_part, color_part)


def _identity_from_stored(state: dict, printer_model: str, cartridge_name: str, color: str,
                          model_missing: bool, name_missing: bool) -> tuple:
    """Replaces a blank or placeholder model/name with the stored one, when the stored one is real.

    A poll that reports no model or name carries no identity information, so it must not create a new
    cartridge_key. A real new name on the same poll is kept, so a genuine name change is still a swap.
    """
    if model_missing and state.get("printer_model") and not _is_placeholder_model(state.get("printer_model")):
        printer_model = state["printer_model"]
    if name_missing and state.get("cartridge_name") and not _is_placeholder_name(
            state.get("cartridge_name"), state.get("color") or color):
        cartridge_name = state["cartridge_name"]
    return printer_model, cartridge_name


def _is_cartridge_swap(state: Optional[dict], cartridge_key: str) -> bool:
    """True when the slot's stored cartridge_key differs from the cartridge now reported in the same slot.

    Not a swap when:
    - the stored row has no cartridge_key (legacy row: keep today's behaviour, no reset), or
    - the incoming key has a placeholder model or name (blank/'Unknown' metadata, not a new cartridge), or
    - the stored identity is a placeholder (model "Unknown", or the name equal to the colour fallback),
      which means the metadata is being completed, not that the cartridge changed.
    """
    old_key = (state or {}).get("cartridge_key")
    if not old_key or old_key == cartridge_key:
        return False
    if _key_has_placeholder(cartridge_key):
        return False
    if _is_placeholder_model(state.get("printer_model")) or _is_placeholder_name(
            state.get("cartridge_name"), state.get("color")):
        return False
    return True


def _swap_reset_fields(level_i: Optional[int], usage_counter: Optional[int], timestamp: str) -> dict:
    """Learned-state fields for a new cartridge in an existing slot.

    Yield falls back to DEFAULT; the caller's catalog/device logic then applies the reference capacity.
    last_refill_at is deliberately not included: it is slot history, not cartridge history.
    """
    has_level = level_i is not None
    return {
        "yield_per_page": DEFAULT_YIELD_PER_PAGE,
        "yield_source": "default",
        "confidence": "low",
        "sample_count": 0,
        "total_weight": 0.0,
        "learned_at": None,
        "current_level": level_i,
        "last_valid_level": level_i,
        "last_counter": usage_counter,
        "anchor_counter": usage_counter,
        "anchor_level": level_i,
        "anchor_timestamp": timestamp if has_level else None,
        "cycle_start_counter": usage_counter,
        "cycle_start_level": level_i,
        "cycle_start_timestamp": timestamp if has_level else None,
        "zero_reached_counter": usage_counter if level_i == 0 else None,
        "zero_reached_timestamp": timestamp if level_i == 0 else None,
        "pages_after_zero": 0,
        "cycle_status": "active",
        "pending_refill_level": None,
        "pending_refill_counter": None,
        "pending_refill_timestamp": None,
    }


def _interpolated_weighted_median(pts: list) -> float:
    """Exact midpoint-interpolated weighted median for sorted (value, weight) pairs.

    Fixes systematic downward bias on even-sized/symmetric sample sets where naive
    cumulative threshold `acc >= total_w / 2` returned the lower element (e.g. 8000
    for [(8000, 2.0), (12000, 2.0)] instead of 10000).
    """
    clean = [(float(y), max(1e-6, float(w))) for y, w in pts if y is not None and float(y) > 0]
    if not clean:
        return 0.0
    clean.sort(key=lambda p: p[0])
    if len(clean) == 1:
        return clean[0][0]
    total_w = sum(w for _, w in clean)
    half_w = total_w / 2.0
    centers = []
    running = 0.0
    for y, w in clean:
        centers.append((running + 0.5 * w, y))
        running += w
    if half_w <= centers[0][0]:
        return centers[0][1]
    if half_w >= centers[-1][0]:
        return centers[-1][1]
    for idx in range(len(centers) - 1):
        c0, y0 = centers[idx]
        c1, y1 = centers[idx + 1]
        if c0 <= half_w <= c1:
            if c1 - c0 <= 1e-9:
                return 0.5 * (y0 + y1)
            frac = (half_w - c0) / (c1 - c0)
            return y0 + frac * (y1 - y0)
    return centers[-1][1]


def _robust_learned_yield(conn, ip: str, color: str, cartridge_key: str,
                          rated: Optional[int] = None, limit: int = 12):
    """Two-stage robust estimator: Midpoint Weighted Median + MAD/Huber Telescoping M-Estimator.

    Stage 1 computes the outlier-resistant midpoint weighted median `M` and MAD scale `sigma`
    (50% breakdown point against blips, fake cycles, or missed swaps).
    Stage 2 trims hard outliers (> 3 * sigma) and combines inliers weighted by `toner_drop * huber_w`,
    which algebraically cancels quantization rounding boundary jitter across consecutive steps:
    (sum_inliers delta_L_i * y_i) / (sum_inliers delta_L_i) == (sum_inliers delta_p_i * 100) / (sum_inliers delta_L_i).

    Output: (yield or None, total weight of inlier samples)
    """
    try:
        # Samples of another cartridge that used the same slot must not feed this estimator (R2).
        # Legacy rows written before cartridge_key existed have NULL/'' keys; they are still matched by slot.
        rows = conn.execute(
            # Phase E.4.1: "accepted=1" on PG (BOOLEAN column) gives a type error ⇒ bool parameter.
            """SELECT estimated_yield, confidence_weight, pages_delta, toner_drop,
                      CASE WHEN cartridge_key IS NULL OR cartridge_key = '' THEN 1 ELSE 0 END
               FROM yield_samples
               WHERE accepted = ? AND estimated_yield IS NOT NULL
                 AND (cartridge_key = ?
                      OR ((cartridge_key IS NULL OR cartridge_key = '') AND printer_ip = ? AND color = ?))
               ORDER BY id DESC LIMIT ?""",
            (True, cartridge_key, ip, color, int(limit)),
        ).fetchall()
    except Exception as exc:
        log.debug("robust yield query failed: %s", exc)
        return None, 0.0
    if any(r[4] for r in rows) and (ip, color) not in _LEGACY_FALLBACK_WARNED:
        _LEGACY_FALLBACK_WARNED.add((ip, color))
        log.warning("yield %s/%s: using legacy samples without cartridge_key for this slot (matched by slot)",
                    ip, color)

    records = []
    for r in rows:
        if not r[0]:
            continue
        y_int = int(r[0])
        w_conf = max(0.1, float(r[1] or 0.1))
        p_delta = float(r[2]) if r[2] is not None and float(r[2]) > 0 else None
        t_drop = float(r[3]) if r[3] is not None and float(r[3]) > 0 else None
        if rated:
            lo, hi = rated * 0.25, rated * 2.5
            if not (lo <= y_int <= hi):
                continue
        # Preserve exact ratio when y_int was rounded from pages_delta * 100 / toner_drop,
        # while respecting synthetic estimated_yield values in unit tests.
        y_exact = float(y_int)
        if p_delta is not None and t_drop is not None:
            raw_ratio = (p_delta * 100.0) / t_drop
            if abs(raw_ratio - y_int) <= 1.0:
                y_exact = raw_ratio
        records.append((y_exact, w_conf, t_drop))

    if not records:
        return None, 0.0
    total_w = sum(w for _, w, _ in records)
    if len(records) == 1:
        return int(round(records[0][0])), total_w

    # Stage 1: Midpoint weighted median & weighted MAD scale
    med = _interpolated_weighted_median([(y, w) for y, w, _ in records])
    abs_devs = [(abs(y - med), w) for y, w, _ in records]
    mad = _interpolated_weighted_median(abs_devs)
    sigma = max(1.4826 * mad, 0.08 * med, 1.0)

    # Stage 2: Hard outlier rejection (> 3.0 * sigma) + Huber telescoping drop-weighted fusion
    num = 0.0
    den = 0.0
    inlier_w = 0.0
    for y_exact, w_conf, t_drop in records:
        dev = abs(y_exact - med)
        if dev > 3.0 * sigma:
            continue
        huber = 1.0 if dev <= 1.5 * sigma else (1.5 * sigma) / dev
        # Weighting by real toner_drop cancels quantization boundary noise across consecutive segments
        tele_w = (max(0.5, t_drop) if t_drop is not None else w_conf) * huber
        num += y_exact * tele_w
        den += tele_w
        inlier_w += w_conf

    if den <= 0:
        return int(round(med)), total_w
    return int(round(num / den)), (inlier_w if inlier_w > 0 else total_w)


def _detect_regime_shift(conn, ip: str, color: str, cartridge_key: str,
                         rated_capacity: Optional[int], limit: int = 12) -> Optional[Tuple[int, float]]:
    """Detects a verified physical capacity regime shift (e.g. refilled/compatible/XL cartridge in OEM slot).

    Prevents both permanent blend-freeze (M8) and premature jump on short noisy runs (ToshibaBug3):
    accepts an out-of-band capacity when either:
      1. >= REGIME_SHIFT_MIN_SAMPLES consistent samples covering >= REGIME_SHIFT_MIN_DROP_PCT
         cumulative toner drop agree with low variance (CV <= CONFIDENCE_CV_MEDIUM), OR
      2. technician-attested full cycle(s) (`cycle_manual_empty` within CYCLE_RATED_BAND) or >= 2
         completed full `cycle_learn` cycles (>= 140% cumulative drop, CV <= CONFIDENCE_CV_MEDIUM) exist.
    """
    if not rated_capacity or rated_capacity <= 0:
        return None
    min_needed = max(REGIME_SHIFT_MIN_SAMPLES, _min_samples_for_high(color))
    try:
        rows = conn.execute(
            """SELECT estimated_yield, toner_drop, source
               FROM yield_samples
               WHERE accepted = ? AND estimated_yield IS NOT NULL
                 AND (cartridge_key = ?
                      OR ((cartridge_key IS NULL OR cartridge_key = '') AND printer_ip = ? AND color = ?))
               ORDER BY id DESC LIMIT ?""",
            (True, cartridge_key, ip, color, int(limit)),
        ).fetchall()
    except Exception:
        return None
    valid_rows = [
        (int(r[0]), max(0.0, float(r[1] or 0.0)), str(r[2] or ""))
        for r in rows
        if r[0] and MIN_ESTIMATED_YIELD <= int(r[0]) <= MAX_ESTIMATED_YIELD
    ]
    if not valid_rows:
        return None

    band_lo = CYCLE_RATED_BAND[0] * rated_capacity
    band_hi = CYCLE_RATED_BAND[1] * rated_capacity
    manual_empty_rows = [
        (y, d) for y, d, src in valid_rows
        if src == "cycle_manual_empty" and d >= 50.0 and band_lo <= y <= band_hi
    ]
    if manual_empty_rows:
        if len(manual_empty_rows) == 1:
            return manual_empty_rows[0][0], max(4.0, manual_empty_rows[0][1] * 0.1)
        cv_m = _sample_cv([y for y, _ in manual_empty_rows])
        if cv_m is not None and cv_m <= CONFIDENCE_CV_MEDIUM:
            return _robust_learned_yield(conn, ip, color, cartridge_key, rated=None, limit=limit)

    full_cycles = [
        (y, d) for y, d, src in valid_rows
        if src in ("cycle_learn", "cycle_manual_empty") and d >= 70.0 and band_lo <= y <= band_hi
    ]
    if len(full_cycles) >= 2 and sum(d for _, d in full_cycles) >= 140.0:
        cv_c = _sample_cv([y for y, _ in full_cycles])
        if cv_c is not None and cv_c <= CONFIDENCE_CV_MEDIUM:
            return _robust_learned_yield(conn, ip, color, cartridge_key, rated=None, limit=limit)

    if len(valid_rows) < min_needed:
        return None
    cum_drop = sum(d for _, d, _ in valid_rows)
    if cum_drop < REGIME_SHIFT_MIN_DROP_PCT:
        return None
    cv = _sample_cv([y for y, _, _ in valid_rows])
    if cv is None or cv > CONFIDENCE_CV_MEDIUM:
        return None
    return _robust_learned_yield(conn, ip, color, cartridge_key, rated=None, limit=limit)


#: columns of ``cartridge_yield_profiles`` written by ``_upsert_profile_if_high``
#: (all 10 columns of the table). ``confidence`` has the constant ``'high'`` in ``VALUES`` and is not a placeholder;
#: that is why ``values_sql`` is passed explicitly.
_PROFILE_UPSERT_COLUMNS = (
    "cartridge_key", "printer_model", "cartridge_name", "color", "yield_per_page",
    "confidence", "sample_count", "total_weight", "source_printer_ip", "updated_at",
)
_PROFILE_UPSERT_VALUES = "?, ?, ?, ?, ?, 'high', ?, ?, ?, ?"


def _recent_accepted_estimates(conn, ip: str, color: str, cartridge_key: str, limit: int = 12) -> list:
    """Estimated yields of the most recent accepted samples of this slot (same selection as _robust_learned_yield)."""
    try:
        rows = conn.execute(
            """SELECT estimated_yield FROM yield_samples
               WHERE accepted = ? AND estimated_yield IS NOT NULL
                 AND (cartridge_key = ?
                      OR ((cartridge_key IS NULL OR cartridge_key = '') AND printer_ip = ? AND color = ?))
               ORDER BY id DESC LIMIT ?""",
            (True, cartridge_key, ip, color, int(limit)),
        ).fetchall()
    except Exception as exc:
        log.debug("recent estimates query failed: %s", exc)
        return []
    return [int(r[0]) for r in rows if r[0]]


def _upsert_profile_sql(conn) -> str:
    """Profile table upsert statement, using the backend of **the same connection** (not the environment)."""
    return _upsert_sql(
        "cartridge_yield_profiles", _PROFILE_UPSERT_COLUMNS, ("cartridge_key",),
        values_sql=_PROFILE_UPSERT_VALUES, backend=conn.backend)


def _upsert_profile_if_high(conn, state: dict) -> None:
    if state.get("confidence") != "high":
        return
    if int(state.get("sample_count") or 0) < _min_samples_for_high(state.get("color")):
        return
    ypp = _to_int(state.get("yield_per_page"))
    if not ypp or ypp <= 0 or ypp == DEFAULT_YIELD_PER_PAGE:
        return

    cartridge_key = state.get("cartridge_key")
    existing = _load_profile(conn, cartridge_key)
    if existing:
        old_weight = float(existing.get("total_weight") or 0.0)
        new_weight = float(state.get("total_weight") or 0.0)
        old_samples = int(existing.get("sample_count") or 0)
        new_samples = int(state.get("sample_count") or 0)
        if existing.get("source_printer_ip") == state.get("printer_ip"):
            # The same device has already built a profile; we replace the profile with the new state
            # so that sample_count/weight is not summed again on every poll.
            total_weight = max(old_weight, new_weight)
            sample_count = max(old_samples, new_samples)
        elif old_weight > 0 and new_weight > 0:
            # Another device with the same model and cartridge name has also reached high; take a weighted average.
            ypp = int(round(((existing["yield_per_page"] * old_weight) + (ypp * new_weight)) / (old_weight + new_weight)))
            total_weight = old_weight + new_weight
            sample_count = old_samples + new_samples
        else:
            total_weight = max(old_weight, new_weight)
            sample_count = max(old_samples, new_samples)
    else:
        total_weight = float(state.get("total_weight") or 0.0)
        sample_count = int(state.get("sample_count") or 0)

    conn.execute(
        # Phase E.5: cross-backend helper; "'high'" intentionally stays constant in VALUES.
        _upsert_profile_sql(conn),
        (
            cartridge_key, state.get("printer_model"), state.get("cartridge_name"),
            state.get("color"), ypp, sample_count, total_weight,
            state.get("printer_ip"), _now(),
        ),
    )

    # Propagate to peer printers sharing this cartridge_key; never overwrite the source printer
    # or peers that already reached high confidence via auto_learn or cycle_learn.
    conn.execute(
        """
        UPDATE cartridge_state
        SET yield_per_page = ?, yield_source = 'shared_profile', confidence = 'high', updated_at = ?
        WHERE cartridge_key = ?
          AND printer_ip != ?
          AND NOT (yield_source IN ('auto_learn', 'cycle_learn') AND confidence = 'high')
        """,
        (ypp, _now(), cartridge_key, state.get("printer_ip") or ""),
    )


def _resolve_interval_prior(
    state: dict,
    profile: Optional[dict] = None,
    device_capacity_pages: Optional[int] = None,
) -> Tuple[Optional[int], str, Optional[int]]:
    """Resolve (prior_yield, prior_source, nominal_capacity) for Empirical Bayes interval estimation."""
    model = state.get("printer_model") or ""
    cart_name = state.get("cartridge_name") or ""
    color = state.get("color") or "black"
    cat = _catalog_lookup(model, cart_name, color)
    cat_cap = _valid_device_capacity((cat or {}).get("yield_per_page"))
    # A stored *catalog* reference is independent of this slot's consumption
    # posterior, unlike auto_learn/cycle_learn or a self-published fleet profile.
    # Preserve it when an incomplete model identity no longer matches the file.
    if cat_cap is None and state.get("yield_source") == "catalog":
        cat_cap = _valid_device_capacity(state.get("yield_per_page"))
    dev_cap = _valid_device_capacity(device_capacity_pages)
    if dev_cap is None and state.get("yield_source") == "device_capacity":
        dev_cap = _valid_device_capacity(state.get("yield_per_page"))
    nom_cap = dev_cap or cat_cap

    if (profile and profile.get("source_printer_ip") != state.get("printer_ip")
            and profile.get("confidence") == "high" and _profile_is_trusted(profile, color)):
        prof_y = _valid_device_capacity(profile.get("yield_per_page"))
        if prof_y:
            return prof_y, "shared_profile", nom_cap
    if dev_cap:
        return dev_cap, "device_capacity", nom_cap
    if cat_cap:
        return cat_cap, "catalog", nom_cap
    fb = estimate_yield_fallback(model, color, default_pages=-1)
    if fb and fb > 0:
        return int(fb), "catalog", None
    # Neither the slot's last posterior nor its own published profile is an
    # independent prior. Replaying identical evidence must not tighten its CI.
    return DEFAULT_YIELD_PER_PAGE, "default", None


def _load_interval_snapshots(conn, *, ip: Optional[str] = None,
                            color: Optional[str] = None, limit: int = 400) -> list:
    """Fetch endpoints of recent telemetry runs, not the last N polling rows.

    Window functions run in the DB (SQLite and PostgreSQL); at most 2*limit rows
    per slot cross the connection. Retain source/validity/identity changes and
    counter rollbacks as boundaries. Repeated constant-percent polls therefore
    cannot evict all the informative consumption transitions from the window.
    This does not expand or repair the underlying stored history.
    """
    clauses, params = [], []
    if ip:
        clauses.append("printer_ip = ?")
        params.append(ip)
    if color:
        clauses.append("LOWER(color) = ?")
        params.append(color.lower())
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    # Normalize only for null-safe SQL comparison, not for telemetry admission.
    fields = ("toner_level", "raw_level", "source", "valid", "reject_reason",
              "cartridge_key", "full_color")
    # Full-color counts may increase within a plateau. Only their presence is a
    # provenance boundary (dedicated color counter vs total-for-color fallback).
    exprs = [f"COALESCE(CAST({f} AS TEXT), '')" for f in fields[:-1]]
    exprs.append("CASE WHEN full_color IS NULL THEN 0 ELSE 1 END")
    changes = " OR ".join(f"{e} <> LAG({e}) OVER w" for e in exprs)
    if conn.backend == BACKEND_POSTGRESQL:
        # Compare one typed tuple rather than evaluating six text casts and six
        # separate lag buffers per historical poll. Null text matches the legacy
        # COALESCE contract; numeric fields retain their database types.
        signature = ("ROW(toner_level, raw_level, COALESCE(source, ''), valid, "
                     "COALESCE(reject_reason, ''), COALESCE(cartridge_key, ''), "
                     "full_color IS NULL)")
        changes = f"{signature} IS DISTINCT FROM LAG({signature}) OVER w"
    params.append(max(1, int(limit)))
    return conn.execute(f"""
        WITH marked AS (
            SELECT id, printer_ip, LOWER(color) AS slot_color,
                   LAG(id) OVER w AS previous_id, LEAD(id) OVER w AS next_id,
                   CASE WHEN LAG(id) OVER w IS NULL
                OR {changes}
                OR usage_counter < LAG(usage_counter) OVER w
                THEN 1 ELSE 0 END AS changed
            FROM toner_snapshots_v2 {where}
            WINDOW w AS (PARTITION BY printer_ip, LOWER(color) ORDER BY id)
        ), edges AS (
            SELECT * FROM marked WHERE changed = 1 OR next_id IS NULL
        ), recent AS (
            SELECT id AS first_id,
                   COALESCE(LEAD(CASE WHEN changed = 1 THEN previous_id ELSE id END)
                       OVER (PARTITION BY printer_ip, slot_color ORDER BY id), id) AS last_id,
                   changed,
                   SUM(changed) OVER (
                       PARTITION BY printer_ip, slot_color ORDER BY id DESC
                       ROWS UNBOUNDED PRECEDING) AS rn
            FROM edges
        )
        SELECT s.printer_ip, s.color, s.usage_counter, s.toner_level, s.raw_level,
               s.source, s.valid, s.reject_reason, s.timestamp, s.cartridge_key,
               s.full_color, s.print_total
        FROM toner_snapshots_v2 s JOIN recent r
          ON s.id = r.first_id OR s.id = r.last_id
        WHERE r.changed = 1 AND r.rn <= ? ORDER BY s.id
    """, params).fetchall()


def estimate_cartridge_interval(
    conn,
    *,
    ip: str,
    color: str,
    state: Optional[dict] = None,
    profile: Optional[dict] = None,
    device_capacity_pages: Optional[int] = None,
    counter_basis: str = "",
    snap_rows: Optional[list] = None,
    limit: int = 400,
) -> PosteriorYieldDecision:
    """Compute interval-censored Empirical Bayes yield & CI95 for (ip, color) from `toner_snapshots_v2`."""
    state = dict(state or {})
    state["printer_ip"] = ip  # caller identity, even if a partial state was supplied
    model_txt = state.get("printer_model") or ""
    color_norm = (color or state.get("color") or "black").lower()
    prior_y, prior_src, nom_cap = _resolve_interval_prior(
        state, profile=profile, device_capacity_pages=device_capacity_pages
    )

    if snap_rows is None:
        try:
            fetched = _load_interval_snapshots(conn, ip=ip, color=color_norm, limit=limit)
            snap_rows = [r[2:] for r in fetched]
        except Exception as exc:
            log.debug("estimate_cartridge_interval snapshot query failed for %s/%s: %s", ip, color_norm, exc)
            snap_rows = []

    readings = []
    inferred_basis = counter_basis
    for r in snap_rows:
        u_cnt = r[0]
        if u_cnt is None:
            continue
        rej = str(r[5] or "")
        if not snapshot_is_usable(r[4], rej):
            continue
        raw_lv = r[2] if r[2] is not None else r[1]
        src_txt = str(r[3] or "poll").lower()
        lv_kind = "http_scrape" if ("http" in src_txt or "web" in src_txt) else "percent"
        ckey = str(r[7] or "")
        chip = ckey if (ckey and not _key_has_placeholder(ckey)) else None
        if not inferred_basis and color_norm in ("cyan", "magenta", "yellow"):
            if r[8] is None and r[9] is not None:
                inferred_basis = "total_for_color"
        readings.append(
            SupplyReading(
                usage_counter=u_cnt,
                raw_level=raw_lv,
                level_kind=lv_kind,
                provenance="http_scrape" if lv_kind == "http_scrape" else "rfc3805",
                observed_at=str(r[6] or ""),
                vendor=model_txt,
                color=color_norm,
                chip_id=chip,
            )
        )

    cur_lvl = state.get("current_level")
    return estimate_supply_series(
        readings,
        vendor_or_model=model_txt,
        prior_yield=prior_y,
        prior_source=prior_src,
        nominal_capacity=nom_cap,
        current_level=float(cur_lvl) if cur_lvl is not None else None,
        counter_basis=inferred_basis,
    )


def _apply_v2_state_decision(
    conn,
    state: dict,
    *,
    profile: Optional[dict] = None,
    device_capacity_pages: Optional[int] = None,
    counter_basis: str = "",
    timestamp: Optional[str] = None,
) -> PosteriorYieldDecision:
    """Apply Layer 4/5 v2 estimator decision to `state` when `YIELD_ESTIMATOR=v2` is active."""
    ip = state.get("printer_ip") or ""
    color = state.get("color") or "black"
    dec = estimate_cartridge_interval(
        conn,
        ip=ip,
        color=color,
        state=state,
        profile=profile,
        device_capacity_pages=device_capacity_pages,
        counter_basis=counter_basis,
    )
    if state.get("force_estimate") or state.get("yield_source") == "forced_estimate":
        return dec

    if dec.learning_state in ("learning", "converged"):
        old_src = state.get("yield_source") or "default"
        # Do not overwrite a high-confidence shared fleet profile until this slot is medium/high
        if old_src == "shared_profile" and dec.confidence == "low":
            return dec
        state["yield_per_page"] = dec.capacity_pages
        state["confidence"] = dec.confidence
        state["yield_source"] = "auto_learn"
        state["sample_count"] = max(int(state.get("sample_count") or 0), dec.n_plateaus)
        state["total_weight"] = max(float(state.get("total_weight") or 0.0), round(dec.drop_pct * 0.5, 2))
        if timestamp:
            state["learned_at"] = timestamp
    elif dec.learning_state == "suspect":
        state["confidence"] = "low"
        if state.get("yield_source") == "auto_learn":
            state["yield_per_page"] = dec.capacity_pages
    return dec


def _interval_operational_fields(state: dict, dec: PosteriorYieldDecision,
                                 *, v2_active: Optional[bool] = None) -> dict:
    """Expose which estimate drives ETA; never label a new CI with old confidence."""
    enabled = _is_v2_estimator_enabled() if v2_active is None else v2_active
    forced = bool(state.get("force_estimate") or state.get("yield_source") == "forced_estimate")
    source_map = {"prior_catalog": "catalog", "prior_device": "device_capacity",
                  "prior_fleet": "shared_profile", "prior_default": "default"}
    fields = {
        "stored_yield_per_page": state.get("yield_per_page"),
        "stored_yield_source": state.get("yield_source"),
        "stored_confidence": state.get("confidence"),
        "interval_confidence": dec.confidence,
        "estimator": "forced_estimate" if forced else ("v2" if enabled else "legacy"),
    }
    cap = int(state.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
    if enabled and not forced:
        cap = dec.capacity_pages
        fields.update(yield_per_page=cap, confidence=dec.confidence,
                      yield_source=source_map.get(dec.capacity_method, "auto_learn"))
    if (not enabled or forced) and dec.nominal_capacity_pages and cap > 0:
        fields["coverage_index"] = round(max(0.15, min(6.0, dec.nominal_capacity_pages / cap)), 3)
    level = _valid_level(state.get("current_level"))
    fields["eta_pages"] = int(round(level / 100.0 * cap)) if level is not None else None
    return fields


def _state_public_dict(
    state: dict,
    profile: Optional[dict] = None,
    interval_decision: Optional[PosteriorYieldDecision] = None,
    extra_meta: Optional[dict] = None,
) -> dict:
    cyc_status = state.get("cycle_status") or "active"
    pages_after_z = _to_int(state.get("pages_after_zero"), 0)
    sensor_state = "healthy"
    if cyc_status in ("overrun_alert", "chip_quarantined", "frozen_chip"):
        sensor_state = cyc_status
    result = {
        "current_level": _valid_level(state.get("current_level")),
        "yield_per_page": _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE),
        "yield_source": state.get("yield_source") or "default",
        "confidence": state.get("confidence") or "low",
        "sample_count": _to_int(state.get("sample_count"), 0),
        "total_weight": float(state.get("total_weight") or 0.0),
        "anchor_level": state.get("anchor_level"),
        "anchor_counter": state.get("anchor_counter"),
        "last_refill_at": state.get("last_refill_at"),
        "cycle_start_counter": state.get("cycle_start_counter"),
        "cycle_start_level": state.get("cycle_start_level"),
        "zero_reached_counter": state.get("zero_reached_counter"),
        "pages_after_zero": pages_after_z,
        "cycle_status": cyc_status,
        "sensor_state": sensor_state,
        "chip_quarantined": cyc_status in ("chip_quarantined", "overrun_alert"),
        "overrun_pages": pages_after_z if cyc_status in ("overrun_alert", "chip_quarantined", "frozen_chip") else 0,
        "unregistered_refill_suspected": cyc_status == "overrun_alert",
        "cartridge_key": state.get("cartridge_key"),
    }
    if extra_meta:
        result.update(extra_meta)
    if profile:
        result["shared_profile"] = {
            "yield_per_page": profile.get("yield_per_page"),
            "confidence": profile.get("confidence"),
            "sample_count": profile.get("sample_count"),
            "source_printer_ip": profile.get("source_printer_ip"),
        }
    if interval_decision is not None:
        result.update({
            "capacity_ci95": [interval_decision.capacity_ci95[0], interval_decision.capacity_ci95[1]],
            "rel_ci95": interval_decision.rel_ci95,
            "capacity_method": interval_decision.capacity_method,
            "learning_state": interval_decision.learning_state,
            "nominal_capacity_pages": interval_decision.nominal_capacity_pages,
            "coverage_index": interval_decision.coverage_index,
            "eta_pages": interval_decision.eta_pages,
            "interval_capacity_pages": interval_decision.capacity_pages,
            "n_plateaus": interval_decision.n_plateaus,
        })
        result.update(_interval_operational_fields(state, interval_decision))
    return result


def _maybe_close_cycle(conn, state: dict, *, ip: str, color: str, printer_model: str,
                       cartridge_name: str, cartridge_key: str, usage_counter: int,
                       timestamp: str, counter_basis: str, source: str,
                       rated_capacity: Optional[int] = None,
                       end_level_before_refill: Optional[int] = None,
                       weight_scale: float = 1.0,
                       accepted_source: str = "cycle_learn") -> Optional[dict]:
    """Closes a completed or substantial partial consumption cycle and records a cycle_learn sample.

    Handles two physical scenarios cleanly (fixing M4):
      1. Zero-reached / depleted cycle (`zero_reached_counter` set or `end_level_before_refill <= 0` or None):
         all pages printed from `cycle_start_counter` through depletion (including `pages_after_zero` reserve)
         are normalized by `min(start_level, 100)`.
      2. Early pre-zero refill (`end_level_before_refill > 0` and `zero_reached_counter is None`):
         requires at least `MIN_PARTIAL_CYCLE_DROP_PCT` (20%) real consumption and normalizes by
         `(start_level - end_level_before_refill)` instead of `start_level`, preventing severe underestimation.
    """
    start_counter = _to_int(state.get("cycle_start_counter"))
    start_level = _to_int(state.get("cycle_start_level"), 100)
    if start_counter is None or usage_counter is None or usage_counter <= start_counter or start_level <= 0:
        return None

    zero_reached = state.get("zero_reached_counter") is not None
    capped_start = min(start_level, 100)
    if not zero_reached and end_level_before_refill is not None and end_level_before_refill > 0:
        toner_consumed = capped_start - min(int(end_level_before_refill), 100)
        if toner_consumed < MIN_PARTIAL_CYCLE_DROP_PCT:
            return None
        recorded_end_level = int(end_level_before_refill)
    else:
        toner_consumed = capped_start
        if end_level_before_refill is not None and toner_consumed < MIN_ZERO_CYCLE_DROP_PCT:
            return None
        recorded_end_level = 0 if (zero_reached or end_level_before_refill == 0) else 100

    pages_delta = usage_counter - start_counter
    if pages_delta < MIN_PAGES_FOR_SAMPLE:
        return None
    estimated_yield = int(round(pages_delta * 100.0 / toner_consumed))
    if estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
        _record_sample(
            conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
            cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
            start_level=start_level, end_level=recorded_end_level, pages_delta=pages_delta,
            toner_drop=toner_consumed, estimated_yield=estimated_yield, counter_basis=counter_basis,
            weight=0, accepted=False, reject_reason="cycle_yield_out_of_range", source=source,
        )
        return None
    # ✅ Compatibility band with the reference rating (real case: a fake 320-page cycle on
    # T-4590 with a rating of ~36,600 that was formed from a blip): a cycle that is several
    # times smaller or larger than the reference cannot be a real cartridge.
    if rated_capacity and not (CYCLE_RATED_BAND[0] * rated_capacity
                               <= estimated_yield <= CYCLE_RATED_BAND[1] * rated_capacity):
        log.info("  [%s/%s] cycle yield %s بیرون از باند مرجع %s — رد شد", ip, color,
                 estimated_yield, rated_capacity)
        _record_sample(
            conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
            cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
            start_level=start_level, end_level=recorded_end_level, pages_delta=pages_delta,
            toner_drop=toner_consumed, estimated_yield=estimated_yield, counter_basis=counter_basis,
            weight=0, accepted=False, reject_reason="cycle_vs_rated_out_of_band", source=source,
        )
        return None
    cycle_fraction = min(1.0, toner_consumed / 100.0)
    min_cycle_w = 4.0 if toner_consumed >= 50 or zero_reached or end_level_before_refill is None else 2.5
    raw_weight = max(min_cycle_w, min(12.0, (pages_delta / 100.0) * cycle_fraction if cycle_fraction < 1.0 else pages_delta / 100.0))
    weight = round(max(1.0, raw_weight * max(0.2, float(weight_scale))), 3)
    _record_sample(
        conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
        cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
        start_level=start_level, end_level=recorded_end_level, pages_delta=pages_delta,
        toner_drop=toner_consumed, estimated_yield=estimated_yield, counter_basis=counter_basis,
        weight=weight, accepted=True, reject_reason=None, source=accepted_source or "cycle_learn",
    )

    # Combine multiple closed cycles robustly when prior cycle_learn samples exist for this cartridge_key
    final_cycle_yield = estimated_yield
    try:
        cyc_rows = conn.execute(
            """SELECT estimated_yield, confidence_weight FROM yield_samples
               WHERE accepted = ?
                 AND source IN ('cycle_learn', 'cycle_manual_empty', 'cycle_dirty_partial')
                 AND estimated_yield IS NOT NULL
                 AND cartridge_key = ?
               ORDER BY id DESC LIMIT 8""",
            (True, cartridge_key),
        ).fetchall()
        if len(cyc_rows) >= 2:
            pts = [(int(r[0]), max(0.1, float(r[1] or 1.0))) for r in cyc_rows if r[0]]
            if rated_capacity:
                pts = [p for p in pts if CYCLE_RATED_BAND[0] * rated_capacity <= p[0] <= CYCLE_RATED_BAND[1] * rated_capacity]
            if pts:
                final_cycle_yield = int(round(_interpolated_weighted_median(pts)))
    except Exception:
        pass

    new_sample_count = int(state.get("sample_count") or 0) + 1
    new_total_weight = float(state.get("total_weight") or 0.0) + weight
    conf = _derive_confidence(
        new_sample_count, new_total_weight, counter_basis,
        max_single_drop=toner_consumed, max_single_pages=pages_delta,
    )
    conf = _cap_confidence_by_variance(conf, _recent_accepted_estimates(conn, ip, color, cartridge_key))
    return {
        "yield_per_page": final_cycle_yield,
        "yield_source": "cycle_learn",
        "confidence": conf,
        "sample_count": new_sample_count,
        "total_weight": new_total_weight,
        "learned_at": timestamp,
        "cycle_status": "closed",
    }


# Reject reason for a poll whose level is a display value, not a device reading (B-3).
_REJECT_DISPLAY_ONLY = "status_coded_level"


def process_cartridge_snapshot(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                               level, raw_level=None, counters: dict, device_type: str,
                               timestamp: str = None, source: str = "poll",
                               device_capacity_pages: int = None,
                               level_is_synthetic: bool = False,
                               exact_level: Optional[float] = None,
                               hardware_swap: bool = False,
                               chip_quarantined: bool = False,
                               virtual_display_level: Optional[int] = None,
                               _ensure_tables: bool = True) -> dict:
    """Processes one cartridge and returns metadata that can be displayed in the API/UI.

    level_is_synthetic=True means `level` is a display value, not a device reading (B-3, e.g. a Canon status
    code shown as 15/70%). It is stored as current_level for the UI, but it never sets an anchor, never moves
    last_valid_level or last_counter, and never learns a sample.
    exact_level carries optional sub-percent precision (0.0..100.0) when SNMP max capacity > 100.
    hardware_swap=True indicates chip_id/serial or supply_pages proved a physical cartridge replacement
    even when model and cartridge_name are unchanged (M5).
    """
    if _ensure_tables:
        ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
    # Blank or placeholder identity is judged on the raw input, before the defaults below (R2 fix).
    model_missing = _is_placeholder_model(printer_model)
    name_missing = _is_placeholder_name(cartridge_name, color)
    printer_model = printer_model or "Unknown"
    cartridge_name = cartridge_name or color
    cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
    counters = counters or {}
    level_i = _valid_level(level)
    exact_f = None
    if exact_level is not None and not level_is_synthetic:
        try:
            ef = float(exact_level)
            if 0.0 <= ef <= 100.0:
                exact_f = ef
        except (TypeError, ValueError):
            exact_f = None
    device_capacity = _valid_device_capacity(device_capacity_pages)
    catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
    catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
    usage_counter, counter_basis, counter_quality = _compute_usage_counter(color, counters, device_type)

    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color)
        # Incomplete identity on this poll: keep the stored model/name, so the poll neither resets the learned
        # state nor stores its sample or metadata under a placeholder key (R2 fix).
        if state is not None and (model_missing or name_missing):
            printer_model, cartridge_name = _identity_from_stored(
                state, printer_model, cartridge_name, color, model_missing, name_missing)
            cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
            catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
            catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
        profile = _load_profile(conn, cartridge_key)
        now = _now()

        if state is None:
            inherited_yield = DEFAULT_YIELD_PER_PAGE
            inherited_source = "default"
            inherited_conf = "low"
            if catalog_capacity:
                inherited_yield = catalog_capacity
                inherited_source = "catalog"
                inherited_conf = (catalog_match or {}).get("confidence") or "medium"
            if device_capacity:
                inherited_yield = device_capacity
                inherited_source = "device_capacity"
                inherited_conf = "medium"
            if profile and profile.get("confidence") == "high" and _profile_is_trusted(profile, color):
                inherited_yield = int(profile.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
                inherited_source = "shared_profile"
                inherited_conf = "high"
            state = {
                "printer_ip": ip,
                "color": color,
                "printer_model": printer_model,
                "cartridge_name": cartridge_name,
                "cartridge_key": cartridge_key,
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter if level_i is not None else None,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp if level_i is not None else None,
                "yield_per_page": inherited_yield,
                "yield_source": inherited_source,
                "confidence": inherited_conf,
                "sample_count": 0,
                "total_weight": 0.0,
                "cycle_start_counter": usage_counter if level_i is not None else None,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp if level_i is not None else None,
                "zero_reached_counter": usage_counter if level_i == 0 else None,
                "zero_reached_timestamp": timestamp if level_i == 0 else None,
                "pages_after_zero": 0,
                "cycle_status": "active",
                "updated_at": now,
            }
            reject = None if level_i is not None and usage_counter is not None else "missing_level_or_counter"
            if level_is_synthetic:
                # B-3: a display value must not seed the baseline. The first real reading starts it.
                state.update({"last_valid_level": None, "anchor_counter": None, "anchor_level": None,
                              "anchor_timestamp": None, "cycle_start_counter": None, "cycle_start_level": None,
                              "cycle_start_timestamp": None, "zero_reached_counter": None,
                              "zero_reached_timestamp": None})
                reject = _REJECT_DISPLAY_ONLY
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            elif exact_f is not None and usage_counter is not None and level_i is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            _upsert_state(conn, state)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=reject is None, reject_reason=reject)
            return _state_public_dict(state, profile)

        # Reference capacity (device-declared first, then catalog) — the "principled" capacity anchor
        rated_capacity = device_capacity or catalog_capacity

        # Replacement retires the previous cycle even if the new sensor is missing
        # or only supplies a display/status estimate. That estimate must not seed it.
        if hardware_swap and level_is_synthetic:
            level_i = None

        # B-3: a display value (Canon status code, manual-override estimate) is shown but never learned from.
        if level_is_synthetic and not hardware_swap:
            state.update({"current_level": level_i, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason=_REJECT_DISPLAY_ONLY)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # Must be read before the metadata update below overwrites the stored key (R2).
        swapped = _is_cartridge_swap(state, cartridge_key)
        old_key = state.get("cartridge_key")

        # metadata is always updated, because the cartridge or model name may become more complete in later polls.
        state.update({
            "printer_model": printer_model,
            "cartridge_name": cartridge_name,
            "cartridge_key": cartridge_key,
            "updated_at": now,
        })
        if swapped:
            # A different cartridge now sits in this slot: the old cartridge's learned yield, confidence and
            # counts must not carry over. The catalog/device logic below fills in the reference capacity.
            log.info("  [%s/%s] cartridge changed (%s -> %s): learned state for this slot restarts",
                     ip, color, old_key, cartridge_key)
            state.update(_swap_reset_fields(level_i, usage_counter, timestamp))
            if exact_f is not None and usage_counter is not None and level_i is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            else:
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)

        # If we still only have the default and the device itself declares a valid capacity, we use it as a
        # better fallback. We do not override auto_learn/shared/high.
        state_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
        state_source = state.get("yield_source") or "default"
        state_confidence = state.get("confidence") or "low"
        low_conf_auto = state_source == "auto_learn" and state_confidence == "low"
        if catalog_capacity and (state_source == "default" or low_conf_auto or (state_source == "catalog" and state_yield != catalog_capacity)):
            state["yield_per_page"] = catalog_capacity
            state["yield_source"] = "catalog"
            state["confidence"] = (catalog_match or {}).get("confidence") or "medium"
            state_yield = catalog_capacity
            state_source = "catalog"
            state_confidence = state["confidence"]
        if device_capacity and (
            state_source in ("default", "catalog") or low_conf_auto or
            (state_source == "device_capacity" and state_yield != device_capacity)
        ):
            state["yield_per_page"] = device_capacity
            state["yield_source"] = "device_capacity"
            state["confidence"] = "medium"

        # ✅ Self-healing of poisoned states, while preserving verified multi-sample regime shifts (M8 fix):
        # if the learned capacity is >40% away from the reference AND NOT backed by a low-CV multi-sample
        # regime shift (such as a consistent refilled/compatible/XL cartridge), snap back to reference.
        if rated_capacity and state.get("yield_source") in ("auto_learn", "cycle_learn", "learned"):
            cur_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            if cur_yield and abs(cur_yield - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity:
                regime_check = _detect_regime_shift(conn, ip, color, cartridge_key, rated_capacity)
                if regime_check is None or abs(regime_check[0] - cur_yield) > 0.25 * max(regime_check[0], 1):
                    log.warning(
                        "  [%s/%s] ظرفیت یادگرفته‌ی ناسازگار (%s) با مرجع (%s) — snap به مرجع و reset یادگیری",
                        ip, color, cur_yield, rated_capacity)
                    state.update({
                        "yield_per_page": rated_capacity,
                        "yield_source": "device_capacity" if device_capacity else "catalog",
                        "confidence": (catalog_match or {}).get("confidence") or "medium",
                        "sample_count": 0,
                        "total_weight": 0.0,
                    })

        # M5: Physical swap of a cartridge with the SAME model & name (detected via chip_id/serial or supply_pages)
        if hardware_swap and not swapped:
            pre_swap_lvl = _to_int(state.get("last_valid_level"))
            if usage_counter is not None and (state.get("zero_reached_counter") is not None or (
                pre_swap_lvl is not None
                and (_to_int(state.get("cycle_start_level"), 100) - pre_swap_lvl) >= MIN_PARTIAL_CYCLE_DROP_PCT
                and _to_int(state.get("cycle_start_counter")) == _to_int(state.get("anchor_counter"))
            )):
                end_lvl = 0 if state.get("zero_reached_counter") is not None else pre_swap_lvl
                cyc_upd = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=usage_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_hardware_swap",
                    rated_capacity=rated_capacity, end_level_before_refill=end_lvl,
                ) or {}
                state.update(cyc_upd)
            state.update({
                "last_refill_at": timestamp,
                "last_refill_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "cycle_start_counter": usage_counter,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp,
                "zero_reached_counter": usage_counter if level_i == 0 else None,
                "zero_reached_timestamp": timestamp if level_i == 0 else None,
                "pages_after_zero": 0,
                "cycle_status": "zero_plateau" if level_i == 0 else "active",
                "pending_refill_level": None,
                "pending_refill_counter": None,
                "pending_refill_timestamp": None,
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "updated_at": now,
            })
            if exact_f is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            else:
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=level_i is not None and usage_counter is not None,
                             reject_reason="hardware_cartridge_swap")
            _upsert_state(conn, state)
            _upsert_profile_if_high(conn, state)
            profile = _load_profile(conn, cartridge_key)
            return _state_public_dict(state, profile)

        # Chip Quarantine after manual reset (e.g. refilled/reused cartridge without a new chip, stuck at 90%):
        # keep anchor_level & cycle_start_level untouched so raw_level=90% never triggers a bogus 100%->90% sample,
        # update current_level to the dead-reckoned virtual display level, and detect overrun past 0%.
        if chip_quarantined:
            disp_lv = _valid_level(virtual_display_level)
            if disp_lv is None:
                disp_lv = _valid_level(state.get("current_level"))
            eff_ypp = _to_int(state.get("yield_per_page"), rated_capacity or DEFAULT_YIELD_PER_PAGE)
            c_start = _to_int(state.get("cycle_start_counter"))
            c_start_lv = _to_int(state.get("cycle_start_level"), 100)
            overrun_p = 0
            if usage_counter is not None and c_start is not None and usage_counter >= c_start:
                pages_in_cycle = usage_counter - c_start
                if disp_lv is None:
                    est_d = int(round(pages_in_cycle * 100.0 / max(eff_ypp, 1)))
                    disp_lv = max(0, min(100, c_start_lv - est_d))
                if disp_lv == 0:
                    exp_empty = int(round(eff_ypp * (min(max(c_start_lv, 1), 100) / 100.0)))
                    overrun_p = max(0, pages_in_cycle - exp_empty)
            overrun_thresh = max(OVERRUN_ALERT_MIN_PAGES, int(round(OVERRUN_ALERT_PCT * max(eff_ypp, 1))))
            cyc_st = "overrun_alert" if overrun_p >= overrun_thresh else "chip_quarantined"
            state.update({
                "current_level": disp_lv,
                "last_counter": usage_counter if usage_counter is not None else state.get("last_counter"),
                "pages_after_zero": overrun_p,
                "cycle_status": cyc_st,
                "updated_at": now,
            })
            _record_snapshot(
                conn, ip, color, printer_model, cartridge_name, cartridge_key,
                timestamp, counters, usage_counter, disp_lv, raw_level, source,
                valid=False, reject_reason=_REJECT_QUARANTINED_CHIP,
            )
            _upsert_state(conn, state)
            return _state_public_dict(
                state, profile,
                extra_meta={"frozen_chip_level": _valid_level(raw_level)},
            )

        if state.get("cycle_start_counter") is None and level_i is not None and usage_counter is not None:
            state.update({
                "cycle_start_counter": usage_counter,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp,
                "cycle_status": "active",
            })

        if level_i is None or usage_counter is None:
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="missing_level_or_counter")
            state["updated_at"] = now
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # If the counter goes backwards, a reset/reboot/counter change is likely; we reset the anchor and do not learn.
        last_counter = _to_int(state.get("last_counter"))
        if last_counter is not None and usage_counter < last_counter:
            _record_sample(conn, ip=ip, color=color, printer_model=printer_model,
                           cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                           start_counter=last_counter, end_counter=usage_counter,
                           start_level=state.get("last_valid_level"), end_level=level_i,
                           pages_delta=usage_counter - last_counter, toner_drop=0,
                           estimated_yield=None, counter_basis=counter_basis, weight=0,
                           accepted=False, reject_reason="counter_decreased", source=source)
            state.update({
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "pending_refill_level": None,
                "pending_refill_counter": None,
                "pending_refill_timestamp": None,
                "updated_at": now,
            })
            if exact_f is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            else:
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="counter_decreased_anchor_reset")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        prev_level = _to_int(state.get("last_valid_level"))
        anchor_level = _to_int(state.get("anchor_level"))
        anchor_counter = _to_int(state.get("anchor_counter"))

        # ─── Surface anti-noise gate (single-pixel blip) ─────────────────────────
        if (prev_level is not None and level_i is not None
                and prev_level - level_i > LEVEL_NOISE_DROP_PERCENT
                and usage_counter is not None and last_counter is not None
                and 0 <= usage_counter - last_counter < LEVEL_NOISE_MAX_PAGES):
            log.info("  [%s/%s] خواندن نویزی سطح رد شد: %s%% → %s%% با %s صفحه (بدون تغییر در anchor)",
                     ip, color, prev_level, level_i, usage_counter - last_counter)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="level_read_noise")
            state.update({"current_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # Large toner increase: refill candidate, then confirmed on the next poll.
        if prev_level is not None and level_i - prev_level >= REFILL_JUMP_PERCENT:
            pending_level = _to_int(state.get("pending_refill_level"))
            pending_counter = _to_int(state.get("pending_refill_counter"))
            if pending_level is not None and level_i >= pending_level - 1 and usage_counter >= (pending_counter or usage_counter):
                end_lvl_pre = 0 if state.get("zero_reached_counter") is not None else anchor_level
                cycle_update = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=pending_counter or usage_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_auto_refill",
                    rated_capacity=rated_capacity,
                    end_level_before_refill=end_lvl_pre,
                ) or {}
                state.update(cycle_update)
                state.update({
                    "last_refill_at": timestamp,
                    "last_refill_counter": usage_counter,
                    "anchor_counter": usage_counter,
                    "anchor_level": level_i,
                    "anchor_timestamp": timestamp,
                    "cycle_start_counter": usage_counter,
                    "cycle_start_level": level_i,
                    "cycle_start_timestamp": timestamp,
                    "zero_reached_counter": None,
                    "zero_reached_timestamp": None,
                    "pages_after_zero": 0,
                    "cycle_status": "active",
                    "pending_refill_level": None,
                    "pending_refill_counter": None,
                    "pending_refill_timestamp": None,
                })
                if exact_f is not None:
                    _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
                else:
                    _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            else:
                state.update({
                    "pending_refill_level": level_i,
                    "pending_refill_counter": usage_counter,
                    "pending_refill_timestamp": timestamp,
                    # From the UI perspective this is no longer an active zero plateau; we are only waiting for the next poll to confirm it.
                    "cycle_status": "refill_pending",
                })
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="refill_candidate_or_confirmed")
            _upsert_state(conn, state)
            _upsert_profile_if_high(conn, state)
            profile = _load_profile(conn, cartridge_key)
            return _state_public_dict(state, profile)

        # A small toner increase is usually bounce/noise; do not learn from it, but record the last value.
        if prev_level is not None and level_i > prev_level and level_i - prev_level <= SMALL_LEVEL_BOUNCE_PERCENT:
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="small_level_bounce")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        if level_i == 0:
            zero_counter = _to_int(state.get("zero_reached_counter"))
            if zero_counter is None:
                state["zero_reached_counter"] = usage_counter
                state["zero_reached_timestamp"] = timestamp
                state["pages_after_zero"] = 0
            else:
                state["pages_after_zero"] = max(0, usage_counter - zero_counter)
            state["cycle_status"] = "zero_plateau"

        if anchor_level is None or anchor_counter is None:
            state.update({
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "updated_at": now,
            })
            if exact_f is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        toner_drop = anchor_level - level_i
        pages_delta = usage_counter - anchor_counter

        # Stale anchor / single-step refill confirmation (M4 fix):
        # When a single-step refill (e.g. 0% -> 100%, or 30% -> 100%) set pending_refill_level on Poll N+1
        # and confirmed at high level on Poll N+2, close any completed zero_plateau cycle (with pages_after_zero > 0
        # or un-sampled drop) before resetting cycle_start_* and anchor_*, without double-counting an already-sampled
        # 100 -> 0 drop when pages_after_zero == 0.
        pending_level = _to_int(state.get("pending_refill_level"))
        if pending_level is not None and level_i - anchor_level >= REFILL_JUMP_PERCENT:
            refill_counter = _to_int(state.get("pending_refill_counter")) or usage_counter
            zero_reached_now = state.get("zero_reached_counter") is not None
            pages_after_z = _to_int(state.get("pages_after_zero"), 0)
            existing_samples = int(state.get("sample_count") or 0)
            should_close_zero = zero_reached_now and (pages_after_z > 0 or existing_samples == 0)
            should_close_partial = (
                not zero_reached_now
                and anchor_level > 0
                and (_to_int(state.get("cycle_start_level"), 100) - anchor_level) >= MIN_PARTIAL_CYCLE_DROP_PCT
                and (refill_counter - anchor_counter) >= MIN_PAGES_FOR_SAMPLE
            )
            if should_close_zero or should_close_partial:
                cycle_update = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=refill_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_auto_refill",
                    rated_capacity=rated_capacity,
                    end_level_before_refill=0 if should_close_zero else anchor_level,
                ) or {}
                state.update(cycle_update)

            state.update({
                "last_refill_at": timestamp,
                "last_refill_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "cycle_start_counter": usage_counter,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp,
                "zero_reached_counter": None,
                "zero_reached_timestamp": None,
                "pages_after_zero": 0,
                "cycle_status": "active",
                "pending_refill_level": None,
                "pending_refill_counter": None,
                "pending_refill_timestamp": None,
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "updated_at": now,
            })
            if exact_f is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            else:
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="stale_anchor_reset")
            _upsert_state(conn, state)
            _upsert_profile_if_high(conn, state)
            profile = _load_profile(conn, cartridge_key)
            return _state_public_dict(state, profile)

        # High-resolution sub-percent telemetry when SNMP reports raw max > 100
        subpct_drop = None
        cached_exact = _EXACT_ANCHOR_CACHE.get((ip, color))
        if exact_f is not None and cached_exact is not None:
            c_counter, c_exact_lvl, c_key = cached_exact
            if c_counter == anchor_counter and c_key == cartridge_key and c_exact_lvl - exact_f >= 0.75:
                subpct_drop = c_exact_lvl - exact_f

        # Stable level: keep the anchor so that low-usage printers also get cumulative calculations.
        if toner_drop <= 0 and subpct_drop is None:
            eff_ypp = _to_int(state.get("yield_per_page"), rated_capacity or DEFAULT_YIELD_PER_PAGE)
            ref_for_freeze = rated_capacity or eff_ypp or DEFAULT_YIELD_PER_PAGE
            freeze_thresh = _frozen_chip_page_threshold(printer_model, ref_for_freeze)
            if level_i is not None and level_i > 0 and pages_delta >= freeze_thresh:
                est_drop = int(round(pages_delta * 100.0 / max(eff_ypp, 1)))
                virt_lv = max(0, min(100, anchor_level - est_drop))
                exp_empty = int(round(eff_ypp * (min(max(anchor_level, 1), 100) / 100.0)))
                overrun_p = max(0, pages_delta - exp_empty) if virt_lv == 0 else 0
                overrun_thresh = max(OVERRUN_ALERT_MIN_PAGES, int(round(OVERRUN_ALERT_PCT * max(eff_ypp, 1))))
                cyc_st = "overrun_alert" if overrun_p >= overrun_thresh else "frozen_chip"
                state.update({
                    "current_level": virt_lv,
                    "last_valid_level": level_i,
                    "last_counter": usage_counter,
                    "pages_after_zero": overrun_p,
                    "cycle_status": cyc_st,
                    "force_estimate": 1,
                    "updated_at": now,
                })
                _record_snapshot(
                    conn, ip, color, printer_model, cartridge_name, cartridge_key,
                    timestamp, counters, usage_counter, virt_lv, raw_level, source,
                    valid=False, reject_reason=_REJECT_FROZEN_CHIP,
                )
                _upsert_state(conn, state)
                return _state_public_dict(
                    state, profile,
                    extra_meta={"frozen_chip_level": level_i},
                )

            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            v2_dec = None
            if _is_v2_estimator_enabled():
                v2_dec = _apply_v2_state_decision(
                    conn, state, profile=profile, device_capacity_pages=device_capacity,
                    counter_basis=counter_basis, timestamp=timestamp,
                )
            _upsert_state(conn, state)
            if v2_dec is not None:
                _upsert_profile_if_high(conn, state)
                profile = _load_profile(conn, cartridge_key)
            return _state_public_dict(state, profile, interval_decision=v2_dec)

        # If a previously frozen chip genuinely steps down, restore healthy active sensor tracking.
        if state.get("cycle_status") in ("frozen_chip", "overrun_alert"):
            state["cycle_status"] = "zero_plateau" if level_i == 0 else "active"
            state["force_estimate"] = 0
            if level_i > 0:
                state["pages_after_zero"] = 0

        eff_drop = subpct_drop if subpct_drop is not None else float(toner_drop)
        recorded_drop = max(1, int(round(eff_drop)))
        estimated_yield = int(round(pages_delta * 100.0 / eff_drop)) if eff_drop > 0 else None
        reject_reason = None
        accepted = True
        if pages_delta < MIN_PAGES_FOR_SAMPLE:
            accepted = False
            reject_reason = "not_enough_pages_yet"
        elif subpct_drop is not None:
            min_subpct_pages = max(30, int(round((rated_capacity or DEFAULT_YIELD_PER_PAGE) * 0.004)))
            if pages_delta < min_subpct_pages:
                accepted = False
                reject_reason = "not_enough_pages_yet"
            elif estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
                accepted = False
                reject_reason = "estimated_yield_out_of_range"
        elif toner_drop == 1 and pages_delta < _quant_min_pages_1pct(rated_capacity):
            # A quantized sample of "1% with few pages" has a large relative error (real example:
            # 11 pages → estimate of 1100 for Cartridge 737 with a rating of 2400). Wait until
            # enough good pages accumulate or the drop reaches 2%+; then the ratio is reliable.
            accepted = False
            reject_reason = "quantization_1pct_noise"
        elif estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
            accepted = False
            reject_reason = "estimated_yield_out_of_range"

        weight = _sample_weight(pages_delta, eff_drop, counter_quality) if accepted else 0.0
        _record_sample(conn, ip=ip, color=color, printer_model=printer_model,
                       cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                       start_counter=anchor_counter, end_counter=usage_counter,
                       start_level=anchor_level, end_level=level_i,
                       pages_delta=pages_delta, toner_drop=recorded_drop,
                       estimated_yield=estimated_yield, counter_basis=counter_basis,
                       weight=weight, accepted=accepted, reject_reason=reject_reason, source=source)

        if accepted:
            old_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            old_source = state.get("yield_source") or "default"
            old_confidence = state.get("confidence") or "low"
            old_weight = float(state.get("total_weight") or 0.0)
            new_weight = old_weight + weight
            sample_count = int(state.get("sample_count") or 0) + 1
            # ✅ Two-stage robust estimator (Midpoint Weighted Median + MAD/Huber Telescoping M-Estimator)
            _rb, _rb_w = _robust_learned_yield(conn, ip, color, cartridge_key, rated=rated_capacity)
            regime = None
            if rated_capacity and (_rb is None or abs(_rb - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity):
                regime = _detect_regime_shift(conn, ip, color, cartridge_key, rated_capacity)

            if regime is not None:
                new_yield = regime[0]
            elif _rb is not None:
                new_yield = _rb
            elif rated_capacity:
                # M8 fix: NEVER blend a reference capacity (catalog/device) with an out-of-band sample,
                # which previously fabricated an intermediate number (e.g. 22,800) and permanently froze state.
                new_yield = None
            else:
                new_yield = _blend_yield(old_yield, old_weight, estimated_yield, weight)

            confidence = _derive_confidence(
                sample_count, new_weight, counter_basis,
                max_single_drop=eff_drop, max_single_pages=pages_delta,
            )
            confidence = _cap_confidence_by_variance(
                confidence, _recent_accepted_estimates(conn, ip, color, cartridge_key))

            keep_existing_capacity = (
                new_yield is None or
                (old_source in ("catalog", "device_capacity") and confidence == "low") or
                (old_source == "shared_profile" and confidence != "high")
            )
            # ✅ Compatibility rule with the reference, unless a multi-sample low-CV regime shift is proven:
            if (rated_capacity and new_yield is not None and regime is None
                    and abs(new_yield - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity
                    and old_source not in ("manual_reset", "forced_estimate", "shared_profile")):
                keep_existing_capacity = True
            update_payload = {
                "sample_count": sample_count,
                "total_weight": new_weight,
                "learned_at": timestamp,
                # After a valid sample, move the anchor to the current level.
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
            }
            if exact_f is not None:
                _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
            else:
                _EXACT_ANCHOR_CACHE.pop((ip, color), None)
            if keep_existing_capacity:
                update_payload.update({
                    "yield_per_page": old_yield,
                    "yield_source": old_source,
                    "confidence": old_confidence,
                })
            else:
                update_payload.update({
                    "yield_per_page": new_yield,
                    "yield_source": "auto_learn",
                    "confidence": confidence,
                })
            state.update(update_payload)
        else:
            # For not_enough_pages_yet and quantization_1pct_noise we deliberately do not change the anchor, so that
            # the next poll accumulates pages and toner drop against the same anchor (B-2). Other rejects re-anchor.
            if reject_reason not in ("not_enough_pages_yet", "quantization_1pct_noise"):
                state.update({"anchor_counter": usage_counter, "anchor_level": level_i, "anchor_timestamp": timestamp})
                if exact_f is not None:
                    _EXACT_ANCHOR_CACHE[(ip, color)] = (usage_counter, exact_f, cartridge_key)
                else:
                    _EXACT_ANCHOR_CACHE.pop((ip, color), None)

        state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters, usage_counter, level_i, raw_level, source,
                         valid=accepted, reject_reason=reject_reason)
        v2_dec = None
        if _is_v2_estimator_enabled():
            v2_dec = _apply_v2_state_decision(
                conn, state, profile=profile, device_capacity_pages=device_capacity,
                counter_basis=counter_basis, timestamp=timestamp,
            )
        _upsert_state(conn, state)
        _upsert_profile_if_high(conn, state)
        profile = _load_profile(conn, cartridge_key)
        return _state_public_dict(state, profile, interval_decision=v2_dec)


def _extract_exact_level(toner: dict) -> Optional[float]:
    """Derives sub-percent supply level when SNMP prtMarkerSuppliesMaxCapacity > 100 (raw units)."""
    try:
        max_v = toner.get("max")
        rem_v = toner.get("remaining")
        unit_code = toner.get("unit_code")
        if max_v is None or rem_v is None:
            return None
        max_i = int(max_v)
        rem_i = int(rem_v)
        # Only when max_i > 100 (sub-percent resolution) and unit_code != 19 (not coarse percent)
        if max_i > 100 and 0 <= rem_i <= max_i and unit_code != 19:
            return round((rem_i * 100.0) / max_i, 4)
    except (TypeError, ValueError):
        return None
    return None


def process_printer_yield_snapshot(*, ip: str, printer_model: str, counters: dict,
                                   toners: Dict[str, dict], device_type: str,
                                   timestamp: str = None, source: str = "poll",
                                   swapped_colors=None,
                                   quarantined_colors=None,
                                   virtual_levels: Optional[Dict[str, int]] = None) -> Dict[str, dict]:
    """Processes all toners of one printer and returns per-color metadata."""
    results = {}
    if not isinstance(toners, dict):
        return results
    swapped_set = {str(c).lower() for c in (swapped_colors or ()) if c}
    quarantined_set = {str(c).lower() for c in (quarantined_colors or ()) if c}
    virt_map = {str(k).lower(): v for k, v in (virtual_levels or {}).items()}
    ensure_yield_tables()
    # A replacement ID may arrive while its supplies row is temporarily missing.
    # Retire that slot's old anchors too, without inventing a displayed supply.
    for color in sorted(set(toners) | swapped_set, key=lambda c: _COLOR_ORDER.index(c) if c in _COLOR_ORDER else 99):
        toner = toners.get(color) or {}
        if not isinstance(toner, dict):
            continue
        color_lc = color.lower()
        is_quarantined = (color_lc in quarantined_set) and (color_lc not in swapped_set)
        # B-3: a toner that has a raw_level key but no device reading (Canon status code, manual-override estimate)
        # is shown with its display level and must never be learned from. A toner without the key keeps the old
        # meaning: `level` is the reading.
        has_raw_key = "raw_level" in toner
        raw_level = toner.get("raw_level") if has_raw_key else toner.get("level")
        level_is_synthetic = (not is_quarantined) and has_raw_key and raw_level is None and toner.get("level") is not None
        level = toner.get("level") if level_is_synthetic else raw_level
        exact_level = None if (level_is_synthetic or is_quarantined) else _extract_exact_level(toner)
        name = toner.get("name") or color
        device_capacity_pages = toner.get("device_capacity_pages")
        virt_lv = virt_map.get(color_lc, toner.get("level")) if is_quarantined else None
        try:
            results[color] = process_cartridge_snapshot(
                ip=ip,
                color=color,
                printer_model=printer_model,
                cartridge_name=name,
                level=level,
                raw_level=raw_level,
                counters=counters,
                device_type=device_type,
                timestamp=timestamp,
                source=source,
                device_capacity_pages=device_capacity_pages,
                level_is_synthetic=level_is_synthetic,
                exact_level=exact_level,
                hardware_swap=(color_lc in swapped_set),
                chip_quarantined=is_quarantined,
                virtual_display_level=virt_lv,
                _ensure_tables=False,
            )
        except Exception as exc:
            log.exception("Yield engine failed for %s/%s: %s", ip, color, exc)
    return results


def register_manual_refill(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                           new_level: int, counters: dict, device_type: str = "unknown",
                           timestamp: str = None,
                           replacement_reason: Optional[str] = None,
                           quarantine_chip: bool = False) -> dict:
    """Records a manual reset/refill as a valid anchor for the Yield Engine.

    `replacement_reason`:
      - `"empty"`: physical toner depleted (faded/white print). Full cycle consumption (`start_level -> 0%`)
        is closed even when the hardware chip was frozen/unreset (e.g. stuck at 90%), and a verified `0%`
        interval endpoint is persisted before resetting to `new_level`.
      - `"dirty"`: early cartridge replacement due to print dirtiness/streaks/drum-blade defect (common on
        Canon, HP, Brother) while toner still remained. Never closes as `0%` full depletion; treats the printed
        pages as a right-censored lower bound (`Y_true > pages_delta`), preserving learned capacity from
        downward bias (or ratcheting up if `pages_delta` already exceeded an underestimated yield).
      - `None` / `"auto"`: backward-compatible automatic sensor/page inference.
    """
    ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
    reason_norm = _normalize_replacement_reason(replacement_reason)
    # Same rule as process_cartridge_snapshot: a blank or 'Unknown' model/name keeps the stored identity (R2 fix).
    model_missing = _is_placeholder_model(printer_model)
    name_missing = _is_placeholder_name(cartridge_name, color)
    printer_model = printer_model or "Unknown"
    cartridge_name = cartridge_name or color
    cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
    level_i = _valid_level(new_level)
    if level_i is None:
        level_i = 100
    usage_counter, counter_basis, counter_quality = _compute_usage_counter(color, counters or {}, device_type)
    catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
    catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))

    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color) or {}
        if state and (model_missing or name_missing):
            printer_model, cartridge_name = _identity_from_stored(
                state, printer_model, cartridge_name, color, model_missing, name_missing)
            cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
            catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
            catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
        profile = _load_profile(conn, cartridge_key)
        if _is_cartridge_swap(state, cartridge_key):
            # Manual reset with a different cartridge: start from an empty state (R2). The old cycle is not
            # closed, because its end level is unknown and the estimate would be biased.
            log.info("  [%s/%s] manual reset with a different cartridge (%s -> %s): learned state restarts",
                     ip, color, state.get("cartridge_key"), cartridge_key)
            state = {}
        inherited_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
        inherited_source = state.get("yield_source") or "default"
        inherited_conf = state.get("confidence") or "low"
        if inherited_yield == DEFAULT_YIELD_PER_PAGE and inherited_source == "default" and catalog_capacity:
            inherited_yield = catalog_capacity
            inherited_source = "catalog"
            inherited_conf = (catalog_match or {}).get("confidence") or "medium"
        if inherited_yield == DEFAULT_YIELD_PER_PAGE and profile and profile.get("confidence") == "high" \
                and _profile_is_trusted(profile, color):
            inherited_yield = int(profile.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
            inherited_source = "shared_profile"
            inherited_conf = "high"

        cycle_update = {}
        prev_cyc_status = str(state.get("cycle_status") or "active")
        was_quarantined_or_frozen = prev_cyc_status in ("chip_quarantined", "frozen_chip", "overrun_alert")
        if state and usage_counter is not None:
            _rated_manual = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
            ref_cap_for_gate = _rated_manual or inherited_yield or DEFAULT_YIELD_PER_PAGE
            start_lvl = _to_int(state.get("cycle_start_level"), 100)
            start_cnt = _to_int(state.get("cycle_start_counter"))
            last_valid_lvl = _to_int(state.get("last_valid_level"))
            pages_delta = (usage_counter - start_cnt) if (start_cnt is not None and usage_counter >= start_cnt) else 0

            if reason_norm == "empty":
                # Technician explicitly confirmed physical toner ran empty:
                # guard only against accidental double-click (< 15% of reference capacity).
                min_empty_pages = max(MIN_PAGES_FOR_SAMPLE, int(0.15 * ref_cap_for_gate))
                if start_cnt is not None and pages_delta >= min_empty_pages:
                    cycle_update = _maybe_close_cycle(
                        conn, state, ip=ip, color=color, printer_model=printer_model,
                        cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                        usage_counter=usage_counter, timestamp=timestamp,
                        counter_basis=counter_basis, source="cycle_manual_empty",
                        rated_capacity=_rated_manual,
                        end_level_before_refill=0,
                        accepted_source="cycle_manual_empty",
                    ) or {}
                    if cycle_update and (was_quarantined_or_frozen or last_valid_lvl is None or last_valid_lvl > 5):
                        # Record verified physical 0% interval endpoint for v2 interval estimator
                        _record_snapshot(
                            conn, ip, color, printer_model, cartridge_name, cartridge_key,
                            timestamp, counters or {}, usage_counter, 0, 0,
                            "manual_empty_confirm", valid=True, reject_reason=None,
                        )
                elif start_cnt is not None and pages_delta > 0:
                    _record_sample(
                        conn, ip=ip, color=color, printer_model=printer_model,
                        cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                        start_counter=start_cnt, end_counter=usage_counter,
                        start_level=start_lvl, end_level=0, pages_delta=pages_delta,
                        toner_drop=min(max(start_lvl, 1), 100),
                        estimated_yield=int(round(pages_delta * 100.0 / max(1, min(start_lvl, 100)))),
                        counter_basis=counter_basis, weight=0, accepted=False,
                        reject_reason="manual_empty_too_short", source="cycle_manual_empty",
                    )

            elif reason_norm == "dirty":
                # Cartridge replaced early due to dirty prints / drum / wiper blade (Canon, HP, Brother):
                # 1. If sensor had ALREADY genuinely hit 0% earlier, close zero cycle normally.
                if state.get("zero_reached_counter") is not None:
                    cycle_update = _maybe_close_cycle(
                        conn, state, ip=ip, color=color, printer_model=printer_model,
                        cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                        usage_counter=usage_counter, timestamp=timestamp,
                        counter_basis=counter_basis, source="cycle_dirty_after_zero",
                        rated_capacity=_rated_manual,
                        end_level_before_refill=0,
                    ) or {}
                # 2. If a healthy, non-quarantined fine-grained chip genuinely dropped >= 25%,
                #    use the partial sensor slope with cautious 0.5x weight (never 0% endpoint).
                elif (
                    not was_quarantined_or_frozen
                    and last_valid_lvl is not None
                    and 0 < last_valid_lvl < start_lvl
                    and (start_lvl - last_valid_lvl) >= DIRTY_PARTIAL_MIN_DROP_PCT
                    and get_brand_spec(printer_model).typical_step_pct < 5.0
                ):
                    cycle_update = _maybe_close_cycle(
                        conn, state, ip=ip, color=color, printer_model=printer_model,
                        cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                        usage_counter=usage_counter, timestamp=timestamp,
                        counter_basis=counter_basis, source="cycle_dirty_partial",
                        rated_capacity=_rated_manual,
                        end_level_before_refill=last_valid_lvl,
                        weight_scale=0.5,
                        accepted_source="cycle_dirty_partial",
                    ) or {}
                # 3. Right-censored lower bound (`Y_true > pages_delta * 100 / start_lvl`):
                #    never drag learned capacity down, but ratchet up if underestimated!
                elif start_cnt is not None and pages_delta >= MIN_PAGES_FOR_SAMPLE:
                    eff_start = max(1, min(start_lvl, 100))
                    lower_bound_y = int(round(pages_delta * 100.0 / eff_start))
                    in_band = (
                        MIN_ESTIMATED_YIELD <= lower_bound_y <= MAX_ESTIMATED_YIELD
                        and (not _rated_manual or lower_bound_y <= CYCLE_RATED_BAND[1] * _rated_manual)
                    )
                    if in_band and lower_bound_y > inherited_yield:
                        ratcheted = int(round(0.4 * inherited_yield + 0.6 * lower_bound_y))
                        w_lb = 2.0
                        _record_sample(
                            conn, ip=ip, color=color, printer_model=printer_model,
                            cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                            start_counter=start_cnt, end_counter=usage_counter,
                            start_level=start_lvl, end_level=last_valid_lvl,
                            pages_delta=pages_delta, toner_drop=eff_start,
                            estimated_yield=lower_bound_y, counter_basis=counter_basis,
                            weight=w_lb, accepted=True, reject_reason=None,
                            source="dirty_lower_bound",
                        )
                        inherited_yield = ratcheted
                        inherited_source = "cycle_learn"
                        if inherited_conf == "low":
                            inherited_conf = "medium"
                        state["sample_count"] = int(state.get("sample_count") or 0) + 1
                        state["total_weight"] = float(state.get("total_weight") or 0.0) + w_lb
                        state["learned_at"] = timestamp
                    else:
                        _record_sample(
                            conn, ip=ip, color=color, printer_model=printer_model,
                            cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                            start_counter=start_cnt, end_counter=usage_counter,
                            start_level=start_lvl, end_level=last_valid_lvl,
                            pages_delta=pages_delta, toner_drop=0,
                            estimated_yield=lower_bound_y if lower_bound_y > 0 else None,
                            counter_basis=counter_basis, weight=0, accepted=False,
                            reject_reason="dirty_cartridge_early_swap",
                            source="manual_reset_dirty",
                        )

            else:
                # Legacy / unspecified reason: preserve existing sensor/page inference, while
                # avoiding bogus partial-drop math if the chip was quarantined/frozen!
                if state.get("zero_reached_counter") is not None:
                    end_lvl_manual = 0
                elif (
                    not was_quarantined_or_frozen
                    and last_valid_lvl is not None
                    and 0 < last_valid_lvl < start_lvl
                ):
                    end_lvl_manual = last_valid_lvl
                else:
                    min_manual_pages = max(MIN_ESTIMATED_YIELD, int(0.30 * ref_cap_for_gate))
                    end_lvl_manual = 0 if (start_cnt is not None and pages_delta >= min_manual_pages) else start_lvl

                cycle_update = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=usage_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_manual_refill",
                    rated_capacity=_rated_manual,
                    end_level_before_refill=end_lvl_manual,
                ) or {}
                if cycle_update and was_quarantined_or_frozen and end_lvl_manual == 0:
                    _record_snapshot(
                        conn, ip, color, printer_model, cartridge_name, cartridge_key,
                        timestamp, counters or {}, usage_counter, 0, 0,
                        "manual_empty_confirm", valid=True, reject_reason=None,
                    )

            if cycle_update:
                inherited_yield = cycle_update.get("yield_per_page", inherited_yield)
                inherited_source = cycle_update.get("yield_source", inherited_source)
                inherited_conf = cycle_update.get("confidence", inherited_conf)
                state.update(cycle_update)

        new_cyc_status = "chip_quarantined" if quarantine_chip else "active"
        snap_source = f"manual_reset:{reason_norm}" if reason_norm else "manual_reset"
        state.update({
            "printer_ip": ip,
            "color": color,
            "printer_model": printer_model,
            "cartridge_name": cartridge_name,
            "cartridge_key": cartridge_key,
            "current_level": level_i,
            "last_valid_level": level_i,
            "last_counter": usage_counter,
            "anchor_counter": usage_counter,
            "anchor_level": level_i,
            "anchor_timestamp": timestamp,
            "yield_per_page": inherited_yield,
            "yield_source": inherited_source,
            "confidence": inherited_conf,
            "sample_count": int(state.get("sample_count") or 0),
            "total_weight": float(state.get("total_weight") or 0.0),
            "last_refill_at": timestamp,
            "last_refill_counter": usage_counter,
            "pending_refill_level": None,
            "pending_refill_counter": None,
            "pending_refill_timestamp": None,
            "cycle_start_counter": usage_counter,
            "cycle_start_level": level_i,
            "cycle_start_timestamp": timestamp,
            "zero_reached_counter": None,
            "zero_reached_timestamp": None,
            "pages_after_zero": 0,
            "cycle_status": new_cyc_status,
            "updated_at": _now(),
        })
        _EXACT_ANCHOR_CACHE.pop((ip, color), None)
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters or {}, usage_counter, level_i, level_i,
                         snap_source, valid=usage_counter is not None,
                         reject_reason=None if usage_counter is not None else "missing_counter_manual_reset")
        if _is_v2_estimator_enabled():
            _apply_v2_state_decision(
                conn, state, profile=profile,
                counter_basis=counter_basis, timestamp=timestamp,
            )
        _upsert_state(conn, state)
        _upsert_profile_if_high(conn, state)
        profile = _load_profile(conn, cartridge_key)
        return _state_public_dict(
            state, profile,
            extra_meta={"last_reset_reason": reason_norm},
        )


_YIELD_STATUS_CACHE_LOCK = threading.Lock()
_YIELD_STATUS_SLOT_CACHE: Dict[tuple, dict] = {}
_YIELD_STATUS_CACHE_MAX = 512


def get_yield_status(ip: str = None) -> list:
    params = []
    where = ""
    if ip:
        where = "WHERE printer_ip = ?"
        params.append(ip)
    try:
        with db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
                       current_level, yield_per_page, yield_source, confidence,
                       sample_count, total_weight, anchor_level, anchor_counter,
                       last_refill_at, learned_at, cycle_start_counter, cycle_start_level,
                       zero_reached_counter, pages_after_zero, cycle_status, updated_at, force_estimate
                FROM cartridge_state
                {where}
                ORDER BY printer_ip, color
                """,
                params,
            ).fetchall()

            profiles_by_key: Dict[str, dict] = {}
            snap_by_slot: Dict[Tuple[str, str], list] = {}
            if rows:
                try:
                    for pr in conn.execute(
                        "SELECT cartridge_key, yield_per_page, confidence, sample_count, total_weight, source_printer_ip "
                        "FROM cartridge_yield_profiles"
                    ).fetchall():
                        if pr[0]:
                            profiles_by_key[str(pr[0])] = {
                                "cartridge_key": pr[0],
                                "yield_per_page": pr[1],
                                "confidence": pr[2],
                                "sample_count": pr[3],
                                "total_weight": pr[4],
                                "source_printer_ip": pr[5],
                            }
                except Exception:
                    profiles_by_key = {}

                try:
                    snap_rows = _load_interval_snapshots(conn, ip=ip)
                    for sr in snap_rows:
                        slot = (str(sr[0] or ""), str(sr[1] or "").lower())
                        buf = snap_by_slot.setdefault(slot, [])
                        buf.append(sr[2:])
                except Exception:
                    snap_by_slot = {}

            out = []
            v2_active = _is_v2_estimator_enabled()
            for r in rows:
                p_ip = r[0]
                c_name = str(r[1] or "").lower()
                c_key = r[4]
                cur_lv = r[5]
                ypp_val = r[6]
                prof = profiles_by_key.get(str(c_key or ""))
                slot_snaps = snap_by_slot.get((str(p_ip or ""), c_name), [])

                prof_sig = tuple(sorted(prof.items())) if prof else None
                snaps_sig = (
                    (len(slot_snaps), tuple(slot_snaps[0]), tuple(slot_snaps[-1]), sum(hash(tuple(x)) for x in slot_snaps))
                    if slot_snaps
                    else (0,)
                )
                cache_key = (tuple(r), prof_sig, snaps_sig, v2_active)

                with _YIELD_STATUS_CACHE_LOCK:
                    cached_item = _YIELD_STATUS_SLOT_CACHE.get(cache_key)
                if cached_item is not None:
                    copied = dict(cached_item)
                    if isinstance(copied.get("capacity_ci95"), list):
                        copied["capacity_ci95"] = list(copied["capacity_ci95"])
                    out.append(copied)
                    continue

                item = {
                    "printer_ip": p_ip,
                    "color": r[1],
                    "printer_model": r[2],
                    "cartridge_name": r[3],
                    "cartridge_key": c_key,
                    "current_level": cur_lv,
                    "yield_per_page": ypp_val,
                    "yield_source": r[7],
                    "confidence": r[8],
                    "sample_count": r[9],
                    "total_weight": r[10],
                    "anchor_level": r[11],
                    "anchor_counter": r[12],
                    "last_refill_at": r[13],
                    "learned_at": r[14],
                    "cycle_start_counter": r[15],
                    "cycle_start_level": r[16],
                    "zero_reached_counter": r[17],
                    "pages_after_zero": r[18],
                    "cycle_status": r[19],
                    "updated_at": r[20],
                    "force_estimate": r[21],
                }
                dec = estimate_cartridge_interval(
                    conn,
                    ip=p_ip,
                    color=c_name,
                    state=item,
                    profile=prof,
                    snap_rows=slot_snaps,
                )
                cyc_st = str(r[19] or "active")
                pages_after_z = int(r[18] or 0)
                sensor_state = cyc_st if cyc_st in ("overrun_alert", "chip_quarantined", "frozen_chip") else "healthy"
                frozen_raw_lv = None
                last_reset_reason = None
                if slot_snaps:
                    if sensor_state != "healthy":
                        frozen_raw_lv = _valid_level(slot_snaps[-1][2])
                    for s_row in reversed(slot_snaps):
                        s_src = str(s_row[3] or "")
                        if s_src.startswith("manual_reset:"):
                            last_reset_reason = s_src.split(":", 1)[1] or None
                            break
                        if s_src == "manual_reset":
                            break

                item.update({
                    "sensor_state": sensor_state,
                    "chip_quarantined": cyc_st in ("chip_quarantined", "overrun_alert"),
                    "frozen_chip_level": frozen_raw_lv,
                    "overrun_pages": pages_after_z if sensor_state != "healthy" else 0,
                    "unregistered_refill_suspected": cyc_st == "overrun_alert",
                    "last_reset_reason": last_reset_reason,
                    "interval_capacity_pages": dec.capacity_pages,
                    "capacity_ci95": [dec.capacity_ci95[0], dec.capacity_ci95[1]],
                    "rel_ci95": dec.rel_ci95,
                    "capacity_method": dec.capacity_method,
                    "learning_state": dec.learning_state,
                    "nominal_capacity_pages": dec.nominal_capacity_pages,
                    "coverage_index": dec.coverage_index,
                    "n_plateaus": dec.n_plateaus,
                })
                item.update(_interval_operational_fields(item, dec, v2_active=v2_active))
                with _YIELD_STATUS_CACHE_LOCK:
                    if len(_YIELD_STATUS_SLOT_CACHE) >= _YIELD_STATUS_CACHE_MAX:
                        _YIELD_STATUS_SLOT_CACHE.clear()
                    _YIELD_STATUS_SLOT_CACHE[cache_key] = {
                        **item,
                        "capacity_ci95": list(item["capacity_ci95"]),
                    }
                out.append(item)
            return out
    except UndefinedTable:
        ensure_yield_tables()
        return []


def write_yield_status_report(path: str = "yield_status_report.txt") -> None:
    rows = get_yield_status()
    lines = []
    for r in rows:
        lines.append(
            f"{r['printer_ip']} {r['color']} yield_per_page={r['yield_per_page']} "
            f"source={r['yield_source']} confidence={r['confidence']} "
            f"samples={r['sample_count']} model={json.dumps(r['printer_model'] or '', ensure_ascii=False)} "
            f"cartridge={json.dumps(r['cartridge_name'] or '', ensure_ascii=False)}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")


# =========================================================================
# Six improvements to the Yield Engine for color Toshiba and multi-color support (phase I.5)
# =========================================================================

BRAND_COLOR_FALLBACKS: Dict[str, Dict[str, int]] = {
    "toshiba": {
        "black": 38400,
        "cyan": 33600,
        "magenta": 33600,
        "yellow": 33600,
        "drum": 120000,
        "default": 33600,
    },
    "hp": {
        "black": 9000,
        "cyan": 6000,
        "magenta": 6000,
        "yellow": 6000,
        "drum": 30000,
        "default": 6000,
    },
    "canon": {
        "black": 3100,
        "cyan": 2300,
        "magenta": 2300,
        "yellow": 2300,
        "drum": 23000,
        "default": 2500,
    },
    "brother": {
        "black": 3000,
        "cyan": 1500,
        "magenta": 1500,
        "yellow": 1500,
        "drum": 12000,
        "default": 2000,
    },
    "kyocera": {
        "black": 12000,
        "cyan": 8000,
        "magenta": 8000,
        "yellow": 8000,
        "drum": 100000,
        "default": 8000,
    },
    "ricoh": {
        "black": 24000,
        "cyan": 18000,
        "magenta": 18000,
        "yellow": 18000,
        "drum": 90000,
        "default": 18000,
    },
    "xerox": {
        "black": 13900,
        "cyan": 9000,
        "magenta": 9000,
        "yellow": 9000,
        "drum": 60000,
        "default": 9000,
    },
    "lexmark": {
        "black": 8500,
        "cyan": 5000,
        "magenta": 5000,
        "yellow": 5000,
        "drum": 40000,
        "default": 5000,
    },
    "samsung": {
        "black": 4000,
        "cyan": 3500,
        "magenta": 3500,
        "yellow": 3500,
        "drum": 20000,
        "default": 3500,
    },
    "epson": {
        "black": 6000,
        "cyan": 5000,
        "magenta": 5000,
        "yellow": 5000,
        "drum": 25000,
        "default": 5000,
    },
}


def filter_invalid_toner_levels(levels: list[Any]) -> list[float]:
    """
    Filters out invalid values and negative sentinels (-1, -2, -3) in the toner average calculation.
    Only numeric values between 0.0 and 100.0 are returned.
    """
    valid: list[float] = []
    for val in levels:
        if val is None:
            continue
        try:
            f = float(val)
            if 0.0 <= f <= 100.0:
                valid.append(f)
        except (ValueError, TypeError):
            continue
    return valid


def estimate_yield_fallback(
    brand: str,
    color: str = "black",
    default_pages: int = DEFAULT_YIELD_PER_PAGE,
) -> int:
    """
    Base yield estimate based on brand and color when the cartridge model is unknown (improvement 6).
    """
    b = (brand or "").strip().lower()
    c = (color or "black").strip().lower()

    if b in BRAND_COLOR_FALLBACKS:
        return BRAND_COLOR_FALLBACKS[b].get(c, BRAND_COLOR_FALLBACKS[b].get("default", default_pages))

    for known_brand, color_map in BRAND_COLOR_FALLBACKS.items():
        if known_brand in b:
            return color_map.get(c, color_map.get("default", default_pages))

    return default_pages


def compute_yield_per_color(
    printer_ip: str,
    color: str,
    conn: Optional[Any] = None,
    brand: Optional[str] = None,
) -> Optional[float]:
    """
    Per-color cartridge yield calculation (yield_per_page) for each dedicated color (improvements 1, 4 and 5).

    Algorithm:
      1. Extract the data of past cycles from cartridge_lifecycle, if present.
      2. Scope yield_samples to the active cartridge_key when available (preventing cross-swap mixing).
      3. Use the median instead of the simple average to filter outliers.
      4. Check cartridge_state (skipping uninformative 'default' rows when brand fallback is known).
      5. Fall back to the brand/color estimate if there is no historical data.
    """
    color = (color or "black").strip().lower()

    def _query(c: Any) -> Optional[float]:
        # Discover active cartridge_key / state metadata up front when available
        active_key: Optional[str] = None
        state_ypp: Optional[float] = None
        state_model: Optional[str] = None
        state_source: Optional[str] = None
        is_pg = getattr(c, "backend", "sqlite") == BACKEND_POSTGRESQL
        try:
            r_state = c.execute(
                "SELECT yield_per_page, printer_model, cartridge_key, yield_source "
                "FROM cartridge_state WHERE printer_ip = ? AND color = ?",
                (printer_ip, color),
            ).fetchone()
            if r_state:
                if r_state[0] and r_state[0] > 0:
                    state_ypp = float(r_state[0])
                if r_state[1]:
                    state_model = str(r_state[1])
                if r_state[2]:
                    active_key = str(r_state[2])
                if r_state[3]:
                    state_source = str(r_state[3])
        except Exception:
            if not is_pg:
                try:
                    r_min = c.execute(
                        "SELECT yield_per_page, printer_model FROM cartridge_state WHERE printer_ip = ? AND color = ?",
                        (printer_ip, color),
                    ).fetchone()
                    if r_min:
                        if r_min[0] and r_min[0] > 0:
                            state_ypp = float(r_min[0])
                        if r_min[1]:
                            state_model = str(r_min[1])
                except Exception as exc:
                    log.debug("compute_yield_per_color: cartridge_state fallback query failed: %s", exc)

        # 1. Check closed cycles in cartridge_lifecycle
        samples: list[float] = []
        if not is_pg:
            try:
                rows = c.execute(
                    """
                    SELECT actual_yield_per_page
                    FROM cartridge_lifecycle
                    WHERE printer_ip = ? AND cartridge_color = ? AND actual_yield_per_page IS NOT NULL AND actual_yield_per_page > 0
                    ORDER BY id DESC LIMIT 20
                    """,
                    (printer_ip, color),
                ).fetchall()
                for r in rows:
                    if r[0] and MIN_ESTIMATED_YIELD <= r[0] <= MAX_ESTIMATED_YIELD:
                        samples.append(float(r[0]))
            except Exception:
                pass

        # 2. Check learning samples in yield_samples (scoped to active cartridge_key when present)
        if len(samples) < 3:
            rows = []
            if active_key:
                try:
                    rows = c.execute(
                        """
                        SELECT estimated_yield
                        FROM yield_samples
                        WHERE printer_ip = ? AND color = ? AND accepted = ? AND estimated_yield > 0
                          AND (cartridge_key = ? OR cartridge_key IS NULL OR cartridge_key = '')
                        ORDER BY id DESC LIMIT 20
                        """,
                        (printer_ip, color, True, active_key),
                    ).fetchall()
                except Exception:
                    rows = []
            if not rows:
                try:
                    rows = c.execute(
                        """
                        SELECT estimated_yield
                        FROM yield_samples
                        WHERE printer_ip = ? AND color = ? AND accepted = ? AND estimated_yield > 0
                        ORDER BY id DESC LIMIT 20
                        """,
                        (printer_ip, color, True),
                    ).fetchall()
                except Exception as exc:
                    log.warning(
                        "compute_yield_per_color: yield_samples query failed for %s/%s: %s",
                        printer_ip, color, exc, exc_info=True,
                    )
            for r in rows:
                if r[0] and MIN_ESTIMATED_YIELD <= r[0] <= MAX_ESTIMATED_YIELD:
                    samples.append(float(r[0]))

        # 3. If there are samples, compute the median (Median) to reject outliers
        if samples:
            samples.sort()
            n = len(samples)
            if n % 2 == 1:
                median_val = samples[n // 2]
            else:
                median_val = (samples[n // 2 - 1] + samples[n // 2]) / 2.0
            return round(median_val, 2)

        # 4. Check cartridge_state: if yield_source is 'default' and a brand fallback is known, prefer brand fallback (R5 fix)
        b_name = brand or state_model
        if state_ypp and state_ypp > 0:
            if state_source == "default" and b_name:
                fb_val = estimate_yield_fallback(b_name, color, default_pages=-1)
                if fb_val > 0:
                    return float(fb_val)
            return state_ypp

        # 5. Fallback based on brand and color
        if b_name:
            return float(estimate_yield_fallback(b_name, color))

        return None

    if conn is not None:
        return _query(conn)

    with db_connection() as c:
        return _query(c)

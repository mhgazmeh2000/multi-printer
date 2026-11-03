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
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from core.database import db_connection
from core.db import BACKEND_POSTGRESQL
from core.db import upsert_sql as _upsert_sql
from core.db.schema import YIELD_TABLES as _PG_YIELD_TABLES, verify_postgres_tables
from core.time_utils import as_utc_datetime, utc_now_iso

log = logging.getLogger("PrinterMonitor")

DEFAULT_YIELD_PER_PAGE = 2000
MIN_PAGES_FOR_SAMPLE = 10
MIN_ESTIMATED_YIELD = 300
MAX_ESTIMATED_YIELD = 100000
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
# within this band; outside it the data is poisoned/noisy and we return the reference value
# (self-heal for old poisoned states such as yield=23800 on T-4590).
LEARNED_DEVIATION_LIMIT = 0.40
CYCLE_RATED_BAND = (0.30, 2.5)

_COLOR_ORDER = ("black", "cyan", "magenta", "yellow", "drum")


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
    """Chooses the right counter for each color + confidence factor."""
    total = _to_int(counters.get("total"))
    full_color = _to_int(counters.get("full_color"))
    black_white = _to_int(counters.get("black_white"))
    color = (color or "black").lower()
    device_type = (device_type or "unknown").lower()

    if color in ("cyan", "magenta", "yellow"):
        if full_color is not None:
            return full_color, "full_color", 1.0
        if total is not None:
            return total, "total_for_color", 0.45
        return None, "missing", 0.0

    if color == "black":
        # For black, total is more conservative and usually more available; on mono printers it is exact.
        if total is not None:
            quality = 1.0 if device_type != "color" else 0.75
            return total, "total_for_black", quality
        if black_white is not None:
            return black_white, "black_white", 0.9
        return None, "missing", 0.0

    # drum/opc and non-toner consumables: if they have a level, they are tracked with low confidence via total.
    if total is not None:
        return total, "total_for_supply", 0.5
    return None, "missing", 0.0


def _sample_weight(pages_delta: int, toner_drop: int, counter_quality: float) -> float:
    if pages_delta <= 0 or toner_drop <= 0 or counter_quality <= 0:
        return 0.0
    page_factor = min(1.0, max(0.2, pages_delta / 100.0))
    drop_factor = min(5.0, float(toner_drop))
    return round(page_factor * drop_factor * counter_quality, 3)


def _derive_confidence(sample_count: int, total_weight: float, counter_basis: str = "") -> str:
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
    return "low"


def _blend_yield(old_yield: int, old_weight: float, estimated_yield: int, sample_weight: float) -> int:
    if old_yield is None or old_yield <= 0 or old_yield == DEFAULT_YIELD_PER_PAGE and old_weight <= 0:
        return int(estimated_yield)
    total_w = max(0.0, old_weight) + max(0.1, sample_weight)
    return int(round(((old_yield * max(0.0, old_weight)) + (estimated_yield * max(0.1, sample_weight))) / total_w))


#: (ip, color) slots for which a legacy sample without cartridge_key was used (warned once per process).
_LEGACY_FALLBACK_WARNED: set = set()


def _is_cartridge_swap(state: Optional[dict], cartridge_key: str) -> bool:
    """True when the slot's stored cartridge_key differs from the cartridge now reported in the same slot.

    Not a swap when:
    - the stored row has no cartridge_key (legacy row: keep today's behaviour, no reset), or
    - the stored identity is a placeholder (model "Unknown", or the name equal to the colour fallback),
      which means the metadata is being completed, not that the cartridge changed.
    """
    old_key = (state or {}).get("cartridge_key")
    if not old_key or old_key == cartridge_key:
        return False
    old_model = _norm_text(state.get("printer_model"))
    old_color = _norm_text(state.get("color"))
    old_name = _norm_text(state.get("cartridge_name"))
    if old_model == "unknown" or old_name in ("unknown", old_color):
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


def _robust_learned_yield(conn, ip: str, color: str, cartridge_key: str,
                          rated: Optional[int] = None, limit: int = 12):
    """Robust yield estimate from accepted samples — weighted median of the N most recent samples.

    Why median: a moving weighted average stays skewed for weeks by one outlier sample (like a fake 320-page cycle
    or a quantized 38,600); a weighted median neutralizes such samples. If a reference capacity (catalog/device)
    exists, samples that are far from it are not used at all:
    those outside the 0.25× to 2.5× band are excluded from the calculation.

    Output: (yield or None, total weight of these mini-samples)
    """
    try:
        # Samples of another cartridge that used the same slot must not feed this median (R2).
        # Legacy rows written before cartridge_key existed have NULL/'' keys; they are still matched by slot
        # so that existing learning is not lost. Migration note: once legacy rows have been re-keyed (or have
        # aged out of the LIMIT window), the slot fallback below can be removed.
        rows = conn.execute(
            # Phase E.4.1: "accepted=1" on PG (BOOLEAN column) gives a type error ⇒
            # comparison with a bool parameter; it has the same meaning on both backends.
            """SELECT estimated_yield, confidence_weight,
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
    if any(r[2] for r in rows) and (ip, color) not in _LEGACY_FALLBACK_WARNED:
        _LEGACY_FALLBACK_WARNED.add((ip, color))
        log.warning("yield %s/%s: using legacy samples without cartridge_key for this slot (matched by slot)",
                    ip, color)
    pts = [(int(r[0]), max(0.1, float(r[1] or 0.1))) for r in rows if r[0]]
    if rated:
        lo, hi = rated * 0.25, rated * 2.5
        pts = [p for p in pts if lo <= p[0] <= hi]
    if not pts:
        return None, 0.0
    pts.sort(key=lambda p: p[0])
    total_w = sum(w for _, w in pts)
    acc = 0.0
    for y, w in pts:
        acc += w
        if acc >= total_w / 2.0:
            return y, total_w
    return pts[-1]


#: columns of ``cartridge_yield_profiles`` written by ``_upsert_profile_if_high``
#: (all 10 columns of the table). ``confidence`` has the constant ``'high'`` in ``VALUES`` and is not a placeholder;
#: that is why ``values_sql`` is passed explicitly.
_PROFILE_UPSERT_COLUMNS = (
    "cartridge_key", "printer_model", "cartridge_name", "color", "yield_per_page",
    "confidence", "sample_count", "total_weight", "source_printer_ip", "updated_at",
)
_PROFILE_UPSERT_VALUES = "?, ?, ?, ?, ?, 'high', ?, ?, ?, ?"


def _upsert_profile_sql(conn) -> str:
    """Profile table upsert statement, using the backend of **the same connection** (not the environment)."""
    return _upsert_sql(
        "cartridge_yield_profiles", _PROFILE_UPSERT_COLUMNS, ("cartridge_key",),
        values_sql=_PROFILE_UPSERT_VALUES, backend=conn.backend)


def _upsert_profile_if_high(conn, state: dict) -> None:
    if state.get("confidence") != "high":
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

    # Record for the other cartridges with the same model and name; learned/high themselves are not overwritten.
    conn.execute(
        """
        UPDATE cartridge_state
        SET yield_per_page = ?, yield_source = 'shared_profile', confidence = 'high', updated_at = ?
        WHERE cartridge_key = ?
          AND NOT (yield_source = 'auto_learn' AND confidence = 'high')
        """,
        (ypp, _now(), cartridge_key),
    )


def _state_public_dict(state: dict, profile: Optional[dict] = None) -> dict:
    result = {
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
        "pages_after_zero": _to_int(state.get("pages_after_zero"), 0),
        "cycle_status": state.get("cycle_status") or "active",
        "cartridge_key": state.get("cartridge_key"),
    }
    if profile:
        result["shared_profile"] = {
            "yield_per_page": profile.get("yield_per_page"),
            "confidence": profile.get("confidence"),
            "sample_count": profile.get("sample_count"),
            "source_printer_ip": profile.get("source_printer_ip"),
        }
    return result


def _maybe_close_cycle(conn, state: dict, *, ip: str, color: str, printer_model: str,
                       cartridge_name: str, cartridge_key: str, usage_counter: int,
                       timestamp: str, counter_basis: str, source: str,
                       rated_capacity: Optional[int] = None) -> Optional[dict]:
    """If the previous cycle is complete, build a real yield from the difference of the two charges' counters.

    This path is for cases where the toner stays at 0% but printing continues; when the
    next reset/refill happens, all pages of the previous cycle become samples with high confidence.
    """
    start_counter = _to_int(state.get("cycle_start_counter"))
    start_level = _to_int(state.get("cycle_start_level"), 100)
    if start_counter is None or usage_counter is None or usage_counter <= start_counter or start_level <= 0:
        return None
    pages_delta = usage_counter - start_counter
    estimated_yield = int(round(pages_delta * 100.0 / min(start_level, 100)))
    if estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
        _record_sample(
            conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
            cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
            start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
            estimated_yield=estimated_yield, counter_basis=counter_basis, weight=0,
            accepted=False, reject_reason="cycle_yield_out_of_range", source=source,
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
            start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
            estimated_yield=estimated_yield, counter_basis=counter_basis, weight=0,
            accepted=False, reject_reason="cycle_vs_rated_out_of_band", source=source,
        )
        return None
    weight = max(4.0, min(12.0, pages_delta / 100.0))
    _record_sample(
        conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
        cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
        start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
        estimated_yield=estimated_yield, counter_basis=counter_basis, weight=weight,
        accepted=True, reject_reason=None, source="cycle_learn",
    )
    return {
        "yield_per_page": estimated_yield,
        "yield_source": "cycle_learn",
        "confidence": "high",
        "sample_count": int(state.get("sample_count") or 0) + 1,
        "total_weight": float(state.get("total_weight") or 0.0) + weight,
        "learned_at": timestamp,
        "cycle_status": "closed",
    }


def process_cartridge_snapshot(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                               level, raw_level=None, counters: dict, device_type: str,
                               timestamp: str = None, source: str = "poll",
                               device_capacity_pages: int = None,
                               _ensure_tables: bool = True) -> dict:
    """Processes one cartridge and returns metadata that can be displayed in the API/UI."""
    if _ensure_tables:
        ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
    printer_model = printer_model or "Unknown"
    cartridge_name = cartridge_name or color
    cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
    counters = counters or {}
    level_i = _valid_level(level)
    device_capacity = _valid_device_capacity(device_capacity_pages)
    catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
    catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
    usage_counter, counter_basis, counter_quality = _compute_usage_counter(color, counters, device_type)

    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color)
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
            if profile and profile.get("confidence") == "high":
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
            _upsert_state(conn, state)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=level_i is not None and usage_counter is not None,
                             reject_reason=None if level_i is not None and usage_counter is not None else "missing_level_or_counter")
            return _state_public_dict(state, profile)

        # Reference capacity (device-declared first, then catalog) — the "principled" capacity anchor
        rated_capacity = device_capacity or catalog_capacity

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

        # ✅ Self-healing of poisoned states (real data: Toshiba .40 with yield=23,800 from
        # noise, while the catalog rating is 36,600; or .41 with a fake cycle of ~320):
        # if the learned capacity is more than 40% away from the reference, the reference is shown
        # and the contaminated counters are reset to zero so that fresh, clean learning starts.
        if rated_capacity and state.get("yield_source") in ("auto_learn", "cycle_learn", "learned"):
            cur_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            if cur_yield and abs(cur_yield - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity:
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
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="counter_decreased_anchor_reset")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        prev_level = _to_int(state.get("last_valid_level"))
        anchor_level = _to_int(state.get("anchor_level"))
        anchor_counter = _to_int(state.get("anchor_counter"))

        # ─── Surface anti-noise gate (single-pixel blip) ─────────────────────────
        # ✅ Real data (TOSHIBA 0.40/0.41): stable level of 100% + lone polls at 8%.
        # Without this gate, every blip would settle the anchor at 8 → the return to 100 would be detected
        # as a "charge" → cycle_learn would record a yield of 320-400 from poisoned data.
        # A severe drop with almost zero pages = read noise: anchor and last_valid_level are not moved,
        # but last_counter moves forward so that its pages are preserved.
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
                cycle_update = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=usage_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_auto_refill",
                    rated_capacity=rated_capacity,
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
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        toner_drop = anchor_level - level_i
        pages_delta = usage_counter - anchor_counter

        # Stale anchor: a refill candidate was recorded (pending_refill_level is set) and the level is now at
        # least REFILL_JUMP_PERCENT above the anchor, but the confirmation never ran. A single-step refill
        # (e.g. 0% -> 100%) is recorded as a candidate and last_valid_level moves to 100, so the next poll sees
        # no new jump and the confirmation never runs; the anchor then stays at the old level and learning
        # freezes. Restart the cycle here and record the refill time. Gradual rises with no pending candidate
        # do not match this branch. It returns early and never reaches the sample path, so the noise gate,
        # the quantization gate and the rated-capacity band are unchanged.
        pending_level = _to_int(state.get("pending_refill_level"))
        if pending_level is not None and level_i - anchor_level >= REFILL_JUMP_PERCENT:
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
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="stale_anchor_reset")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # Stable level: keep the anchor so that low-usage printers also get cumulative calculations.
        if toner_drop <= 0:
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        estimated_yield = int(round(pages_delta * 100.0 / toner_drop)) if toner_drop > 0 else None
        reject_reason = None
        accepted = True
        if pages_delta < MIN_PAGES_FOR_SAMPLE:
            accepted = False
            reject_reason = "not_enough_pages_yet"
        elif toner_drop == 1 and pages_delta < QUANT_MIN_PAGES_1PCT:
            # A quantized sample of "1% with few pages" has a large relative error (real example:
            # 11 pages → estimate of 1100 for Cartridge 737 with a rating of 2400). Wait until
            # enough good pages accumulate or the drop reaches 2%+; then the ratio is reliable.
            accepted = False
            reject_reason = "quantization_1pct_noise"
        elif estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
            accepted = False
            reject_reason = "estimated_yield_out_of_range"

        weight = _sample_weight(pages_delta, toner_drop, counter_quality) if accepted else 0.0
        _record_sample(conn, ip=ip, color=color, printer_model=printer_model,
                       cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                       start_counter=anchor_counter, end_counter=usage_counter,
                       start_level=anchor_level, end_level=level_i,
                       pages_delta=pages_delta, toner_drop=toner_drop,
                       estimated_yield=estimated_yield, counter_basis=counter_basis,
                       weight=weight, accepted=accepted, reject_reason=reject_reason, source=source)

        if accepted:
            old_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            old_source = state.get("yield_source") or "default"
            old_confidence = state.get("confidence") or "low"
            old_weight = float(state.get("total_weight") or 0.0)
            new_weight = old_weight + weight
            sample_count = int(state.get("sample_count") or 0) + 1
            # ✅ Weighted median of recent samples (outlier-resistant) instead of a moving average
            _rb, _rb_w = _robust_learned_yield(conn, ip, color, cartridge_key, rated=rated_capacity)
            new_yield = _rb if _rb else _blend_yield(old_yield, old_weight, estimated_yield, weight)
            confidence = _derive_confidence(sample_count, new_weight, counter_basis)

            # A low-confidence sample, especially for colors or toners that have just
            # reached 0/1%, can create an unrealistic jump. So if a better baseline exists, such as
            # catalog/device_capacity/shared_profile, auto_learn with low confidence must not
            # override the displayed capacity. The sample is recorded and will be used by later samples.
            keep_existing_capacity = (
                (old_source in ("catalog", "device_capacity") and confidence == "low") or
                (old_source == "shared_profile" and confidence != "high")
            )
            # ✅ Compatibility rule with the reference: when a reference capacity (catalog/device) exists,
            # the learned value is only displayed when it is within the ±40% band of the reference;
            # outside it is a sign of noise or an unusual cartridge, and the reference stays displayed.
            if (rated_capacity and new_yield
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
            # For not_enough_pages_yet we deliberately do not change the anchor, so that the next drop accumulates.
            if reject_reason != "not_enough_pages_yet":
                state.update({"anchor_counter": usage_counter, "anchor_level": level_i, "anchor_timestamp": timestamp})

        state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters, usage_counter, level_i, raw_level, source,
                         valid=accepted, reject_reason=reject_reason)
        _upsert_state(conn, state)
        _upsert_profile_if_high(conn, state)
        profile = _load_profile(conn, cartridge_key)
        return _state_public_dict(state, profile)


def process_printer_yield_snapshot(*, ip: str, printer_model: str, counters: dict,
                                   toners: Dict[str, dict], device_type: str,
                                   timestamp: str = None, source: str = "poll") -> Dict[str, dict]:
    """Processes all toners of one printer and returns per-color metadata."""
    results = {}
    if not isinstance(toners, dict):
        return results
    ensure_yield_tables()
    for color in sorted(toners.keys(), key=lambda c: _COLOR_ORDER.index(c) if c in _COLOR_ORDER else 99):
        toner = toners.get(color) or {}
        if not isinstance(toner, dict):
            continue
        raw_level = toner.get("raw_level", toner.get("level"))
        level = raw_level if raw_level is not None else toner.get("level")
        name = toner.get("name") or color
        device_capacity_pages = toner.get("device_capacity_pages")
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
                _ensure_tables=False,
            )
        except Exception as exc:
            log.exception("Yield engine failed for %s/%s: %s", ip, color, exc)
    return results


def register_manual_refill(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                           new_level: int, counters: dict, device_type: str = "unknown",
                           timestamp: str = None) -> dict:
    """Records a manual reset/refill as a valid anchor for the Yield Engine."""
    ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
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
        if inherited_yield == DEFAULT_YIELD_PER_PAGE and profile and profile.get("confidence") == "high":
            inherited_yield = int(profile.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
            inherited_source = "shared_profile"
            inherited_conf = "high"

        cycle_update = {}
        if state and usage_counter is not None:
            _rated_manual = (lambda cm: (cm or {}).get("yield_per_page"))(
                _catalog_lookup(printer_model, cartridge_name, color))
            cycle_update = _maybe_close_cycle(
                conn, state, ip=ip, color=color, printer_model=printer_model,
                cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                usage_counter=usage_counter, timestamp=timestamp,
                counter_basis=counter_basis, source="cycle_manual_refill",
                rated_capacity=_valid_device_capacity(_rated_manual),
            ) or {}
            if cycle_update:
                inherited_yield = cycle_update.get("yield_per_page", inherited_yield)
                inherited_source = cycle_update.get("yield_source", inherited_source)
                inherited_conf = cycle_update.get("confidence", inherited_conf)

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
            "cycle_status": "active",
            "updated_at": _now(),
        })
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters or {}, usage_counter, level_i, level_i,
                         "manual_reset", valid=usage_counter is not None,
                         reject_reason=None if usage_counter is not None else "missing_counter_manual_reset")
        _upsert_state(conn, state)
        return _state_public_dict(state, profile)


def get_yield_status(ip: str = None) -> list:
    ensure_yield_tables()
    params = []
    where = ""
    if ip:
        where = "WHERE printer_ip = ?"
        params.append(ip)
    with db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
                   current_level, yield_per_page, yield_source, confidence,
                   sample_count, total_weight, anchor_level, anchor_counter,
                   last_refill_at, learned_at, cycle_start_counter, cycle_start_level,
                   zero_reached_counter, pages_after_zero, cycle_status, updated_at
            FROM cartridge_state
            {where}
            ORDER BY printer_ip, color
            """,
            params,
        ).fetchall()
    return [
        {
            "printer_ip": r[0],
            "color": r[1],
            "printer_model": r[2],
            "cartridge_name": r[3],
            "cartridge_key": r[4],
            "current_level": r[5],
            "yield_per_page": r[6],
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
        }
        for r in rows
    ]


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
        "default": 33600,
    },
    "hp": {
        "black": 9000,
        "cyan": 6000,
        "magenta": 6000,
        "yellow": 6000,
        "default": 6000,
    },
    "canon": {
        "black": 3100,
        "cyan": 2300,
        "magenta": 2300,
        "yellow": 2300,
        "default": 2500,
    },
    "brother": {
        "black": 3000,
        "cyan": 1500,
        "magenta": 1500,
        "yellow": 1500,
        "default": 2000,
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
      2. Use the median instead of the simple average to filter outliers.
      3. If there are not enough samples, use the samples from cartridge_state and yield_samples.
      4. Fall back to the brand/color estimate if there is no historical data.
    """
    color = (color or "black").strip().lower()

    def _query(c: Any) -> Optional[float]:
        # 1. Check closed cycles in cartridge_lifecycle
        samples: list[float] = []
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
        except Exception as exc:
            log.warning(
                "compute_yield_per_color: cartridge_lifecycle query failed for %s/%s: %s",
                printer_ip, color, exc, exc_info=True,
            )

        # 2. Check learning samples in yield_samples
        if len(samples) < 3:
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
                for r in rows:
                    if r[0] and MIN_ESTIMATED_YIELD <= r[0] <= MAX_ESTIMATED_YIELD:
                        samples.append(float(r[0]))
            except Exception as exc:
                log.warning(
                    "compute_yield_per_color: yield_samples query failed for %s/%s: %s",
                    printer_ip, color, exc, exc_info=True,
                )

        # 3. If there are enough samples, compute the median (Median) after removing outliers
        if samples:
            samples.sort()
            n = len(samples)
            if n % 2 == 1:
                median_val = samples[n // 2]
            else:
                median_val = (samples[n // 2 - 1] + samples[n // 2]) / 2.0
            return round(median_val, 2)

        # 4. Check cartridge_state
        b_name = brand
        try:
            r = c.execute(
                "SELECT yield_per_page, printer_model FROM cartridge_state WHERE printer_ip = ? AND color = ?",
                (printer_ip, color),
            ).fetchone()
            if r and r[0] and r[0] > 0:
                return float(r[0])
            if not b_name and r and r[1]:
                b_name = str(r[1])
        except Exception as exc:
            log.warning(
                "compute_yield_per_color: cartridge_state query failed for %s/%s: %s",
                printer_ip, color, exc, exc_info=True,
            )

        # 5. Fallback based on brand and color
        if b_name:
            return float(estimate_yield_fallback(b_name, color))

        return None

    if conn is not None:
        return _query(conn)

    with db_connection() as c:
        return _query(c)

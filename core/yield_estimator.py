# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/yield_estimator.py
# @brief:        Pure interval-censored, plateau-compressed cartridge yield estimator (v2).
# @details:      Implements the 5-layer yield estimation architecture without DB, Flask, or
#                SNMP side effects:
#                  [1] Declarative BrandSpec & SupplyReading -> interval uncertainty [L_lo, L_hi]
#                  [2] Breakpoint segmentation (refills, gradual swaps, blips, counter resets)
#                      + lossless plateau boundary compression (eliminating idle-poll autocorrelation)
#                  [3] Huberized Interval-Censored IRLS / Golden-Section Estimator in rate space
#                      r = 100 / Y with sandwich overdispersion & exact asymmetric CI95 bounds
#                  [4] Empirical Bayes hierarchical prior fusion + CI-based confidence & learning_state
#                  [5] Separation of nominal_capacity_pages, coverage_index, and operational eta_pages
# =============================================================================

"""Pure statistical interval-censored yield estimator compatible with all 10 project brands."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MIN_PHYSICAL_YIELD = 300
MAX_PHYSICAL_YIELD = 600000
DEFAULT_PRIOR_YIELD = 2000


# =============================================================================
# Layer 1 — Declarative Brand Capability & Uncertainty Specifications
# =============================================================================

@dataclass(frozen=True)
class BrandSpec:
    """Declarative telemetry & sensor uncertainty profile for a printer vendor."""

    vendor: str
    percent_half_width: float = 0.5
    scrape_half_width: float = 2.0
    typical_step_pct: float = 1.0
    has_color_counter: bool = True
    has_supply_counter: bool = False
    max_minus_3_is_unknown: bool = True
    unit_19_is_percent: bool = True
    blip_prone: bool = False
    prior_cv: float = 0.25


BRAND_SPECS: Dict[str, BrandSpec] = {
    "hp": BrandSpec(
        vendor="hp",
        percent_half_width=0.5,
        scrape_half_width=1.5,
        typical_step_pct=1.0,
        has_color_counter=True,
        has_supply_counter=True,
        prior_cv=0.22,
    ),
    "toshiba": BrandSpec(
        vendor="toshiba",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=1.0,
        has_color_counter=True,
        blip_prone=True,
        prior_cv=0.25,
    ),
    "canon": BrandSpec(
        vendor="canon",
        percent_half_width=0.5,
        scrape_half_width=2.5,
        typical_step_pct=1.0,
        has_color_counter=True,
        prior_cv=0.28,
    ),
    "brother": BrandSpec(
        vendor="brother",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=1.0,
        has_color_counter=False,
        has_supply_counter=True,
        prior_cv=0.28,
    ),
    "kyocera": BrandSpec(
        vendor="kyocera",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=5.0,
        has_color_counter=True,
        prior_cv=0.25,
    ),
    "ricoh": BrandSpec(
        vendor="ricoh",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=5.0,
        has_color_counter=True,
        prior_cv=0.25,
    ),
    "xerox": BrandSpec(
        vendor="xerox",
        percent_half_width=0.5,
        scrape_half_width=1.5,
        typical_step_pct=1.0,
        has_color_counter=True,
        prior_cv=0.24,
    ),
    "lexmark": BrandSpec(
        vendor="lexmark",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=5.0,
        has_color_counter=True,
        prior_cv=0.25,
    ),
    "samsung": BrandSpec(
        vendor="samsung",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=1.0,
        has_color_counter=True,
        prior_cv=0.28,
    ),
    "epson": BrandSpec(
        vendor="epson",
        percent_half_width=0.5,
        scrape_half_width=2.0,
        typical_step_pct=1.0,
        has_color_counter=True,
        prior_cv=0.28,
    ),
}

DEFAULT_BRAND_SPEC = BrandSpec(vendor="generic")


def get_brand_spec(vendor_or_model: Optional[str]) -> BrandSpec:
    """Resolve BrandSpec from explicit vendor slug or printer model text."""
    text = (vendor_or_model or "").strip().lower()
    if not text:
        return DEFAULT_BRAND_SPEC
    if text in BRAND_SPECS:
        return BRAND_SPECS[text]
    for slug, spec in BRAND_SPECS.items():
        if slug in text:
            return spec
    if any(tok in text for tok in ("laserjet", "officejet", "pagewide", "hewlett")):
        return BRAND_SPECS["hp"]
    if "e-studio" in text or "estudio" in text:
        return BRAND_SPECS["toshiba"]
    if any(tok in text for tok in ("ecosys", "taskalfa")):
        return BRAND_SPECS["kyocera"]
    if any(tok in text for tok in ("versalink", "altalink", "workcentre", "phaser")):
        return BRAND_SPECS["xerox"]
    if any(tok in text for tok in ("im c", "mp c", "aficio")):
        return BRAND_SPECS["ricoh"]
    return DEFAULT_BRAND_SPEC


@dataclass(frozen=True)
class SupplyReading:
    """Normalized single-poll supply observation (Layer 1 contract)."""

    usage_counter: int
    raw_level: Optional[float]
    level_kind: str = "percent"           # percent | capacity_units | http_scrape | status_code | synthetic
    provenance: str = "rfc3805"           # rfc3805 | vendor_oid | http_scrape | learned_oid | manual
    max_capacity: Optional[int] = None
    unit_code: Optional[int] = None
    observed_at: Optional[str] = None
    vendor: str = ""
    color: str = "black"
    counter_quality: float = 1.0
    step_hint_pct: Optional[float] = None
    chip_id: Optional[str] = None
    status_text: Optional[str] = None


# =============================================================================
# Layer 2 — Interval Evidence Representation, Blip Filtering & Segmentation
# =============================================================================

@dataclass(frozen=True)
class IntervalObservation:
    """Interval-censored observation l(P) in [level_lo, level_hi]."""

    usage_counter: int
    level_lo: float
    level_hi: float
    level_mid: float
    level_nominal: float
    level_kind: str
    provenance: str
    observed_at: Optional[str] = None
    is_status_band: bool = False
    chip_id: Optional[str] = None


@dataclass(frozen=True)
class CompressedPlateau:
    """Lossless monotone compression of consecutive polls sharing the same discrete level."""

    level_nominal: float
    level_lo: float
    level_hi: float
    first_counter: int
    last_counter: int
    poll_count: int
    level_kind: str
    provenance: str
    first_observed_at: Optional[str] = None
    last_observed_at: Optional[str] = None


@dataclass
class ConsumptionSegment:
    """Continuous consumption trajectory between two physical breakpoints."""

    observations: List[IntervalObservation] = field(default_factory=list)
    break_reason: str = "initial"
    zero_reached_counter: Optional[int] = None
    pages_after_zero: int = 0
    closed_by_refill: bool = False
    refill_counter: Optional[int] = None


def _finite_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _page_counter(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except ValueError:
            return None
    number = _finite_number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


# `valid` historically describes acceptance of the v1 ratio sample, not just
# sensor validity. Only these known sample-size/ratio rejects retain real raw
# telemetry for the interval estimator. Unknown invalid reasons fail closed.
_RATIO_ONLY_REJECTIONS = frozenset({
    "not_enough_pages_yet", "quantization_1pct_noise", "estimated_yield_out_of_range",
})
_INVALID_TELEMETRY_REJECTIONS = frozenset({
    "synthetic_level", "status_coded_level", "quarantined_chip", "frozen_chip",
    "level_read_noise", "missing_level_or_counter", "counter_decreased_anchor_reset",
    "missing_counter_manual_reset",
})


def snapshot_is_usable(valid, reject_reason) -> bool:
    """Shared admission policy for live v2 estimation and offline DB backtests."""
    reason = str(reject_reason or "")
    if reason in _INVALID_TELEMETRY_REJECTIONS:
        return False
    return valid in (True, 1, "1") or reason in _RATIO_ONLY_REJECTIONS


def _status_text_to_nominal(status_text: Optional[str], raw_level: Optional[float]) -> Optional[float]:
    txt = (status_text or "").strip().lower()
    if txt:
        # Specific low-band phrases must precede the broad terminal word "end".
        if re.search(r"\b(very low|critical|near end)\b", txt):
            return 8.0
        if re.search(r"\b(empty|replace|exhausted|depleted|out|end)\b", txt):
            return 0.0
        if re.search(r"\b(low|prepare|order|warning)\b", txt):
            return 15.0
        if re.search(r"\b(ok|normal|good|ready|some remaining|full)\b", txt):
            return 62.5
    if raw_level is not None and raw_level == -3.0:
        return 62.5  # RFC 3805 "some remaining", not a measured percent
    return None


def reading_to_interval(reading: SupplyReading, spec: Optional[BrandSpec] = None) -> Optional[IntervalObservation]:
    """Convert a SupplyReading into an interval constraint [L_lo, L_hi] (or None if synthetic/invalid)."""
    counter = _page_counter(reading.usage_counter)
    quality = _finite_number(reading.counter_quality)
    if counter is None or quality is None or not 0 < quality <= 1:
        return None
    kind = (reading.level_kind or "percent").lower()
    prov = (reading.provenance or "rfc3805").lower()
    if kind not in {"percent", "capacity_units", "http_scrape", "status_code"}:
        return None

    lvl = _finite_number(reading.raw_level)
    if reading.raw_level is not None and lvl is None:
        return None
    max_capacity = _finite_number(reading.max_capacity)
    if reading.max_capacity is not None and max_capacity is None:
        return None
    if kind == "capacity_units" and (max_capacity is None or max_capacity <= 0):
        return None
    step_hint = _finite_number(reading.step_hint_pct)
    if reading.step_hint_pct is not None and (step_hint is None or step_hint <= 0 or step_hint > 100):
        return None

    spec = spec or get_brand_spec(reading.vendor)
    chip_norm = (reading.chip_id or "").strip() or None

    # RFC 3805 negative sentinel (-3 / -2) or explicit status_text without numeric percent
    if (lvl is None or lvl < 0.0) and (reading.status_text or lvl == -3.0):
        mapped = _status_text_to_nominal(reading.status_text, lvl)
        if mapped is not None:
            lvl = mapped
            kind = "status_code"

    if lvl is None:
        return None

    # Raw capacity units (e.g. impressions, tenth-grams, 0.1% units where max_capacity > 100 and unit != 19)
    if (
        (kind == "capacity_units" or (max_capacity and max_capacity > 100))
        and max_capacity
        and (kind == "capacity_units" or max_capacity > 100)
        and (not spec.unit_19_is_percent or reading.unit_code != 19)
    ):
        max_c = max_capacity
        # If raw_level was passed in raw units [0..max_c] vs already scaled [0..100]:
        if 0.0 <= lvl <= max_c and (kind == "capacity_units" or lvl > 100.0):
            pct = (lvl / max_c) * 100.0
        elif 0.0 <= lvl <= 100.0:
            pct = lvl
        else:
            return None
        half_w = max(0.02, 50.0 / max_c)
        lo = max(0.0, pct - half_w)
        hi = min(100.0, pct + half_w)
        return IntervalObservation(
            usage_counter=counter,
            level_lo=lo,
            level_hi=hi,
            level_mid=0.5 * (lo + hi),
            level_nominal=pct,
            level_kind="capacity_units",
            provenance=prov,
            observed_at=reading.observed_at,
            chip_id=chip_norm,
        )

    if not (0.0 <= lvl <= 100.0):
        return None

    if kind == "status_code":
        # Discrete status bands carry real information ONLY at band transitions
        if lvl <= 2.0:
            lo, hi = 0.0, 2.0
        elif lvl <= 25.0:
            lo, hi = max(0.0, lvl - 7.5), min(30.0, lvl + 7.5)
        else:
            lo, hi = 25.0, 100.0
        return IntervalObservation(
            usage_counter=counter,
            level_lo=lo,
            level_hi=hi,
            level_mid=0.5 * (lo + hi),
            level_nominal=round(lvl, 1),
            level_kind="status_code",
            provenance=prov,
            observed_at=reading.observed_at,
            is_status_band=True,
            chip_id=chip_norm,
        )

    if kind == "http_scrape" or prov == "http_scrape":
        half_w = spec.scrape_half_width
    else:
        step = step_hint if step_hint is not None and step_hint >= 1.0 else 1.0
        half_w = max(spec.percent_half_width, 0.5 * step)

    lo = max(0.0, lvl - half_w)
    hi = min(100.0, lvl + half_w)
    return IntervalObservation(
        usage_counter=counter,
        level_lo=lo,
        level_hi=hi,
        level_mid=0.5 * (lo + hi),
        level_nominal=lvl,
        level_kind=kind,
        provenance=prov,
        observed_at=reading.observed_at,
        chip_id=chip_norm,
    )


def _filter_transient_blips(obs_list: Sequence[IntervalObservation]) -> List[IntervalObservation]:
    """Remove single-poll downward read blips (e.g. Toshiba 100 -> 8 -> 100 with <25 pages)."""
    n = len(obs_list)
    if n < 3:
        return list(obs_list)
    cleaned: List[IntervalObservation] = []
    idx = 0
    while idx < n:
        cur = obs_list[idx]
        if cleaned and idx + 1 < n:
            prev = cleaned[-1]
            nxt = obs_list[idx + 1]
            drop_from_prev = prev.level_nominal - cur.level_nominal
            pages_on_blip = cur.usage_counter - prev.usage_counter
            recover_diff = abs(prev.level_nominal - nxt.level_nominal)
            chip_ids = {o.chip_id for o in (prev, cur, nxt) if o.chip_id}
            continuous = prev.usage_counter <= cur.usage_counter <= nxt.usage_counter
            if (len(chip_ids) <= 1 and continuous and drop_from_prev >= 20.0
                    and 0 <= pages_on_blip <= 25 and recover_diff <= 5.0):
                # Transient single-poll blip: skip it so it neither breaks segments nor skews fit
                idx += 1
                continue
        cleaned.append(cur)
        idx += 1
    return cleaned


def segment_readings(
    readings: Iterable[SupplyReading],
    spec: Optional[BrandSpec] = None,
) -> List[ConsumptionSegment]:
    """Partition chronological SupplyReadings into clean physical consumption segments."""
    raw_obs: List[IntervalObservation] = []
    for r in readings:
        iv = reading_to_interval(r, spec=spec)
        if iv is not None:
            raw_obs.append(iv)
    if not raw_obs:
        return []

    obs_list = _filter_transient_blips(raw_obs)
    segments: List[ConsumptionSegment] = []
    current = ConsumptionSegment(break_reason="initial")

    n = len(obs_list)
    for i, ob in enumerate(obs_list):
        if not current.observations:
            current.observations.append(ob)
            if ob.level_nominal <= 0.0:
                current.zero_reached_counter = ob.usage_counter
            continue

        prev = current.observations[-1]
        # 1a. Explicit hardware chip/serial swap
        if prev.chip_id and ob.chip_id and prev.chip_id != ob.chip_id:
            current.closed_by_refill = True
            current.refill_counter = ob.usage_counter
            segments.append(current)
            current = ConsumptionSegment(observations=[ob], break_reason="chip_swap")
            if ob.level_nominal <= 0.0:
                current.zero_reached_counter = ob.usage_counter
            continue

        # 1b. Counter rollback (service / NVRAM reset)
        if ob.usage_counter < prev.usage_counter:
            segments.append(current)
            current = ConsumptionSegment(observations=[ob], break_reason="counter_reset")
            if ob.level_nominal <= 0.0:
                current.zero_reached_counter = ob.usage_counter
            continue

        # 2. Sustained upward jump (classic refill >=15% OR mid-level cartridge swap >6% sustained across >=2 polls)
        rise = ob.level_nominal - prev.level_nominal
        if rise > 6.0 and not ob.is_status_band:
            # Verify persistence on next poll when available (avoid splitting on a 1-poll upward glitch)
            sustained = True
            if i + 1 < n:
                nxt = obs_list[i + 1]
                if nxt.usage_counter >= ob.usage_counter and (nxt.level_nominal - prev.level_nominal) <= 5.0:
                    sustained = False
            if sustained and (rise >= 15.0 or (i + 1 < n and rise > 6.0)):
                current.closed_by_refill = True
                current.refill_counter = ob.usage_counter
                segments.append(current)
                current = ConsumptionSegment(observations=[ob], break_reason="refill_or_swap")
                if ob.level_nominal <= 0.0:
                    current.zero_reached_counter = ob.usage_counter
                continue
            if not sustained:
                # Ignore 1-poll upward spike
                continue

        # Track 0% plateau reserve pages
        if ob.level_nominal <= 0.0:
            if current.zero_reached_counter is None:
                current.zero_reached_counter = ob.usage_counter
            else:
                current.pages_after_zero = max(0, ob.usage_counter - current.zero_reached_counter)

        current.observations.append(ob)

    if current.observations:
        segments.append(current)
    return segments


def _infer_coarse_step_pct(plateaus: Sequence[CompressedPlateau]) -> float:
    """Detect coarse quantization grid (e.g. 5%, 10%, 20%, 25% steps on Kyocera/Brother/Lexmark/Ricoh/Epson)."""
    if len(plateaus) < 2:
        return 1.0
    if any(p.level_kind in ("capacity_units", "http_scrape", "status_code") for p in plateaus):
        return 1.0
    levels = [p.level_nominal for p in plateaus if p.level_nominal > 0]
    if len(levels) < 2:
        return 1.0
    drops = [abs(plateaus[i].level_nominal - plateaus[i + 1].level_nominal) for i in range(len(plateaus) - 1)]
    positive_drops = [d for d in drops if d > 0]
    if not positive_drops:
        return 1.0
    for grid in (25.0, 20.0, 10.0, 5.0):
        if all(abs(lv % grid) < 1e-6 for lv in levels) and min(positive_drops) >= grid:
            if len(plateaus) >= 3 or abs(positive_drops[0] - grid) < 1e-6:
                return grid
    return 1.0


def compress_plateaus(observations: Sequence[IntervalObservation]) -> List[CompressedPlateau]:
    """Compress consecutive readings at the same level into lossless (first_counter, last_counter) plateaus.

    Also strips post-zero clipped plateau polls (retaining only the first arrival at 0%) so that
    printing on reserve toner at 0% never biases the pre-zero linear consumption slope.
    """
    plateaus: List[CompressedPlateau] = []
    seen_zero = False
    for ob in observations:
        if ob.is_status_band and ob.level_lo >= 25.0 and ob.level_hi >= 100.0:
            # Uninformative wide [25, 100] status band: only record if followed by a transition
            pass
        if ob.level_nominal <= 0.0:
            if seen_zero:
                continue
            seen_zero = True

        if plateaus and abs(plateaus[-1].level_nominal - ob.level_nominal) < 1e-4 and plateaus[-1].level_kind == ob.level_kind:
            last = plateaus[-1]
            plateaus[-1] = CompressedPlateau(
                level_nominal=last.level_nominal,
                level_lo=min(last.level_lo, ob.level_lo),
                level_hi=max(last.level_hi, ob.level_hi),
                first_counter=min(last.first_counter, ob.usage_counter),
                last_counter=max(last.last_counter, ob.usage_counter),
                poll_count=last.poll_count + 1,
                level_kind=last.level_kind,
                provenance=last.provenance,
                first_observed_at=last.first_observed_at,
                last_observed_at=ob.observed_at or last.last_observed_at,
            )
        else:
            plateaus.append(
                CompressedPlateau(
                    level_nominal=ob.level_nominal,
                    level_lo=ob.level_lo,
                    level_hi=ob.level_hi,
                    first_counter=ob.usage_counter,
                    last_counter=ob.usage_counter,
                    poll_count=1,
                    level_kind=ob.level_kind,
                    provenance=ob.provenance,
                    first_observed_at=ob.observed_at,
                    last_observed_at=ob.observed_at,
                )
            )

    # Filter out status_code wide [25, 100] plateaus unless adjacent to a lower band transition
    if any(p.level_kind == "status_code" for p in plateaus):
        filtered: List[CompressedPlateau] = []
        for idx, p in enumerate(plateaus):
            if p.level_kind == "status_code" and p.level_hi - p.level_lo > 50.0:
                if idx + 1 < len(plateaus) and plateaus[idx + 1].level_hi <= 35.0:
                    # Keep ONLY the last counter right before entering the low-toner band (~20-25%)
                    filtered.append(
                        CompressedPlateau(
                            level_nominal=25.0,
                            level_lo=20.0,
                            level_hi=30.0,
                            first_counter=p.last_counter,
                            last_counter=p.last_counter,
                            poll_count=1,
                            level_kind="status_transition",
                            provenance=p.provenance,
                            first_observed_at=p.last_observed_at,
                            last_observed_at=p.last_observed_at,
                        )
                    )
                continue
            filtered.append(p)
        plateaus = filtered

    # Widen intervals automatically when a coarse-step quantization grid (5%/10%/20%/25%) is detected
    coarse_grid = _infer_coarse_step_pct(plateaus)
    if coarse_grid > 1.0:
        half = 0.5 * coarse_grid
        plateaus = [
            CompressedPlateau(
                level_nominal=p.level_nominal,
                level_lo=max(0.0, p.level_nominal - half),
                level_hi=min(100.0, p.level_nominal + half),
                first_counter=p.first_counter,
                last_counter=p.last_counter,
                poll_count=p.poll_count,
                level_kind=p.level_kind,
                provenance=p.provenance,
                first_observed_at=p.first_observed_at,
                last_observed_at=p.last_observed_at,
            )
            for p in plateaus
        ]
    return plateaus


# =============================================================================
# Layer 3 — Pure Interval-Censored Huber Estimator (No DB / No Side Effects)
# =============================================================================

@dataclass(frozen=True)
class SegmentEstimate:
    """Statistical output of fit_segment() on a single consumption segment."""

    yield_pages: Optional[float]
    sigma_yield: Optional[float]
    rate_pct_per_page: Optional[float]
    sigma_rate: Optional[float]
    ci95_lo: Optional[float]
    ci95_hi: Optional[float]
    rel_ci95: Optional[float]
    p0_counter: Optional[int]
    l0_pct: Optional[float]
    n_plateaus: int
    n_polls: int
    pages_span: int
    drop_pct: float
    residual_scale: float
    converged: bool
    suspect: bool
    suspect_reason: Optional[str] = None


def _weighted_median(pairs: Sequence[Tuple[float, float]]) -> float:
    pts = [(float(v), max(1e-9, float(w))) for v, w in pairs]
    pts.sort(key=lambda x: x[0])
    if len(pts) == 1:
        return pts[0][0]
    tot = sum(w for _, w in pts)
    half = 0.5 * tot
    acc = 0.0
    centers = []
    for v, w in pts:
        centers.append((acc + 0.5 * w, v))
        acc += w
    if half <= centers[0][0]:
        return centers[0][1]
    if half >= centers[-1][0]:
        return centers[-1][1]
    for i in range(len(centers) - 1):
        c0, v0 = centers[i]
        c1, v1 = centers[i + 1]
        if c0 <= half <= c1:
            return v0 if (c1 - c0) <= 1e-12 else v0 + ((half - c0) / (c1 - c0)) * (v1 - v0)
    return centers[-1][1]


def _t_critical_95(n_eff: int) -> float:
    """Calibrated 95% multiplier accounting for small-plateau finite-sample uncertainty."""
    if n_eff <= 2:
        return 2.15
    if n_eff == 3:
        return 2.06
    if n_eff == 4:
        return 2.01
    return 1.96


def fit_segment(
    readings_or_plateaus: Sequence,
    *,
    spec: Optional[BrandSpec] = None,
    huber_k: float = 1.345,
    process_sigma_pct: float = 0.20,
) -> SegmentEstimate:
    """Fit interval-censored linear consumption model l(P) = l0 - r * (P - P_ref), Y = 100 / r.

    Accepts either raw `SupplyReading`s, `IntervalObservation`s, or `CompressedPlateau`s.
    Pure function: zero database, network, or global state access.
    """
    if not readings_or_plateaus:
        return SegmentEstimate(
            yield_pages=None, sigma_yield=None, rate_pct_per_page=None, sigma_rate=None,
            ci95_lo=None, ci95_hi=None, rel_ci95=None, p0_counter=None, l0_pct=None,
            n_plateaus=0, n_polls=0, pages_span=0, drop_pct=0.0,
            residual_scale=0.0, converged=False, suspect=False,
        )

    first_item = readings_or_plateaus[0]
    if isinstance(first_item, CompressedPlateau):
        plateaus = list(readings_or_plateaus)
    elif isinstance(first_item, IntervalObservation):
        plateaus = compress_plateaus(_filter_transient_blips(list(readings_or_plateaus)))
    else:
        obs = [iv for r in readings_or_plateaus if (iv := reading_to_interval(r, spec=spec)) is not None]
        plateaus = compress_plateaus(_filter_transient_blips(obs))

    n_plateaus = len(plateaus)
    n_polls = sum(p.poll_count for p in plateaus)
    if n_plateaus == 0:
        return SegmentEstimate(
            yield_pages=None, sigma_yield=None, rate_pct_per_page=None, sigma_rate=None,
            ci95_lo=None, ci95_hi=None, rel_ci95=None, p0_counter=None, l0_pct=None,
            n_plateaus=0, n_polls=0, pages_span=0, drop_pct=0.0,
            residual_scale=0.0, converged=False, suspect=False,
        )

    p_ref = plateaus[0].first_counter
    pages_span = max(0, plateaus[-1].last_counter - plateaus[0].first_counter)
    drop_pct = plateaus[0].level_nominal - plateaus[-1].level_nominal

    if n_plateaus < 2 or pages_span < 10 or drop_pct <= 0.0:
        return SegmentEstimate(
            yield_pages=None, sigma_yield=None, rate_pct_per_page=None, sigma_rate=None,
            ci95_lo=None, ci95_hi=None, rel_ci95=None,
            p0_counter=p_ref, l0_pct=plateaus[0].level_nominal,
            n_plateaus=n_plateaus, n_polls=n_polls, pages_span=pages_span,
            drop_pct=max(0.0, drop_pct), residual_scale=0.0,
            converged=False, suspect=False,
        )

    # Build Regression Anchor Nodes from compressed plateaus:
    # When two adjacent plateaus share a quantization boundary AND were polled closely,
    # their midpoint brackets the exact rounding threshold T_m = L_{m,lo} = L_{m+1,hi}!
    # Otherwise, each plateau contributes its midpoint (0.5*(P_first+P_last), L_mid) with uniform box variance h^2/3.
    nodes: List[Tuple[float, float, float, float, float]] = []  # (x = P - p_ref, y_mid, lo, hi, sigma_i)
    prelim_r = max(100.0 / MAX_PHYSICAL_YIELD, min(100.0 / MIN_PHYSICAL_YIELD, drop_pct / max(1.0, float(pages_span))))

    for idx, pl in enumerate(plateaus):
        p_mid = 0.5 * (pl.first_counter + pl.last_counter)
        x_val = float(p_mid - p_ref)
        h_box = max(0.02, 0.5 * (pl.level_hi - pl.level_lo))
        # Uniform quantization variance = h^2 / 3; plus intrinsic coverage process variance
        span_half_pages = 0.5 * (pl.last_counter - pl.first_counter)
        # Wide multi-poll plateau narrows the midpoint quantization error!
        effective_h = max(0.10 * h_box, h_box - 0.35 * min(h_box, prelim_r * span_half_pages)) if pl.poll_count >= 2 else h_box
        sigma_i = math.sqrt((effective_h ** 2) / 3.0 + process_sigma_pct ** 2)
        y_mid = 0.5 * (pl.level_lo + pl.level_hi)
        # Correct asymmetric boundary truncation at 100% and 0%
        if pl.level_nominal >= 100.0 and pl.level_hi == 100.0 and pl.level_lo < 100.0:
            y_mid = 100.0 - 0.35 * (100.0 - pl.level_lo)
        elif pl.level_nominal <= 0.0 and pl.level_lo == 0.0 and pl.level_hi > 0.0:
            y_mid = 0.35 * pl.level_hi
        nodes.append((x_val, y_mid, pl.level_lo, pl.level_hi, sigma_i))

    # Add tightly bracketed boundary crossing constraints when poll gap across adjacent 1%-bins is narrow
    crossing_nodes: List[Tuple[float, float, float, float, float]] = []
    for idx in range(len(plateaus) - 1):
        p_hi = plateaus[idx]
        p_lo = plateaus[idx + 1]
        gap_pages = p_lo.first_counter - p_hi.last_counter
        if gap_pages >= 0 and abs(p_hi.level_lo - p_lo.level_hi) <= 0.05:
            h_cross = 0.5 * prelim_r * gap_pages
            h_orig = 0.5 * (p_hi.level_hi - p_hi.level_lo)
            if h_cross < 0.65 * h_orig:
                t_exact = 0.5 * (p_hi.level_lo + p_lo.level_hi)
                x_cross = 0.5 * (p_hi.last_counter + p_lo.first_counter) - p_ref
                sig_cross = math.sqrt((h_cross ** 2) / 3.0 + (0.85 * process_sigma_pct) ** 2)
                crossing_nodes.append((x_cross, t_exact, t_exact - h_cross, t_exact + h_cross, sig_cross))

    fit_pts = crossing_nodes if len(crossing_nodes) >= max(2, len(nodes) - 1) else nodes
    m_pts = len(fit_pts)

    # Robust initial slope via Theil-Sen pairwise median over fit_pts
    pairwise_slopes: List[Tuple[float, float]] = []
    for i in range(m_pts):
        xi, yi, _, _, _ = fit_pts[i]
        for j in range(i + 1, m_pts):
            xj, yj, _, _, _ = fit_pts[j]
            dx = xj - xi
            if dx > 5.0:
                s_ij = (yi - yj) / dx
                if s_ij > 0:
                    pairwise_slopes.append((s_ij, dx))

    if pairwise_slopes:
        r_est = _weighted_median(pairwise_slopes)
    else:
        r_est = prelim_r
    r_min = 100.0 / MAX_PHYSICAL_YIELD
    r_max = 100.0 / MIN_PHYSICAL_YIELD
    r_est = max(r_min, min(r_max, r_est))
    l0_est = _weighted_median([(yi + r_est * xi, 1.0 / (si * si)) for xi, yi, _, _, si in fit_pts])

    # Iteratively Reweighted Least Squares (IRLS) with Interval-Aware Huber Weights (10 fast iterations)
    huber_weights = [1.0] * m_pts
    for _ in range(10):
        base_weights = []
        for idx, (xi, yi, lo_i, hi_i, si) in enumerate(fit_pts):
            pred = l0_est - r_est * xi
            # Interval-aware standardized residual: inside [lo_i, hi_i] soft curvature, outside Huber linear tail
            raw_res = yi - pred
            box_excess = 0.0
            if pred < lo_i:
                box_excess = lo_i - pred
            elif pred > hi_i:
                box_excess = pred - hi_i
            std_res = abs(raw_res) / max(si, 1e-6)
            if box_excess > 0.0 and std_res > huber_k:
                hw = huber_k / std_res
            elif std_res > 2.0 * huber_k:
                hw = (2.0 * huber_k) / std_res
            else:
                hw = 1.0
            huber_weights[idx] = hw
            base_weights.append(hw / (si * si))

        w_sum = sum(base_weights)
        if w_sum <= 1e-12:
            break
        x_bar = sum(w * fit_pts[i][0] for i, w in enumerate(base_weights)) / w_sum
        y_bar = sum(w * fit_pts[i][1] for i, w in enumerate(base_weights)) / w_sum
        s_xx = sum(w * ((fit_pts[i][0] - x_bar) ** 2) for i, w in enumerate(base_weights))
        s_xy = sum(w * (fit_pts[i][0] - x_bar) * (fit_pts[i][1] - y_bar) for i, w in enumerate(base_weights))
        if s_xx <= 1e-12:
            break
        new_r = -s_xy / s_xx
        new_r = max(r_min, min(r_max, new_r))
        new_l0 = y_bar + new_r * x_bar
        if abs(new_r - r_est) <= 1e-9 * max(r_est, 1e-6) and abs(new_l0 - l0_est) <= 1e-6:
            r_est, l0_est = new_r, new_l0
            break
        r_est, l0_est = new_r, new_l0

    # Variance & Overdispersion Calculation (Sandwich / Quasi-Likelihood + Finite-Span Box Bound)
    eff_weights = [huber_weights[i] / (fit_pts[i][4] ** 2) for i in range(m_pts)]
    w_sum = sum(eff_weights)
    x_bar = sum(eff_weights[i] * fit_pts[i][0] for i in range(m_pts)) / max(w_sum, 1e-12)
    s_xx = sum(eff_weights[i] * ((fit_pts[i][0] - x_bar) ** 2) for i in range(m_pts))

    # Standardized interval-violation & midpoint dispersion
    box_violations = []
    std_sq_sum = 0.0
    for i, (xi, yi, lo_i, hi_i, si) in enumerate(fit_pts):
        pred = l0_est - r_est * xi
        std_sq_sum += huber_weights[i] * (((yi - pred) / max(si, 1e-6)) ** 2)
        if pred < lo_i:
            box_violations.append((lo_i - pred) / max(si, 1e-6))
        elif pred > hi_i:
            box_violations.append((pred - hi_i) / max(si, 1e-6))
        else:
            box_violations.append(0.0)

    df = max(1, m_pts - 2)
    phi = max(1.0, std_sq_sum / df) if m_pts >= 3 else 1.0
    var_r_irls = phi / max(s_xx, 1e-12)

    # Newey-West HAC sandwich variance (captures cumulative random-walk coverage drift across plateaus)
    var_r_hac = 0.0
    if m_pts >= 4 and s_xx > 1e-12:
        scores = [
            eff_weights[i] * (fit_pts[i][0] - x_bar) * (fit_pts[i][1] - (l0_est - r_est * fit_pts[i][0]))
            for i in range(m_pts)
        ]
        max_lag = min(3, max(1, int(round(m_pts ** 0.33))))
        meat = sum(u * u for u in scores)
        for lag in range(1, max_lag + 1):
            bartlett = 1.0 - (lag / float(max_lag + 1))
            meat += 2.0 * bartlett * sum(scores[i] * scores[i + lag] for i in range(m_pts - lag))
        dof_infl = float(m_pts) / float(max(1, m_pts - 2))
        var_r_hac = max(0.0, dof_infl * meat) / (s_xx * s_xx)

    # Non-asymptotic endpoint quantization + disjoint-block Brownian increment dispersion
    h_start = 0.5 * (fit_pts[0][3] - fit_pts[0][2])
    h_end = 0.5 * (fit_pts[-1][3] - fit_pts[-1][2])
    span_x = max(1.0, fit_pts[-1][0] - fit_pts[0][0])
    sigma_r_box_floor = math.sqrt((h_start ** 2 + h_end ** 2) / 3.0) / (span_x * math.sqrt(max(1.0, m_pts / 2.0)))
    sigma_r_quant = math.sqrt(max(var_r_irls, var_r_hac, (0.90 * sigma_r_box_floor) ** 2))

    crit = _t_critical_95(n_plateaus)
    n_blocks = min(6, n_plateaus - 1)
    if n_blocks >= 2:
        b_idxs = [round(k * (n_plateaus - 1) / n_blocks) for k in range(n_blocks + 1)]
        tot_dx = (
            0.5 * (plateaus[-1].first_counter + plateaus[-1].last_counter)
            - 0.5 * (plateaus[0].first_counter + plateaus[0].last_counter)
        )
        if tot_dx > 10.0:
            z_sq = 0.0
            for b in range(n_blocks):
                pa = plateaus[b_idxs[b]]
                pc = plateaus[b_idxs[b + 1]]
                dx_b = 0.5 * (pc.first_counter + pc.last_counter) - 0.5 * (pa.first_counter + pa.last_counter)
                dy_b = 0.5 * (pa.level_lo + pa.level_hi) - 0.5 * (pc.level_lo + pc.level_hi)
                z_sq += (dy_b - r_est * dx_b) ** 2
            var_block = (n_blocks / float(n_blocks - 1)) * z_sq / (tot_dx * tot_dx)
            t_block = 0.80 * {1: 4.30, 2: 3.18, 3: 2.78, 4: 2.57, 5: 2.45}.get(n_blocks - 1, 2.05)
            sigma_r = max(sigma_r_quant, (t_block / crit) * math.sqrt(var_block))
        else:
            sigma_r = sigma_r_quant
    else:
        single_step_drift = (0.08 * r_est) / math.sqrt(max(1.0, drop_pct / 5.0))
        sigma_r = math.sqrt(sigma_r_quant ** 2 + single_step_drift ** 2)

    # Detect broken / erratic sensor (`suspect` state)
    residual_scale = math.sqrt(std_sq_sum / max(1, m_pts))
    severe_violations = sum(1 for v in box_violations if v > 3.5)
    suspect = False
    suspect_reason = None
    if m_pts >= 5 and (residual_scale > 4.5 and severe_violations >= max(2, m_pts // 3)):
        suspect = True
        suspect_reason = "systematic_sensor_nonlinearity_or_broken_gauge"

    yield_hat = 100.0 / r_est
    crit = _t_critical_95(n_plateaus)
    r_lo_95 = max(1e-9, r_est - crit * sigma_r)
    r_hi_95 = r_est + crit * sigma_r
    ci95_lo = max(float(MIN_PHYSICAL_YIELD), 100.0 / r_hi_95)
    ci95_hi = min(float(MAX_PHYSICAL_YIELD), 100.0 / r_lo_95)
    rel_ci95 = (crit * sigma_r) / r_est
    sigma_yield = yield_hat * (sigma_r / r_est)
    p0_est = int(round(p_ref - max(0.0, 100.0 - l0_est) / r_est))
    converged = (not suspect) and (rel_ci95 <= 0.10) and (n_plateaus >= 4) and (drop_pct >= 5.0)

    return SegmentEstimate(
        yield_pages=round(yield_hat, 2),
        sigma_yield=round(sigma_yield, 2),
        rate_pct_per_page=r_est,
        sigma_rate=sigma_r,
        ci95_lo=round(ci95_lo, 1),
        ci95_hi=round(ci95_hi, 1),
        rel_ci95=round(rel_ci95, 4),
        p0_counter=p0_est,
        l0_pct=round(l0_est, 3),
        n_plateaus=n_plateaus,
        n_polls=n_polls,
        pages_span=pages_span,
        drop_pct=round(drop_pct, 3),
        residual_scale=round(residual_scale, 3),
        converged=converged,
        suspect=suspect,
        suspect_reason=suspect_reason,
    )


# =============================================================================
# Layer 4 & Layer 5 — Multi-Segment Pooling, Empirical Bayes, Policy & Presentation
# =============================================================================

@dataclass(frozen=True)
class PosteriorYieldDecision:
    """Final policy & presentation output (Layers 4 & 5)."""

    capacity_pages: int
    capacity_ci95: Tuple[int, int]
    rel_ci95: float
    sigma_pages: float
    confidence: str                       # high | medium | low
    learning_state: str                   # prior | learning | converged | suspect
    capacity_method: str                  # interval_eb | interval_mle | prior_catalog | prior_device | prior_fleet | prior_default
    nominal_capacity_pages: Optional[int]
    coverage_index: Optional[float]
    eta_pages: Optional[int]
    n_segments: int
    n_plateaus: int
    drop_pct: float
    residual_scale: float
    suspect_reason: Optional[str] = None

    def to_api_dict(self) -> Dict[str, object]:
        return {
            "capacity_pages": self.capacity_pages,
            "capacity_ci95": [self.capacity_ci95[0], self.capacity_ci95[1]],
            "capacity_method": self.capacity_method,
            "learning_state": self.learning_state,
            "confidence": self.confidence,
            "rel_ci95": self.rel_ci95,
            "nominal_capacity_pages": self.nominal_capacity_pages,
            "coverage_index": self.coverage_index,
            "eta_pages": self.eta_pages,
        }


def combine_segments(
    segments: Sequence[ConsumptionSegment],
    *,
    spec: Optional[BrandSpec] = None,
) -> Tuple[Optional[SegmentEstimate], int]:
    """Pool valid segment estimates across refill cycles using MAD-filtered inverse-variance fusion."""
    valid_ests: List[SegmentEstimate] = []
    for seg in segments:
        est = fit_segment(seg.observations, spec=spec)
        # If a segment hit 0% and printed reserve pages before refill, incorporate full-cycle reserve rate
        if (
            seg.closed_by_refill
            and seg.zero_reached_counter is not None
            and seg.pages_after_zero > 0
            and seg.refill_counter is not None
            and seg.observations
        ):
            start_p = seg.observations[0].usage_counter
            start_l = max(15.0, min(100.0, seg.observations[0].level_nominal))
            total_p = seg.refill_counter - start_p
            if total_p >= 20:
                full_y = (total_p * 100.0) / start_l
                if MIN_PHYSICAL_YIELD <= full_y <= MAX_PHYSICAL_YIELD:
                    r_full = 100.0 / full_y
                    sig_r = r_full * max(0.03, 1.5 / start_l)
                    est = SegmentEstimate(
                        yield_pages=round(full_y, 2),
                        sigma_yield=round(full_y * (sig_r / r_full), 2),
                        rate_pct_per_page=r_full,
                        sigma_rate=sig_r,
                        ci95_lo=round(100.0 / (r_full + 1.96 * sig_r), 1),
                        ci95_hi=round(100.0 / max(1e-9, r_full - 1.96 * sig_r), 1),
                        rel_ci95=round((1.96 * sig_r) / r_full, 4),
                        p0_counter=start_p,
                        l0_pct=start_l,
                        n_plateaus=max(est.n_plateaus, 5),
                        n_polls=est.n_polls,
                        pages_span=total_p,
                        drop_pct=start_l,
                        residual_scale=est.residual_scale,
                        converged=True,
                        suspect=False,
                    )
        if est.rate_pct_per_page is not None and est.sigma_rate is not None:
            valid_ests.append(est)

    if not valid_ests:
        return None, 0
    if len(valid_ests) == 1:
        return valid_ests[0], 1

    # If the latest active segment is flagged suspect, surface it directly
    if valid_ests[-1].suspect and valid_ests[-1].n_plateaus >= 5:
        return valid_ests[-1], len(valid_ests)

    clean_ests = [e for e in valid_ests if not e.suspect]
    if not clean_ests:
        return valid_ests[-1], len(valid_ests)
    if len(clean_ests) == 1:
        return clean_ests[0], 1

    # Keep only prior segments that are statistically compatible with the active cartridge SKU;
    # if the user swapped from Standard to XL (or vice versa), do not contaminate the active SKU!
    active_est = clean_ests[-1]
    inliers = []
    for e in clean_ests[:-1]:
        compat_tol = 2.2 * math.sqrt(
            (e.sigma_rate ** 2) + (active_est.sigma_rate ** 2) + ((0.08 * active_est.rate_pct_per_page) ** 2)
        )
        if abs(e.rate_pct_per_page - active_est.rate_pct_per_page) <= compat_tol:
            inliers.append(e)
    inliers.append(active_est)
    if len(inliers) == 1:
        return active_est, 1

    # Inverse-variance precision pooling (including 3% natural inter-cartridge variance)
    prec_sum = 0.0
    rate_num = 0.0
    tot_plateaus = 0
    tot_polls = 0
    tot_pages = 0
    tot_drop = 0.0
    max_res = 0.0
    for idx, e in enumerate(inliers):
        # Slight recency preference for most recent cartridges
        recency = 0.85 ** (len(inliers) - 1 - idx)
        var_i = (e.sigma_rate ** 2) + ((0.03 * e.rate_pct_per_page) ** 2)
        w_i = recency / max(var_i, 1e-14)
        prec_sum += w_i
        rate_num += w_i * e.rate_pct_per_page
        tot_plateaus += e.n_plateaus
        tot_polls += e.n_polls
        tot_pages += e.pages_span
        tot_drop += e.drop_pct
        max_res = max(max_res, e.residual_scale)

    r_pool = rate_num / max(prec_sum, 1e-14)
    sigma_r_pool = 1.0 / math.sqrt(max(prec_sum, 1e-14))
    y_pool = 100.0 / r_pool
    crit = _t_critical_95(tot_plateaus)
    ci_lo = max(float(MIN_PHYSICAL_YIELD), 100.0 / (r_pool + crit * sigma_r_pool))
    ci_hi = min(float(MAX_PHYSICAL_YIELD), 100.0 / max(1e-9, r_pool - crit * sigma_r_pool))
    rel_ci = (crit * sigma_r_pool) / r_pool
    latest = inliers[-1]
    return (
        SegmentEstimate(
            yield_pages=round(y_pool, 2),
            sigma_yield=round(y_pool * (sigma_r_pool / r_pool), 2),
            rate_pct_per_page=r_pool,
            sigma_rate=sigma_r_pool,
            ci95_lo=round(ci_lo, 1),
            ci95_hi=round(ci_hi, 1),
            rel_ci95=round(rel_ci, 4),
            p0_counter=latest.p0_counter,
            l0_pct=latest.l0_pct,
            n_plateaus=tot_plateaus,
            n_polls=tot_polls,
            pages_span=tot_pages,
            drop_pct=round(tot_drop, 3),
            residual_scale=round(max_res, 3),
            converged=(rel_ci <= 0.10 and tot_plateaus >= 4 and tot_drop >= 5.0),
            suspect=False,
        ),
        len(inliers),
    )


def derive_ci_confidence(rel_ci95: float, *, counter_basis: str = "") -> str:
    """Derive confidence directly from relative 95% CI half-width (Layer 4 rule)."""
    if _finite_number(rel_ci95) is None or rel_ci95 < 0:
        return "low"
    if rel_ci95 <= 0.10 and not str(counter_basis or "").startswith("total_for_color"):
        return "high"
    if rel_ci95 <= 0.25:
        return "medium"
    return "low"


def combine_with_prior(
    seg_est: Optional[SegmentEstimate],
    *,
    prior_yield: Optional[int] = None,
    prior_source: str = "default",
    prior_cv: Optional[float] = None,
    nominal_capacity: Optional[int] = None,
    current_level: Optional[float] = None,
    counter_basis: str = "",
    n_segments: int = 1,
    spec: Optional[BrandSpec] = None,
) -> PosteriorYieldDecision:
    """Fuse interval likelihood estimate with hierarchical/catalog prior via Empirical Bayes."""
    spec = spec or DEFAULT_BRAND_SPEC
    # Learned values from this very series are not independent Bayes priors.
    has_real_prior = bool(prior_yield and prior_yield > 0
                          and prior_source in ("catalog", "device_capacity", "shared_profile"))
    eff_prior_y = float(prior_yield) if has_real_prior else float(DEFAULT_PRIOR_YIELD)
    base_cv = prior_cv if (prior_cv and prior_cv > 0) else (
        0.16 if prior_source == "shared_profile" else (spec.prior_cv if has_real_prior else 0.65)
    )
    nom_cap = int(nominal_capacity) if (nominal_capacity and nominal_capacity > 0) else (
        int(prior_yield) if has_real_prior and prior_source in ("catalog", "device_capacity") else None
    )

    def _calc_eta(cap: int) -> Optional[int]:
        if current_level is None:
            return None
        try:
            lv = max(0.0, min(100.0, float(current_level)))
            return int(round((lv / 100.0) * cap))
        except (TypeError, ValueError):
            return None

    method_map = {
        "catalog": "prior_catalog",
        "device_capacity": "prior_device",
        "shared_profile": "prior_fleet",
    }

    # Case 1: No informative consumption drop yet -> pure Prior state
    if seg_est is None or seg_est.rate_pct_per_page is None or seg_est.sigma_rate is None:
        cap_i = int(round(eff_prior_y))
        r0 = 100.0 / eff_prior_y
        sig0 = r0 * base_cv
        lo_i = max(MIN_PHYSICAL_YIELD, int(round(100.0 / (r0 + 1.96 * sig0))))
        hi_i = min(MAX_PHYSICAL_YIELD, int(round(100.0 / max(1e-9, r0 - 1.96 * sig0))))
        rel_ci = round(1.96 * base_cv, 4)
        conf = derive_ci_confidence(rel_ci, counter_basis=counter_basis) if has_real_prior else "low"
        if conf == "high":
            conf = "medium"  # a prior alone is not locally converged consumption evidence
        return PosteriorYieldDecision(
            capacity_pages=cap_i,
            capacity_ci95=(lo_i, hi_i),
            rel_ci95=rel_ci,
            sigma_pages=round(cap_i * base_cv, 1),
            confidence=conf,
            learning_state="prior",
            capacity_method=method_map.get(prior_source, "prior_default"),
            nominal_capacity_pages=nom_cap,
            coverage_index=1.0 if nom_cap else None,
            eta_pages=_calc_eta(cap_i),
            n_segments=0,
            n_plateaus=seg_est.n_plateaus if seg_est else 0,
            drop_pct=seg_est.drop_pct if seg_est else 0.0,
            residual_scale=0.0,
        )

    # Case 2: Broken / erratic sensor detected (`suspect` state)
    if seg_est.suspect:
        cap_i = int(round(eff_prior_y if has_real_prior else (seg_est.yield_pages or eff_prior_y)))
        r_s = 100.0 / max(1.0, float(cap_i))
        sig_s = r_s * max(base_cv, 0.35)
        lo_i = max(MIN_PHYSICAL_YIELD, int(round(100.0 / (r_s + 1.96 * sig_s))))
        hi_i = min(MAX_PHYSICAL_YIELD, int(round(100.0 / max(1e-9, r_s - 1.96 * sig_s))))
        return PosteriorYieldDecision(
            capacity_pages=cap_i,
            capacity_ci95=(lo_i, hi_i),
            rel_ci95=round((1.96 * sig_s) / r_s, 4),
            sigma_pages=round(cap_i * (sig_s / r_s), 1),
            confidence="low",
            learning_state="suspect",
            capacity_method=method_map.get(prior_source, "interval_suspect"),
            nominal_capacity_pages=nom_cap,
            coverage_index=None,
            eta_pages=_calc_eta(cap_i),
            n_segments=n_segments,
            n_plateaus=seg_est.n_plateaus,
            drop_pct=seg_est.drop_pct,
            residual_scale=seg_est.residual_scale,
            suspect_reason=seg_est.suspect_reason,
        )

    # Case 3: Empirical Bayes Precision Fusion in Rate Space r = 100 / Y
    r_data = seg_est.rate_pct_per_page
    sig_data = seg_est.sigma_rate
    if counter_basis == "total_for_color":
        # Shared total counter on CMY without color counter carries higher structural uncertainty
        sig_data = sig_data * 1.6

    if has_real_prior:
        y_data = 100.0 / r_data
        # If strong multi-plateau low-CI evidence proves a different cartridge regime (e.g. XL or refilled),
        # widen the prior variance so Empirical Bayes adapts instead of pulling back toward mismatched rating.
        regime_shifted = (
            seg_est.n_plateaus >= 6
            and seg_est.drop_pct >= 25.0
            and (seg_est.rel_ci95 or 1.0) <= 0.18
            and abs(y_data - eff_prior_y) > 0.40 * eff_prior_y
        )
        eff_cv = 0.85 if regime_shifted else base_cv
        r_prior = 100.0 / eff_prior_y
        tau_r = r_prior * eff_cv
        w_data = 1.0 / max(sig_data ** 2, 1e-14)
        w_prior = 1.0 / max(tau_r ** 2, 1e-14)
        r_post = (w_data * r_data + w_prior * r_prior) / (w_data + w_prior)
        sig_post = 1.0 / math.sqrt(w_data + w_prior)
        method = "interval_eb"
    else:
        r_post = r_data
        sig_post = sig_data
        method = "interval_mle"

    y_post = max(float(MIN_PHYSICAL_YIELD), min(float(MAX_PHYSICAL_YIELD), 100.0 / r_post))
    cap_i = int(round(y_post))
    crit = _t_critical_95(seg_est.n_plateaus)
    lo_i = max(MIN_PHYSICAL_YIELD, int(round(100.0 / (r_post + crit * sig_post))))
    hi_i = min(MAX_PHYSICAL_YIELD, int(round(100.0 / max(1e-9, r_post - crit * sig_post))))
    rel_ci = round((crit * sig_post) / r_post, 4)
    conf = derive_ci_confidence(rel_ci, counter_basis=counter_basis)
    l_state = "converged" if (rel_ci <= 0.10 and seg_est.drop_pct >= 5.0 and seg_est.n_plateaus >= 4) else "learning"

    cov_idx = None
    if nom_cap and cap_i > 0:
        cov_idx = round(max(0.15, min(6.0, float(nom_cap) / float(cap_i))), 3)

    return PosteriorYieldDecision(
        capacity_pages=cap_i,
        capacity_ci95=(lo_i, hi_i),
        rel_ci95=rel_ci,
        sigma_pages=round(y_post * (sig_post / r_post), 1),
        confidence=conf,
        learning_state=l_state,
        capacity_method=method,
        nominal_capacity_pages=nom_cap,
        coverage_index=cov_idx,
        eta_pages=_calc_eta(cap_i),
        n_segments=n_segments,
        n_plateaus=seg_est.n_plateaus,
        drop_pct=seg_est.drop_pct,
        residual_scale=seg_est.residual_scale,
    )


def estimate_supply_series(
    readings: Sequence[SupplyReading],
    *,
    vendor_or_model: str = "",
    prior_yield: Optional[int] = None,
    prior_source: str = "default",
    nominal_capacity: Optional[int] = None,
    current_level: Optional[float] = None,
    counter_basis: str = "",
) -> PosteriorYieldDecision:
    """End-to-end pure pipeline: SupplyReadings -> Breakpoints -> Plateaus -> Interval Fit -> Posterior."""
    spec = get_brand_spec(vendor_or_model)
    segments = segment_readings(readings, spec=spec)
    pooled_est, n_seg = combine_segments(segments, spec=spec)
    if current_level is None and segments and segments[-1].observations:
        current_level = segments[-1].observations[-1].level_nominal
    return combine_with_prior(
        pooled_est,
        prior_yield=prior_yield,
        prior_source=prior_source,
        nominal_capacity=nominal_capacity,
        current_level=current_level,
        counter_basis=counter_basis,
        n_segments=n_seg,
        spec=spec,
    )

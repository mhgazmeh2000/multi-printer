#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/yield_backtest.py
# @brief:        Offline replay & Monte Carlo calibration backtest for v2 vs v1 yield estimators.
# @details:      Supports two modes:
#                  1. Database replay (`--db logs.db` or live DB): replays `toner_snapshots_v2`
#                     trajectories per (printer_id, color) and compares v1 pointwise vs v2 interval.
#                  2. Synthetic multi-brand fleet benchmark (`--synthetic` / `--verify-gates`):
#                     evaluates all 10 printer brands under quantization (1%/5%/10%/20%), idle polls,
#                     Toshiba blips, Canon status bands, mid-level swaps, and 0%-reserve printing.
# =============================================================================

"""Backtest and CI95 calibration harness for `core.yield_estimator` (v2) vs legacy v1."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.yield_estimator import (  # noqa: E402
    reading_to_interval,
    snapshot_is_usable,
    BRAND_SPECS,
    MAX_PHYSICAL_YIELD,
    MIN_PHYSICAL_YIELD,
    SupplyReading,
    estimate_supply_series,
    fit_segment,
    get_brand_spec,
    segment_readings,
)


def legacy_v1_pointwise_estimate(
    readings: Sequence[SupplyReading],
    *,
    prior_yield: Optional[int] = None,
) -> Tuple[int, str]:
    """Replicate legacy v1 naive pointwise estimator (no plateau compression, 15% refill only)."""
    fallback = int(prior_yield) if (prior_yield and prior_yield > 0) else 2000
    pts = [
        (int(r.usage_counter), float(r.raw_level))
        for r in readings
        if r.raw_level is not None and 0.0 <= float(r.raw_level) <= 100.0 and r.level_kind != "synthetic"
    ]
    if len(pts) < 2:
        return fallback, "low"

    # Legacy v1 splits only on >= 15% single-step rise (no blip filtering, no mid-level swap detection)
    active: List[Tuple[int, float]] = [pts[0]]
    for u, lv in pts[1:]:
        if u < active[-1][0] or (lv - active[-1][1]) >= 15.0:
            active = [(u, lv)]
        else:
            active.append((u, lv))

    if len(active) < 2:
        return fallback, "low"

    # Pairwise endpoint + step slopes without plateau deduplication or post-zero clipping
    raw_yields: List[float] = []
    p0, l0 = active[0]
    for pk, lk in active[1:]:
        dp = pk - p0
        dl = l0 - lk
        if dp >= 10 and dl >= 2.0:
            y_k = (dp * 100.0) / dl
            if MIN_PHYSICAL_YIELD <= y_k <= MAX_PHYSICAL_YIELD:
                raw_yields.append(y_k)

    if not raw_yields:
        return fallback, "low"

    med_y = statistics.median(raw_yields)
    # Legacy v1 confidence depends purely on raw poll count (`len(active)`), fooled by idle polls!
    total_drop = active[0][1] - active[-1][1]
    if len(active) >= 20 and total_drop >= 10.0:
        conf = "high"
    elif len(active) >= 5 and total_drop >= 5.0:
        conf = "medium"
    else:
        conf = "low"
    return int(round(med_y)), conf


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = max(0.0, min(float(len(sorted_vals) - 1), q * (len(sorted_vals) - 1)))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    return float(sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo]))


def run_synthetic_benchmark(trials_per_brand: int = 80, seed: int = 20261107) -> Dict[str, object]:
    """Run controlled 10-brand Monte Carlo benchmark with realistic anomalies and quantization."""
    rng = random.Random(seed)
    brand_configs = [
        ("hp", 1.0, 10000, "fine_1pct"),
        ("toshiba", 1.0, 28000, "blips_and_reserve"),
        ("canon", 1.0, 36000, "status_and_1pct"),
        ("brother", 5.0, 3000, "coarse_5pct_midswap"),
        ("kyocera", 10.0, 20000, "coarse_10pct_idle"),
        ("ricoh", 10.0, 24000, "coarse_10pct"),
        ("xerox", 1.0, 15000, "capacity_units"),
        ("lexmark", 5.0, 8500, "coarse_5pct"),
        ("samsung", 1.0, 5000, "reserve_zero_clip"),
        ("epson", 20.0, 6000, "coarse_20pct"),
    ]

    per_brand: Dict[str, Dict[str, object]] = {}
    all_v1_ape: List[float] = []
    all_v2_ape: List[float] = []
    all_v2_mle_cov: List[int] = []
    all_v2_eb_cov: List[int] = []
    false_high_v1 = 0
    false_high_v2 = 0
    total_cases = 0

    for brand, step_pct, nom_yield, mode in brand_configs:
        b_v1_ape: List[float] = []
        b_v2_ape: List[float] = []
        b_cov_mle: List[int] = []
        b_cov_eb: List[int] = []
        b_conv_drops: List[float] = []

        for t_idx in range(trials_per_brand):
            # Realized yield differs from ISO 5% nominal due to office document coverage (0.68x .. 1.38x)
            cov_mult = rng.uniform(0.68, 1.38)
            true_yield = max(600.0, nom_yield * cov_mult)
            p_counter = rng.randint(5000, 150000)
            readings: List[SupplyReading] = []

            if mode == "coarse_5pct_midswap" and t_idx % 2 == 0:
                # Unannounced mid-level swap (old 4000-page cartridge swapped at 55% -> 80% new cartridge)
                old_y = true_yield * 0.45
                lv_old = 85.0
                for _ in range(18):
                    dp = rng.randint(10, max(20, int(old_y * 0.025)))
                    p_counter += dp
                    lv_old -= (dp / old_y) * 100.0
                    if lv_old <= 55.0:
                        break
                    q = round(lv_old / step_pct) * step_pct
                    readings.append(SupplyReading(usage_counter=p_counter, raw_level=q, vendor=brand, max_capacity=100))
                start_lv = 80.0
            else:
                start_lv = rng.uniform(75.0, 100.0)

            target_drop = rng.uniform(max(step_pct + 4.0, 18.0), 48.0)
            if mode == "reserve_zero_clip" and t_idx % 2 == 0:
                start_lv = 35.0
                target_drop = 35.0

            cur_lv = start_lv
            first_converged_drop: Optional[float] = None
            poll_idx = 0
            while (start_lv - cur_lv) < target_drop:
                poll_idx += 1
                # Weekend / night idle polls (especially heavy on Kyocera & office fleets)
                idle_prob = 0.75 if mode == "coarse_10pct_idle" else 0.45
                dp = 0 if rng.random() < idle_prob else rng.randint(3, max(12, int(true_yield * 0.012)))
                p_counter += dp
                job_factor = max(0.35, rng.gauss(1.0, 0.16))
                cur_lv -= (dp / true_yield) * 100.0 * job_factor

                if cur_lv <= 0.0:
                    cur_lv = 0.0
                    readings.append(SupplyReading(usage_counter=p_counter, raw_level=0.0, vendor=brand, max_capacity=100))
                    if mode in ("reserve_zero_clip", "blips_and_reserve"):
                        # Printer keeps printing 8-15% extra pages on reserve while pinned at 0%!
                        reserve_pages = int(true_yield * rng.uniform(0.08, 0.16))
                        for _ in range(8):
                            p_counter += max(10, reserve_pages // 8)
                            readings.append(SupplyReading(usage_counter=p_counter, raw_level=0.0, vendor=brand, max_capacity=100))
                    break

                if mode == "blips_and_reserve" and poll_idx == 7 and cur_lv > 40.0:
                    # Inject Toshiba single-poll downward SNMP read blip (e.g. 78% -> 6% -> 77%)
                    readings.append(SupplyReading(usage_counter=p_counter, raw_level=6.0, vendor=brand, max_capacity=100))
                    p_counter += 4

                if mode == "status_and_1pct" and t_idx % 3 == 0:
                    # Canon status-coded trajectory (OK -> Toner Low at 20% -> Replace at 0%)
                    st = "OK" if cur_lv > 20.0 else ("Toner Low" if cur_lv > 2.0 else "Replace Toner")
                    raw_v = -3.0 if cur_lv > 2.0 else 0.0
                    readings.append(SupplyReading(usage_counter=p_counter, raw_level=raw_v, vendor=brand, status_text=st))
                elif mode == "capacity_units":
                    raw_units = round((cur_lv / 100.0) * nom_yield)
                    readings.append(
                        SupplyReading(
                            usage_counter=p_counter,
                            raw_level=float(raw_units),
                            level_kind="capacity_units",
                            max_capacity=nom_yield,
                            unit_code=8,
                            vendor=brand,
                        )
                    )
                else:
                    q_lv = max(0.0, min(100.0, round(cur_lv / step_pct) * step_pct))
                    readings.append(SupplyReading(usage_counter=p_counter, raw_level=q_lv, vendor=brand, max_capacity=100))

                if first_converged_drop is None and len(readings) >= 5 and poll_idx % 4 == 0:
                    seg_now = fit_segment(readings[-25:], spec=get_brand_spec(brand))
                    if seg_now.converged:
                        first_converged_drop = start_lv - cur_lv

            if len(readings) < 3:
                continue

            v1_y, v1_conf = legacy_v1_pointwise_estimate(readings, prior_yield=nom_yield)
            v2_dec = estimate_supply_series(
                readings,
                vendor_or_model=brand,
                prior_yield=nom_yield,
                prior_source="catalog",
            )
            segs = segment_readings(readings, spec=get_brand_spec(brand))
            mle_seg = fit_segment(segs[-1].observations, spec=get_brand_spec(brand)) if segs else None

            ape_v1 = abs(v1_y - true_yield) / true_yield
            ape_v2 = abs(v2_dec.capacity_pages - true_yield) / true_yield
            b_v1_ape.append(ape_v1)
            b_v2_ape.append(ape_v2)
            all_v1_ape.append(ape_v1)
            all_v2_ape.append(ape_v2)
            total_cases += 1

            if v1_conf == "high" and ape_v1 > 0.15:
                false_high_v1 += 1
            if v2_dec.confidence == "high" and ape_v2 > 0.15:
                false_high_v2 += 1

            eb_hit = 1 if (v2_dec.capacity_ci95[0] <= true_yield <= v2_dec.capacity_ci95[1]) else 0
            b_cov_eb.append(eb_hit)
            all_v2_eb_cov.append(eb_hit)

            if mle_seg and mle_seg.ci95_lo is not None and mle_seg.ci95_hi is not None:
                mle_hit = 1 if (mle_seg.ci95_lo <= true_yield <= mle_seg.ci95_hi) else 0
                b_cov_mle.append(mle_hit)
                all_v2_mle_cov.append(mle_hit)

            if first_converged_drop is not None:
                b_conv_drops.append(first_converged_drop)

        s_v1 = sorted(b_v1_ape)
        s_v2 = sorted(b_v2_ape)
        per_brand[brand] = {
            "trials": len(b_v2_ape),
            "mode": mode,
            "v1_mape_pct": round(100.0 * statistics.mean(b_v1_ape), 2),
            "v2_mape_pct": round(100.0 * statistics.mean(b_v2_ape), 2),
            "v1_med_ape_pct": round(100.0 * _percentile(s_v1, 0.50), 2),
            "v2_med_ape_pct": round(100.0 * _percentile(s_v2, 0.50), 2),
            "v1_p90_ape_pct": round(100.0 * _percentile(s_v1, 0.90), 2),
            "v2_p90_ape_pct": round(100.0 * _percentile(s_v2, 0.90), 2),
            "v2_mle_ci95_coverage_pct": round(100.0 * statistics.mean(b_cov_mle), 1) if b_cov_mle else None,
            "v2_eb_ci95_coverage_pct": round(100.0 * statistics.mean(b_cov_eb), 1) if b_cov_eb else None,
            "median_drop_to_converge_pct": round(statistics.median(b_conv_drops), 1) if b_conv_drops else None,
        }

    # Verify exact mathematical invariance to 800 idle polls
    sparse_r = [
        SupplyReading(usage_counter=1000, raw_level=90, vendor="kyocera"),
        SupplyReading(usage_counter=3000, raw_level=80, vendor="kyocera"),
        SupplyReading(usage_counter=5000, raw_level=70, vendor="kyocera"),
        SupplyReading(usage_counter=7000, raw_level=60, vendor="kyocera"),
    ]
    dense_r = [
        SupplyReading(usage_counter=u, raw_level=lv, vendor="kyocera")
        for u, lv in [(1000, 90), (3000, 80), (5000, 70), (7000, 60)]
        for _ in range(200)
    ]
    est_sparse = fit_segment(sparse_r, spec=get_brand_spec("kyocera"))
    est_dense = fit_segment(dense_r, spec=get_brand_spec("kyocera"))
    idle_diff = abs((est_sparse.yield_pages or 0.0) - (est_dense.yield_pages or 0.0)) + abs(
        (est_sparse.rel_ci95 or 0.0) - (est_dense.rel_ci95 or 0.0)
    )

    s_all_v1 = sorted(all_v1_ape)
    s_all_v2 = sorted(all_v2_ape)
    v1_mape = 100.0 * statistics.mean(all_v1_ape)
    v2_mape = 100.0 * statistics.mean(all_v2_ape)
    mape_rel_reduction = ((v1_mape - v2_mape) / v1_mape) * 100.0 if v1_mape > 0 else 0.0
    mle_cov = 100.0 * statistics.mean(all_v2_mle_cov)

    return {
        "mode": "synthetic_10brand_fleet",
        "total_trials": total_cases,
        "fleet_summary": {
            "v1_mape_pct": round(v1_mape, 2),
            "v2_mape_pct": round(v2_mape, 2),
            "mape_relative_reduction_pct": round(mape_rel_reduction, 1),
            "v1_median_ape_pct": round(100.0 * _percentile(s_all_v1, 0.50), 2),
            "v2_median_ape_pct": round(100.0 * _percentile(s_all_v2, 0.50), 2),
            "v1_p90_ape_pct": round(100.0 * _percentile(s_all_v1, 0.90), 2),
            "v2_p90_ape_pct": round(100.0 * _percentile(s_all_v2, 0.90), 2),
            "v2_mle_ci95_coverage_pct": round(mle_cov, 2),
            "v2_eb_ci95_coverage_pct": round(100.0 * statistics.mean(all_v2_eb_cov), 2),
            "v1_false_high_confidence_rate_pct": round(100.0 * false_high_v1 / max(1, total_cases), 2),
            "v2_false_high_confidence_rate_pct": round(100.0 * false_high_v2 / max(1, total_cases), 2),
            "idle_poll_invariance_diff": round(idle_diff, 8),
        },
        "per_brand": per_brand,
    }


def run_db_replay(db_path: Optional[str] = None) -> Dict[str, object]:
    """Replay historical `toner_snapshots_v2` from configured DB (PostgreSQL/SQLite) or a SQLite file."""
    state_lookup: Dict[Tuple[str, str], Dict[str, object]] = {}
    grouped: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    id_col = "printer_ip"
    ts_col = "timestamp"
    label = db_path or "configured_db"

    if db_path:
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database file not found: {db_path}")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "toner_snapshots_v2" not in tables:
                return {"mode": "db_replay", "db_path": label, "series_count": 0, "note": "toner_snapshots_v2 not found"}

            snap_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(toner_snapshots_v2)").fetchall()
            }
            id_col = "printer_ip" if "printer_ip" in snap_cols else "printer_id"
            ts_col = "timestamp" if "timestamp" in snap_cols else "captured_at"

            if "cartridge_state" in tables:
                st_cols = {r[1] for r in conn.execute("PRAGMA table_info(cartridge_state)").fetchall()}
                st_id = "printer_ip" if "printer_ip" in st_cols else "printer_id"
                for row in conn.execute("SELECT * FROM cartridge_state").fetchall():
                    d = dict(row)
                    state_lookup[(str(d.get(st_id) or ""), str(d.get("color") or ""))] = d

            for r in conn.execute(f"SELECT * FROM toner_snapshots_v2 ORDER BY {id_col}, color, id ASC").fetchall():
                d = dict(r)
                grouped[(str(d.get(id_col) or ""), str(d.get("color") or ""))].append(d)
        finally:
            conn.close()
    else:
        from core.database import db_connection

        with db_connection() as conn:
            label = f"{getattr(conn, 'backend', 'db')}_live"
            st_rows = conn.execute(
                """
                SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
                       yield_per_page, yield_source, confidence
                FROM cartridge_state
                """
            ).fetchall()
            for r in st_rows:
                state_lookup[(str(r[0] or ""), str(r[1] or ""))] = {
                    "printer_ip": r[0],
                    "color": r[1],
                    "printer_model": r[2],
                    "cartridge_name": r[3],
                    "cartridge_key": r[4],
                    "yield_per_page": r[5],
                    "yield_source": r[6],
                    "confidence": r[7],
                }

            sn_rows = conn.execute(
                """
                SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
                       timestamp, print_total, full_color, black_white, usage_counter,
                       toner_level, raw_level, source, valid, reject_reason
                FROM toner_snapshots_v2
                ORDER BY printer_ip, color, id ASC
                """
            ).fetchall()
            for r in sn_rows:
                d = {
                    "printer_ip": r[0],
                    "color": r[1],
                    "printer_model": r[2],
                    "cartridge_name": r[3],
                    "cartridge_key": r[4],
                    "timestamp": r[5],
                    "print_total": r[6],
                    "full_color": r[7],
                    "black_white": r[8],
                    "usage_counter": r[9],
                    "toner_level": r[10],
                    "raw_level": r[11],
                    "source": r[12],
                    "valid": r[13],
                    "reject_reason": r[14],
                }
                grouped[(str(r[0] or ""), str(r[1] or ""))].append(d)

    series_reports = []
    early_v1_apes: List[float] = []
    early_v2_apes: List[float] = []
    ci_hits: List[int] = []

    for (pid, color), s_rows in grouped.items():
        st = state_lookup.get((pid, color), {})
        model_txt = str(st.get("printer_model") or (s_rows[-1].get("printer_model") if s_rows else "") or "")
        prior_src = str(st.get("yield_source") or "default")
        raw_prior = st.get("yield_per_page") or st.get("effective_yield")
        prior_y = int(raw_prior) if (raw_prior and prior_src in ("catalog", "device_capacity", "shared_profile")) else None

        readings: List[SupplyReading] = []
        for r in s_rows:
            u_cnt = r.get("usage_counter")
            if u_cnt is None:
                continue
            rej = str(r.get("reject_reason") or "")
            if not snapshot_is_usable(r.get("valid", 1), rej):
                continue
            raw_lv = r.get("raw_level") if r.get("raw_level") is not None else r.get("toner_level", r.get("toner_pct"))
            src_txt = str(r.get("source") or "poll").lower()
            lv_kind = str(r.get("level_kind") or ("http_scrape" if "http" in src_txt or "web" in src_txt else "percent"))
            candidate = SupplyReading(
                usage_counter=u_cnt,
                raw_level=raw_lv,
                level_kind=lv_kind,
                provenance="http_scrape" if lv_kind == "http_scrape" else "rfc3805",
                observed_at=str(r.get(ts_col) or ""),
                vendor=model_txt,
                color=color,
                chip_id=str(r.get("cartridge_key") or "") or None,
            )
            # Also protect the legacy comparison and ETA from malformed rows;
            # the v2 pure pipeline independently applies this same validation.
            if reading_to_interval(candidate) is not None:
                readings.append(candidate)
        if not readings:
            continue

        basis = str(s_rows[-1].get("counter_basis") or "")
        v2_full = estimate_supply_series(
            readings,
            vendor_or_model=model_txt,
            prior_yield=prior_y,
            prior_source=prior_src,
            current_level=readings[-1].raw_level,
            counter_basis=basis,
        )
        v1_y, v1_conf = legacy_v1_pointwise_estimate(readings, prior_yield=prior_y)

        # Evaluate early-horizon accuracy on segments that achieved >= 45% realized drop
        segs = segment_readings(readings, spec=get_brand_spec(model_txt))
        for seg in segs:
            full_fit = fit_segment(seg.observations, spec=get_brand_spec(model_txt))
            if full_fit.yield_pages and full_fit.drop_pct >= 45.0 and not full_fit.suspect:
                gt_y = full_fit.yield_pages
                half_idx = max(3, len(seg.observations) // 2)
                early_sub = [
                    SupplyReading(
                        usage_counter=ob.usage_counter,
                        raw_level=ob.level_nominal,
                        level_kind=ob.level_kind,
                        vendor=model_txt,
                    )
                    for ob in seg.observations[:half_idx]
                ]
                e_v1, _ = legacy_v1_pointwise_estimate(early_sub, prior_yield=prior_y)
                e_v2 = estimate_supply_series(early_sub, vendor_or_model=model_txt, prior_yield=prior_y, prior_source=prior_src)
                early_v1_apes.append(abs(e_v1 - gt_y) / gt_y)
                early_v2_apes.append(abs(e_v2.capacity_pages - gt_y) / gt_y)
                ci_hits.append(1 if (e_v2.capacity_ci95[0] <= gt_y <= e_v2.capacity_ci95[1]) else 0)

        series_reports.append(
            {
                "printer_ip": pid,
                "color": color,
                "polls": len(readings),
                "plateaus": v2_full.n_plateaus,
                "v1_capacity": v1_y,
                "v1_confidence": v1_conf,
                **v2_full.to_api_dict(),
            }
        )

    summary = {
        "mode": "db_replay",
        "db_path": label,
        "series_count": len(series_reports),
        "closed_or_deep_segments_evaluated": len(early_v2_apes),
        "early_v1_mape_pct": round(100.0 * statistics.mean(early_v1_apes), 2) if early_v1_apes else None,
        "early_v2_mape_pct": round(100.0 * statistics.mean(early_v2_apes), 2) if early_v2_apes else None,
        "early_ci95_coverage_pct": round(100.0 * statistics.mean(ci_hits), 1) if ci_hits else None,
        "series": series_reports,
    }
    return summary


def _print_synthetic_report(res: Dict[str, object]) -> None:
    fs = res["fleet_summary"]
    print("=" * 94)
    print("  MULTI-PRINTER YIELD ESTIMATOR v2 vs v1 — 10-BRAND BACKTEST & CI95 CALIBRATION")
    print("=" * 94)
    print(
        f"{'Brand':<10} {'Scenario':<22} {'v1 MAPE':>8} {'v2 MAPE':>8} "
        f"{'v1 Med':>7} {'v2 Med':>7} {'MLE CI95':>9} {'EB CI95':>8}"
    )
    print("-" * 94)
    for brand, b in res["per_brand"].items():
        mle_c = f"{b['v2_mle_ci95_coverage_pct']:.1f}%" if b["v2_mle_ci95_coverage_pct"] is not None else "n/a"
        eb_c = f"{b['v2_eb_ci95_coverage_pct']:.1f}%" if b["v2_eb_ci95_coverage_pct"] is not None else "n/a"
        print(
            f"{brand:<10} {b['mode']:<22} {b['v1_mape_pct']:>7.2f}% {b['v2_mape_pct']:>7.2f}% "
            f"{b['v1_med_ape_pct']:>6.2f}% {b['v2_med_ape_pct']:>6.2f}% {mle_c:>9} {eb_c:>8}"
        )
    print("-" * 94)
    print(
        f"FLEET ({res['total_trials']} cartridges): "
        f"v1 MAPE={fs['v1_mape_pct']:.2f}% -> v2 MAPE={fs['v2_mape_pct']:.2f}% "
        f"({fs['mape_relative_reduction_pct']:+.1f}% reduction)"
    )
    print(
        f"Median APE: v1={fs['v1_median_ape_pct']:.2f}% -> v2={fs['v2_median_ape_pct']:.2f}% | "
        f"p90 APE: v1={fs['v1_p90_ape_pct']:.2f}% -> v2={fs['v2_p90_ape_pct']:.2f}%"
    )
    print(
        f"Empirical CI95 Coverage: MLE={fs['v2_mle_ci95_coverage_pct']:.2f}% | "
        f"EB={fs['v2_eb_ci95_coverage_pct']:.2f}% | "
        f"False High-Conf (>15% err): v1={fs['v1_false_high_confidence_rate_pct']:.2f}% vs v2={fs['v2_false_high_confidence_rate_pct']:.2f}%"
    )
    print(f"Idle-Poll Compression Invariance Drift: {fs['idle_poll_invariance_diff']:.8f}")
    print("=" * 94)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest v2 interval-censored yield estimator vs v1.")
    parser.add_argument("--db", default="", help="Path to SQLite database containing toner_snapshots_v2.")
    parser.add_argument("--live-db", action="store_true", help="Replay configured DB (PostgreSQL or SQLite).")
    parser.add_argument("--synthetic", action="store_true", help="Run 10-brand synthetic Monte Carlo benchmark.")
    parser.add_argument("--trials", type=int, default=80, help="Trials per brand in synthetic mode.")
    parser.add_argument("--verify-gates", action="store_true", help="Enforce quantitative acceptance gates.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    if args.db or args.live_db:
        result = run_db_replay(args.db if args.db else None)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(
                f"DB Replay ({result['db_path']}): {result['series_count']} series, "
                f"closed segments={result.get('closed_or_deep_segments_evaluated', 0)}, "
                f"v1 MAPE={result.get('early_v1_mape_pct')}%, v2 MAPE={result.get('early_v2_mape_pct')}%, "
                f"CI95={result.get('early_ci95_coverage_pct')}%"
            )
        return 0

    result = run_synthetic_benchmark(trials_per_brand=args.trials)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_synthetic_report(result)

    if args.verify_gates:
        fs = result["fleet_summary"]
        failures = []
        if fs["mape_relative_reduction_pct"] < 30.0:
            failures.append(f"Expected >=30% MAPE reduction, got {fs['mape_relative_reduction_pct']}%")
        if fs["v2_median_ape_pct"] > 5.0:
            failures.append(f"Expected v2 Median APE <= 5.0%, got {fs['v2_median_ape_pct']}%")
        if not (90.0 <= fs["v2_mle_ci95_coverage_pct"] <= 99.5):
            failures.append(f"Expected MLE CI95 coverage in [90%, 99.5%], got {fs['v2_mle_ci95_coverage_pct']}%")
        if fs["idle_poll_invariance_diff"] > 1e-6:
            failures.append(f"Idle-poll invariance violated: {fs['idle_poll_invariance_diff']}")
        if failures:
            for msg in failures:
                print(f"[GATE FAILED] {msg}", file=sys.stderr)
            return 1
        if not args.json:
            print("[ALL ACCEPTANCE GATES PASSED]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

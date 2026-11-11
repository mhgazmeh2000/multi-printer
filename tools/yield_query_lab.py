"""Bounded synthetic query lab; never connects to a supplied DSN or loads .env.

Optional dependencies: psycopg[binary], pgserver. Creates and deletes its own
PostgreSQL cluster. This is NOT a benchmark of the Windows copy or production.
No candidate is installed into the application by this tool.

Example: python -m tools.yield_query_lab --rows 2126300 --repeats 2
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Optional


def loader_query(*, backend="postgresql", ip=None, color=None, limit=400):
    """Capture the checked-out loader's SQL without importing app/settings/.env.

    Execute only the loader function's AST, with no database connection in its
    namespace. Keep this extraction explicit: fail if the loader ever needs new
    application dependencies rather than importing the app as a fallback.
    """
    source = Path(__file__).resolve().parents[1] / "core" / "yield_engine.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_interval_history_query")
    namespace = {"Optional": Optional, "BACKEND_POSTGRESQL": "postgresql"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

    class Capture:
        def __init__(self):
            self.backend = backend

    return namespace["_interval_history_query"](Capture(), ip=ip, color=color, limit=limit)


def candidates(sql):
    """Small window-only experiments; reject silently stale source templates."""
    lead = """COALESCE(LEAD(CASE WHEN changed = 1 THEN previous_id ELSE id END)
                       OVER (PARTITION BY printer_ip, slot_color ORDER BY id), id)"""
    lag = """COALESCE(LAG(CASE WHEN changed = 1 THEN previous_id ELSE id END)
                       OVER (PARTITION BY printer_ip, slot_color ORDER BY id DESC
                             ROWS UNBOUNDED PRECEDING), id)"""
    window = "WINDOW w AS (PARTITION BY printer_ip, LOWER(color) ORDER BY id)"
    bounded = ("WINDOW w AS (PARTITION BY printer_ip, LOWER(color) ORDER BY id "
               "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING)")
    if sql.count(lead) != 1 or sql.count(window) != 1:
        raise ValueError("Loader changed; review the experimental SQL templates")
    return {"current": sql, "reverse_edges": sql.replace(lead, lag),
            "bounded_frame": sql.replace(window, bounded)}


def plan_summary(result):
    plan = result[0]
    return {"execution_ms": plan["Execution Time"],
            "planning_ms": plan["Planning Time"],
            "endpoint_rows": plan["Plan"]["Actual Rows"],
            # Parent totals already include their descendants: do not sum nodes.
            "temp_read_blocks": plan["Plan"].get("Temp Read Blocks", 0),
            "temp_written_blocks": plan["Plan"].get("Temp Written Blocks", 0)}


@contextmanager
def isolated_libpq_environment():
    """CLI-only: prevent PGHOSTADDR/PGSERVICE/etc overriding the private URI.

    Restore the caller's environment afterwards; never log its values. This
    process-global context is for this single-threaded diagnostic, not the app.
    """
    inherited = {k: v for k, v in os.environ.items() if k.upper().startswith('PG')}
    for key in inherited:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key in tuple(os.environ):
            if key.upper().startswith('PG'):
                os.environ.pop(key, None)
        os.environ.update(inherited)


def run_lab(*, rows, repeats, slots, timeout_seconds):
    with isolated_libpq_environment():
        return _run_lab(rows=rows, repeats=repeats, slots=slots,
                        timeout_seconds=timeout_seconds)


def _run_lab(*, rows, repeats, slots, timeout_seconds):
    import pgserver
    import psycopg

    sql, params = loader_query()
    queries = {k: v.replace("?", "%s") for k, v in candidates(sql).items()}
    report = {"scope": "synthetic disposable PostgreSQL only",
              "rows": rows, "slots": slots, "repeats": repeats,
              "windows_copy_tested": False, "deployment_ready": False,
              "measurements": [], "exact_ordered_output_equal": None,
              "cluster_cleanup_completed": False}
    server = None
    with tempfile.TemporaryDirectory(prefix="printguard-query-lab-") as directory:
        try:
            server = pgserver.get_server(Path(directory) / "data", cleanup_mode="delete")
            # Explicit local URI from the cluster handle, never libpq env/DSN.
            with psycopg.connect(server.get_uri(), autocommit=True) as conn:
                conn.execute("CREATE DATABASE printguard_test_query_lab")
            with psycopg.connect(server.get_uri(database="printguard_test_query_lab"),
                                autocommit=True) as conn:
                conn.execute("SET work_mem = '4MB'")
                conn.execute("SELECT set_config('statement_timeout', %s, false)",
                             (f"{timeout_seconds}s",))
                # Match the loader-relevant types in migration 0001; no schema
                # migration, new index, cache, trigger or production tuning.
                conn.execute("""CREATE TABLE toner_snapshots_v2 (
                    id BIGINT PRIMARY KEY, printer_ip TEXT NOT NULL, color TEXT NOT NULL,
                    usage_counter INTEGER, toner_level INTEGER, raw_level INTEGER,
                    source TEXT, valid INTEGER, reject_reason TEXT, timestamp TEXT NOT NULL,
                    cartridge_key TEXT, full_color INTEGER, print_total INTEGER)""")
                conn.execute("""INSERT INTO toner_snapshots_v2
                    SELECT i, '192.0.2.' || ((i-1) %% %s + 1), 'black',
                        ((i-1)/%s)::integer,
                        (100-(((i-1)/%s)/100) %% 100)::integer,
                        (100-(((i-1)/%s)/100) %% 100)::integer,
                        'poll', 1, NULL, '2026-01-01T00:00:00Z', 'chip', NULL,
                        ((i-1)/%s)::integer
                    FROM generate_series(1, %s::bigint) AS i""",
                             (slots, slots, slots, slots, slots, rows))
                conn.execute("""CREATE INDEX idx_toner_snapshots_v2_ip_color_ts
                    ON toner_snapshots_v2(printer_ip, color, timestamp)""")
                conn.execute("ANALYZE toner_snapshots_v2")
                report["settings"] = {name: conn.execute("SHOW " + name).fetchone()[0]
                                      for name in ("server_version", "shared_buffers", "work_mem",
                                                   "max_parallel_workers_per_gather",
                                                   "statement_timeout", "jit")}
                conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                reference = None
                equality = {}
                names = list(queries)
                for iteration in range(repeats):
                    # Rotate order; these are not cold-cache measurements.
                    order = names[iteration % len(names):] + names[:iteration % len(names)]
                    for name in order:
                        measured = time.perf_counter()
                        explained = conn.execute(
                            "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
                            + queries[name], params).fetchone()[0]
                        measurement = {"candidate": name, "iteration": iteration + 1,
                                       "explain_wall_seconds": round(time.perf_counter() - measured, 4),
                                       **plan_summary(explained)}
                        report["measurements"].append(measurement)
                        print(json.dumps(measurement), flush=True)
                        if iteration == 0:
                            # Full returned tuples in their actual order, not a
                            # checksum, row count or sorted-away discrepancy.
                            output = conn.execute(queries[name], params).fetchall()
                            if name == "current":
                                reference = output
                            equality[name] = output == reference
                report["exact_ordered_output_equal"] = equality
                conn.execute("ROLLBACK")
        except Exception as exc:
            # Do not dump connection strings, server logs or exception messages.
            report["error"] = {"type": type(exc).__name__,
                               "sqlstate": getattr(exc, "sqlstate", None)}
        finally:
            if server is not None:
                server.cleanup()
                report["cluster_cleanup_completed"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--slots", type=int, default=48)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    if not (1 <= args.rows <= 3000000 and 1 <= args.slots <= 200
            and 1 <= args.repeats <= 3 and 1 <= args.timeout_seconds <= 30):
        parser.error("Bounds: rows 1..3000000; slots 1..200; repeats 1..3; timeout 1..30")
    report = run_lab(rows=args.rows, slots=args.slots, repeats=args.repeats,
                     timeout_seconds=args.timeout_seconds)
    print(json.dumps(report, indent=2), flush=True)
    return int(bool(report.get("error"))
               or not report["cluster_cleanup_completed"]
               or not all((report["exact_ordered_output_equal"] or {"missing": False}).values()))


if __name__ == "__main__":
    raise SystemExit(main())

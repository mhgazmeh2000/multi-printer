"""Opt-in PostgreSQL run reader; no DDL or background rebuild at app startup."""
from __future__ import annotations

import os
import re

from core.db.retry import raise_if_retryable

COLUMNS = ("printer_ip", "color", "usage_counter", "toner_level", "raw_level", "source",
           "valid", "reject_reason", "timestamp", "cartridge_key", "full_color", "print_total")


def load_derived_snapshots(conn, history_sql, history_params, *, ip=None, color=None, limit=400):
    """None means use the unchanged history reader, [] means a valid empty result.

    Readiness and both reader branches share ONE MVCC statement snapshot. A
    savepoint handles missing/partially installed schema without poisoning an
    enclosing write transaction; it never rolls back the caller's earlier work.
    No TTL, MAX(id), persistent readiness cache, or stale-summary fallback.
    """
    if conn.backend != "postgresql" or os.getenv("YIELD_DERIVED_RUNS_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    conn.execute("SAVEPOINT yield_runs_read")
    try:
        # Avoid a parse-time undefined-relation error on pre-migration databases.
        present = conn.execute("""SELECT to_regclass('yield_run_meta') IS NOT NULL
            AND to_regclass('yield_run_slots') IS NOT NULL
            AND to_regclass('yield_run_boundaries') IS NOT NULL""").fetchone()[0]
        if not present:
            conn.execute("RELEASE SAVEPOINT yield_runs_read")
            return None
        sql, params = reader_query(history_sql, history_params, ip=ip, color=color, limit=limit)
        rows = conn.execute(sql, params).fetchall()
        conn.execute("RELEASE SAVEPOINT yield_runs_read")
        return rows
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT yield_runs_read")
        conn.execute("RELEASE SAVEPOINT yield_runs_read")
        raise_if_retryable(exc)
        # Only absent/incompatible optional objects or insufficient privilege are
        # expected fallbacks. Timeouts, corruption and other DB errors propagate.
        if getattr(exc, "sqlstate", None) not in {"42P01", "42703", "3F000", "42501"}:
            raise
        return None


def reader_query(history_sql, history_params, *, ip=None, color=None, limit=400):
    needle = 'SELECT s.printer_ip, s.color'
    if history_sql.count(needle) != 1:
        raise ValueError("REVIEW_HISTORY_QUERY_TEMPLATE")
    reference = history_sql.replace(needle, 'SELECT s.id AS snapshot_id, s.printer_ip, s.color')
    params = {'limit': max(1, int(limit))}
    for i, value in enumerate(history_params):
        reference = reference.replace('?', f'%(history_{i})s', 1)
        params[f'history_{i}'] = value
    clauses = ['st.last_id IS NOT NULL', '(SELECT ok FROM _run_ready)']
    if ip:
        clauses.append('st.printer_ip=%(ip)s')
        params['ip'] = ip
    if color:
        clauses.append('st.slot_color=%(color)s')
        params['color'] = color.lower()
    output = ', '.join(COLUMNS)
    source_output = ', '.join('s.' + c for c in COLUMNS)
    sql = f"""WITH _run_ready AS MATERIALIZED (
        SELECT COALESCE((SELECT ready AND version=1 FROM yield_run_meta
                         WHERE singleton), false) AS ok
    ), _run_chosen AS (
        SELECT st.printer_ip, st.slot_color, st.last_id, b.start_id, b.previous_id
        FROM yield_run_slots st CROSS JOIN LATERAL (
            SELECT start_id, previous_id FROM yield_run_boundaries b
            WHERE b.printer_ip=st.printer_ip AND b.slot_color=st.slot_color
            ORDER BY start_id DESC LIMIT %(limit)s
        ) b WHERE {' AND '.join(clauses)}
    ), _run_ends AS (
        SELECT start_id, COALESCE(LAG(previous_id) OVER (
            PARTITION BY printer_ip, slot_color ORDER BY start_id DESC), last_id) AS last_id
        FROM _run_chosen
    ), _run_ids AS (
        SELECT start_id AS id FROM _run_ends UNION SELECT last_id FROM _run_ends
    ) SELECT {output} FROM (
        SELECT s.id AS snapshot_id, {source_output}
        FROM _run_ids e CROSS JOIN LATERAL (
            SELECT * FROM toner_snapshots_v2 s WHERE s.id=e.id LIMIT 1
        ) s
        UNION ALL
        SELECT * FROM ({reference}) old WHERE NOT (SELECT ok FROM _run_ready)
    ) results ORDER BY snapshot_id"""
    ordered = []
    def positional(match):
        ordered.append(params[match.group(1)])
        return "?"
    return re.sub(r"%\((\w+)\)s", positional, sql), ordered


def rebuild_yield_runs(conn, *, progress=None, max_slots=None):
    """Atomic offline build, scanning COMPLETE history one normalized slot at a time.

    A callback receives only phase/ordinal/count, never slot identities. Limits
    abort the whole build; they never produce a truncated ready view. Caller owns
    the transaction, commit/rollback and statement/total deadlines. This is
    an explicit maintenance primitive, never called automatically by the app.
    """
    def notify(phase, **info):
        if progress is not None:
            progress(phase, info)

    if conn.backend != "postgresql":
        raise ValueError("PostgreSQL required")
    if conn.raw_connection.autocommit:
        raise ValueError("Atomic rebuild requires autocommit=False and caller-owned commit/rollback")
    if conn.execute('SHOW transaction_isolation').fetchone()[0] != 'read committed':
        raise ValueError('READ_COMMITTED_REBUILD_REQUIRED')
    notify('REBUILD_LOCK')
    conn.execute('LOCK TABLE toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE')
    # Check under the source lock: deactivation cannot race the ready marker.
    installed = conn.execute("""SELECT count(*)=5 AND COALESCE(bool_and(tgenabled='O'), false)
        FROM pg_trigger WHERE tgrelid='toner_snapshots_v2'::regclass AND NOT tgisinternal
        AND (tgname, tgfoid) IN (
            ('yield_runs_lock', to_regprocedure('yield_run_lock_slots()')),
            ('yield_runs_insert', to_regprocedure('yield_run_maintain()')),
            ('yield_runs_delete', to_regprocedure('yield_run_maintain()')),
            ('yield_runs_update', to_regprocedure('yield_run_maintain()')),
            ('yield_runs_truncate', to_regprocedure('yield_run_clear_runs()')))
        """).fetchone()[0]
    if not installed:
        raise RuntimeError('EXPLICIT_YIELD_MAINTENANCE_ACTIVATION_REQUIRED')
    conn.execute('''INSERT INTO yield_run_meta VALUES (true, 1, false)
        ON CONFLICT (singleton) DO UPDATE SET ready=false''')
    conn.execute('TRUNCATE yield_run_boundaries')
    conn.execute('UPDATE yield_run_slots SET last_id=NULL, revision=revision+1')
    notify('BUILD_SLOT_TAILS')
    conn.execute("""INSERT INTO yield_run_slots (printer_ip, slot_color, last_id)
        SELECT printer_ip, LOWER(color), MAX(id) FROM toner_snapshots_v2
        GROUP BY printer_ip, LOWER(color)
        ON CONFLICT (printer_ip, slot_color) DO UPDATE SET
            last_id=EXCLUDED.last_id, revision=yield_run_slots.revision+1""")
    keys = conn.execute('''SELECT printer_ip, slot_color FROM yield_run_slots
        WHERE last_id IS NOT NULL ORDER BY printer_ip, slot_color''').fetchall()
    if max_slots is not None and len(keys) > max_slots:
        raise RuntimeError('HISTORY_SLOT_COUNT_NEEDS_REVIEW')
    for ordinal, key in enumerate(keys, 1):
        notify('BUILD_BOUNDARIES_SLOT', ordinal=ordinal, total=len(keys))
        conn.execute("""INSERT INTO yield_run_boundaries
            WITH marked AS (
                SELECT id, printer_ip, LOWER(color) AS slot_color,
                    LAG(id) OVER w AS previous_id,
                    CASE WHEN LAG(id) OVER w IS NULL OR
                    ROW(toner_level, raw_level, COALESCE(source,''), valid,
                        COALESCE(reject_reason,''), COALESCE(cartridge_key,''), full_color IS NULL)
                    IS DISTINCT FROM LAG(ROW(toner_level, raw_level, COALESCE(source,''), valid,
                        COALESCE(reject_reason,''), COALESCE(cartridge_key,''), full_color IS NULL)) OVER w
                    OR usage_counter < LAG(usage_counter) OVER w THEN true ELSE false END AS changed
                FROM toner_snapshots_v2
                WHERE printer_ip=? AND LOWER(color)=?
                WINDOW w AS (ORDER BY id)
            ) SELECT id, printer_ip, slot_color, previous_id FROM marked WHERE changed""", key)
    notify('REBUILD_READY', total=len(keys))
    conn.execute('UPDATE yield_run_meta SET version=1, ready=true')

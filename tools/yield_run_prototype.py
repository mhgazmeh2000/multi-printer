"""Local-only transactional run-boundary prototype; NOT an app migration.

No DSN input, app/settings import or .env loading. Optional dependencies:
psycopg[binary], pgserver. CLI owns and deletes its PostgreSQL cluster.
    python -m tools.yield_run_prototype --rows 2126300 --repeats 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time

from tools.yield_query_lab import isolated_libpq_environment, loader_query, plan_summary

SCHEMA = "yield_run_lab"
COLUMNS = ("printer_ip", "color", "usage_counter", "toner_level", "raw_level", "source",
           "valid", "reject_reason", "timestamp", "cartridge_key", "full_color", "print_total")
BASE_DDL = """CREATE TABLE yield_run_lab.toner_snapshots_v2 (
    id BIGINT PRIMARY KEY, printer_ip TEXT NOT NULL, color TEXT NOT NULL,
    usage_counter INTEGER, toner_level INTEGER, raw_level INTEGER, source TEXT,
    valid INTEGER, reject_reason TEXT, timestamp TEXT NOT NULL,
    cartridge_key TEXT, full_color INTEGER, print_total INTEGER);
CREATE INDEX idx_toner_snapshots_v2_ip_color_ts
    ON yield_run_lab.toner_snapshots_v2(printer_ip, color, timestamp);"""


def guard(conn):
    if not conn.execute("SELECT current_database()").fetchone()[0].startswith('printguard_test_'):
        raise RuntimeError('LOCAL_TEST_DATABASE_REQUIRED')


def create_source(conn):
    guard(conn)
    # Deliberately no IF NOT EXISTS: never adopt or overwrite another lab.
    conn.execute('CREATE SCHEMA yield_run_lab')
    conn.execute(BASE_DDL)


def install(conn):
    guard(conn)
    conn.execute((Path(__file__).parent / 'sql' / 'yield_run_prototype.sql').read_text())


def rebuild(conn, *, progress=None, max_slots=None):
    """Atomic offline build, scanning COMPLETE history one normalized slot at a time.

    A callback receives only phase/ordinal/count, never slot identities. Limits
    abort the whole build; they never produce a truncated ready view. Caller owns
    the transaction and statement/total deadlines (the Windows runner does so).
    """
    def notify(phase, **info):
        if progress is not None:
            progress(phase, info)

    guard(conn)
    notify('REBUILD_LOCK')
    conn.execute('LOCK TABLE yield_run_lab.toner_snapshots_v2 IN ACCESS EXCLUSIVE MODE')
    conn.execute('''INSERT INTO yield_run_lab.meta VALUES (true, 1, false)
        ON CONFLICT (singleton) DO UPDATE SET ready=false''')
    conn.execute('TRUNCATE yield_run_lab.boundaries')
    conn.execute('UPDATE yield_run_lab.slots SET last_id=NULL, revision=revision+1')
    notify('BUILD_SLOT_TAILS')
    conn.execute("""INSERT INTO yield_run_lab.slots (printer_ip, slot_color, last_id)
        SELECT printer_ip, LOWER(color), MAX(id) FROM yield_run_lab.toner_snapshots_v2
        GROUP BY printer_ip, LOWER(color)
        ON CONFLICT (printer_ip, slot_color) DO UPDATE SET
            last_id=EXCLUDED.last_id, revision=yield_run_lab.slots.revision+1""")
    keys = conn.execute('''SELECT printer_ip, slot_color FROM yield_run_lab.slots
        WHERE last_id IS NOT NULL ORDER BY printer_ip, slot_color''').fetchall()
    if max_slots is not None and len(keys) > max_slots:
        raise RuntimeError('HISTORY_SLOT_COUNT_NEEDS_REVIEW')
    for ordinal, key in enumerate(keys, 1):
        notify('BUILD_BOUNDARIES_SLOT', ordinal=ordinal, total=len(keys))
        conn.execute("""INSERT INTO yield_run_lab.boundaries
            WITH marked AS (
                SELECT id, printer_ip, LOWER(color) AS slot_color,
                    LAG(id) OVER w AS previous_id,
                    CASE WHEN LAG(id) OVER w IS NULL OR
                    ROW(toner_level, raw_level, COALESCE(source,''), valid,
                        COALESCE(reject_reason,''), COALESCE(cartridge_key,''), full_color IS NULL)
                    IS DISTINCT FROM LAG(ROW(toner_level, raw_level, COALESCE(source,''), valid,
                        COALESCE(reject_reason,''), COALESCE(cartridge_key,''), full_color IS NULL)) OVER w
                    OR usage_counter < LAG(usage_counter) OVER w THEN true ELSE false END AS changed
                FROM yield_run_lab.toner_snapshots_v2
                WHERE printer_ip=%s AND LOWER(color)=%s
                WINDOW w AS (ORDER BY id)
            ) SELECT id, printer_ip, slot_color, previous_id FROM marked WHERE changed""", key)
    notify('REBUILD_READY', total=len(keys))
    conn.execute('UPDATE yield_run_lab.meta SET version=1, ready=true')


def reference_query(**kwargs):
    sql, params = loader_query(**kwargs)
    return sql.replace('toner_snapshots_v2', 'yield_run_lab.toner_snapshots_v2').replace('?', '%s'), params


def reader_query(*, ip=None, color=None, limit=400):
    """Derived data + original payload + readiness gate in ONE SQL snapshot.

    Unready/unknown version uses the original full-history query, not stale
    summaries. The disabled branch must have Actual Loops=0 in actual plans.
    Missing schema is intentionally not supported by this unintegrated lab.
    """
    reference, values = reference_query(ip=ip, color=color, limit=limit)
    needle = 'SELECT s.printer_ip, s.color'
    if reference.count(needle) != 1:
        raise RuntimeError('REVIEW_REFERENCE_QUERY_TEMPLATE')
    reference = reference.replace(needle, 'SELECT s.id AS snapshot_id, s.printer_ip, s.color')
    params = {'limit': max(1, int(limit))}
    for i, value in enumerate(values):
        reference = reference.replace('%s', f'%(reference_{i})s', 1)
        params[f'reference_{i}'] = value
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
        SELECT COALESCE((SELECT ready AND version=1 FROM yield_run_lab.meta
                         WHERE singleton), false) AS ok
    ), _run_chosen AS (
        SELECT st.printer_ip, st.slot_color, st.last_id, b.start_id, b.previous_id
        FROM yield_run_lab.slots st CROSS JOIN LATERAL (
            SELECT start_id, previous_id FROM yield_run_lab.boundaries b
            WHERE b.printer_ip=st.printer_ip AND b.slot_color=st.slot_color
            ORDER BY start_id DESC LIMIT %(limit)s
        ) b WHERE {' AND '.join(clauses)}
    ), _run_ends AS (
        SELECT start_id,
            COALESCE(LAG(previous_id) OVER (
                PARTITION BY printer_ip, slot_color ORDER BY start_id DESC), last_id) AS last_id
        FROM _run_chosen
    ), _run_ids AS (
        SELECT start_id AS id FROM _run_ends UNION SELECT last_id FROM _run_ends
    ) SELECT {output} FROM (
        SELECT s.id AS snapshot_id, {source_output}
        FROM _run_ids e CROSS JOIN LATERAL (
            SELECT * FROM yield_run_lab.toner_snapshots_v2 s
            WHERE s.id=e.id LIMIT 1
        ) s
        UNION ALL
        SELECT * FROM ({reference}) old WHERE NOT (SELECT ok FROM _run_ready)
    ) results ORDER BY snapshot_id"""
    return sql, params


def seed(conn, rows, slots, plateau_polls=100):
    conn.execute("""INSERT INTO yield_run_lab.toner_snapshots_v2
        SELECT i, '192.0.2.' || ((i-1) %% %s + 1), 'black',
            ((i-1)/%s)::integer, (100-(((i-1)/%s)/%s) %% 100)::integer,
            (100-(((i-1)/%s)/%s) %% 100)::integer,
            'poll', 1, NULL, '2026-01-01T00:00:00Z', 'chip', NULL, ((i-1)/%s)::integer
        FROM generate_series(1, %s::bigint) AS i""",
                 (slots, slots, slots, plateau_polls, slots, plateau_polls, slots, rows))
    conn.execute('ANALYZE yield_run_lab.toner_snapshots_v2')


def write_fixture(start_id, slots, count=200):
    return [(start_id+i, f'192.0.2.{i % slots + 1}', 'black', 100000+i,
             50, 50, 'poll', 1, '', '2026-01-02T00:00:00Z', 'chip', None, 100000+i)
            for i in range(count)]


def measure_writes(conn, rows):
    times = []
    wal_start = conn.execute('SELECT pg_current_wal_insert_lsn()').fetchone()[0]
    for row in rows:
        start = time.perf_counter()
        # Autocommit: each INSERT is a committed single-row transaction.
        conn.execute('INSERT INTO yield_run_lab.toner_snapshots_v2 VALUES ('
                     + ','.join(['%s'] * 13) + ')', row)
        times.append((time.perf_counter()-start)*1000)
    wal_bytes = int(conn.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)',
                                 (wal_start,)).fetchone()[0])
    return {'transactions': len(times), 'p50_ms': round(statistics.median(times), 4),
            'p95_ms': round(sorted(times)[int(len(times)*.95)-1], 4),
            'total_seconds': round(sum(times)/1000, 4), 'wal_bytes': wal_bytes}


def measure_bulk_mutations(conn, rows):
    """Committed UPDATE/DELETE/reinsert, restoring identical logical input.

    One sample per operation, not a p95 or sustained-load test. No concurrent
    writer exists in this owned lab. Batch size is at most 1000 rows.
    """
    batch = conn.execute('SELECT * FROM yield_run_lab.toner_snapshots_v2 WHERE id<=%s ORDER BY id',
                         (min(rows, 1000),)).fetchall()
    actions = [
        ('update', 'UPDATE yield_run_lab.toner_snapshots_v2 SET usage_counter=usage_counter+1 WHERE id<=%s', (min(rows, 1000),)),
        ('delete', 'DELETE FROM yield_run_lab.toner_snapshots_v2 WHERE id<=%s', (min(rows, 1000),)),
        ('restore_insert', 'INSERT INTO yield_run_lab.toner_snapshots_v2 VALUES '
         + ','.join(['('+','.join(['%s']*13)+')']*len(batch)), [v for row in batch for v in row]),
    ]
    result = {'batch_rows': len(batch), 'operations': {}}
    for name, sql, params in actions:
        wal = conn.execute('SELECT pg_current_wal_insert_lsn()').fetchone()[0]
        start = time.perf_counter()
        conn.execute(sql, params)
        elapsed = (time.perf_counter()-start)*1000
        size = int(conn.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)', (wal,)).fetchone()[0])
        result['operations'][name] = {'wall_ms': round(elapsed, 4), 'wal_bytes': size}
    return result


def benchmark(rows=2126300, slots=48, repeats=5, plateau_polls=100):
    import pgserver
    import psycopg
    report = {'scope': 'local synthetic prototype only', 'rows': rows, 'slots': slots,
              'plateau_polls': plateau_polls,
              'production_changed': False, 'deployment_ready': False}
    with isolated_libpq_environment(), tempfile.TemporaryDirectory(prefix='yield-run-prototype-') as directory:
        server = pgserver.get_server(Path(directory)/'data', cleanup_mode='delete')
        try:
            with psycopg.connect(server.get_uri(), autocommit=True) as conn:
                conn.execute('CREATE DATABASE printguard_test_run_benchmark')
            with psycopg.connect(server.get_uri(database='printguard_test_run_benchmark'), autocommit=True) as conn:
                conn.execute("SET statement_timeout='30s'")
                conn.execute("SET lock_timeout='3s'")
                conn.execute("SET work_mem='4MB'")
                report['settings'] = {key: conn.execute('SHOW '+key).fetchone()[0] for key in (
                    'server_version', 'shared_buffers', 'work_mem', 'max_parallel_workers_per_gather',
                    'fsync', 'synchronous_commit', 'full_page_writes', 'statement_timeout')}
                create_source(conn)
                seed(conn, rows, slots, plateau_polls)
                report['baseline_source_storage_bytes'] = conn.execute(
                    "SELECT pg_total_relation_size('yield_run_lab.toner_snapshots_v2')").fetchone()[0]
                writes = write_fixture(rows+1, slots)
                report['baseline_writes'] = measure_writes(conn, writes)
                conn.execute('DELETE FROM yield_run_lab.toner_snapshots_v2 WHERE id>%s', (rows,))
                report['baseline_bulk'] = measure_bulk_mutations(conn, rows)
                start = time.perf_counter()
                with conn.transaction():
                    install(conn)
                    rebuild(conn)
                report['install_and_build_seconds'] = round(time.perf_counter()-start, 4)
                conn.execute('ANALYZE yield_run_lab.boundaries')
                conn.execute('ANALYZE yield_run_lab.slots')
                report['boundary_rows'] = conn.execute('SELECT COUNT(*) FROM yield_run_lab.boundaries').fetchone()[0]
                report['storage_bytes'] = {table: conn.execute('SELECT pg_total_relation_size(%s)',
                    ('yield_run_lab.'+table,)).fetchone()[0] for table in ('toner_snapshots_v2', 'boundaries', 'slots')}
                ref, ref_params = reference_query()
                query, params = reader_query()
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                    start = time.perf_counter()
                    expected = conn.execute(ref, ref_params).fetchall()
                    report['reference_select_wall_seconds'] = round(time.perf_counter()-start, 4)
                    start = time.perf_counter()
                    actual = conn.execute(query, params).fetchall()
                    report['first_derived_select_wall_seconds'] = round(time.perf_counter()-start, 4)
                    report['exact_ordered_output_equal'] = expected == actual
                    report['endpoint_rows'] = len(actual)
                    first_plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) '+query, params).fetchone()[0]
                    report['first_explain'] = plan_summary(first_plan)
                    report['warm_reads'] = []
                    for _ in range(repeats):
                        start = time.perf_counter()
                        result = conn.execute(query, params).fetchall()
                        report['warm_reads'].append({'wall_seconds': round(time.perf_counter()-start, 4),
                                                     'exact_equal': result == expected})
                    def scans(node):
                        found = []
                        if 'Scan' in node.get('Node Type', ''):
                            found.append({k: node[k] for k in ('Node Type', 'Relation Name', 'Index Name',
                                          'Actual Rows', 'Actual Loops') if k in node})
                        for child in node.get('Plans', []):
                            found.extend(scans(child))
                        return found
                    report['actual_scan_nodes'] = scans(first_plan[0]['Plan'])
                    report['active_full_snapshot_scans'] = [n for n in report['actual_scan_nodes']
                        if n.get('Relation Name') == 'toner_snapshots_v2'
                        and n.get('Node Type') in ('Seq Scan', 'Parallel Seq Scan')
                        and n.get('Actual Loops', 0) > 0]
                    report['local_warm_read_goal_met'] = all(
                        x['wall_seconds'] <= 2 and x['exact_equal'] for x in report['warm_reads'])
                report['derived_writes'] = measure_writes(conn, writes)
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                    report['equal_after_ingestion'] = conn.execute(ref, ref_params).fetchall() == conn.execute(query, params).fetchall()
                report['derived_bulk'] = measure_bulk_mutations(conn, rows)
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                    report['equal_after_bulk'] = conn.execute(ref, ref_params).fetchall() == conn.execute(query, params).fetchall()
        except Exception as exc:
            report['error'] = {'type': type(exc).__name__, 'sqlstate': getattr(exc, 'sqlstate', None)}
        finally:
            server.cleanup()
            report['cluster_cleanup_completed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=200000)
    parser.add_argument('--slots', type=int, default=48)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--plateau-polls', type=int, default=100)
    args = parser.parse_args()
    if not (1 <= args.rows <= 3000000 and 1 <= args.slots <= 200 and 1 <= args.repeats <= 5
            and 1 <= args.plateau_polls <= 3000000):
        parser.error('Bounds: rows/plateau-polls 1..3000000; slots 1..200; repeats 1..5')
    result = benchmark(args.rows, args.slots, args.repeats, args.plateau_polls)
    print(json.dumps(result, indent=2), flush=True)
    return int(bool(result.get('error')) or not result.get('exact_ordered_output_equal')
               or not result.get('equal_after_ingestion') or not result.get('equal_after_bulk')
               or bool(result.get('active_full_snapshot_scans'))
               or not result.get('local_warm_read_goal_met'))


if __name__ == '__main__':
    raise SystemExit(main())

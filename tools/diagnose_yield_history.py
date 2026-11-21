"""Bounded, genuinely read-only history comparison. NEVER loads .env or PG_DSN.

Choose an explicit SQLite file OR the approved loopback PostgreSQL COPY credentials.
The PostgreSQL copy must already be running. This tool never starts/stops services,
activates derived runs, migrates, ANALYZEs, or prints row values/credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

# Standalone invocation with python -I is supported, without importing app settings.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.yield_query_lab import isolated_libpq_environment, loader_query

NEW_CTE = '''        ), picked AS (
            SELECT r.first_id, r.last_id FROM recent r WHERE r.changed = 1 AND r.rn <= ?
        )'''
NEW_FROM = '''        FROM toner_snapshots_v2 s
        WHERE s.id IN (SELECT first_id FROM picked UNION SELECT last_id FROM picked)
        ORDER BY s.id'''
OLD_FROM = '''        FROM toner_snapshots_v2 s JOIN recent r
          ON s.id = r.first_id OR s.id = r.last_id
        WHERE r.changed = 1 AND r.rn <= ? ORDER BY s.id'''


def legacy_query(query):
    if query.count(NEW_CTE) != 1 or query.count(NEW_FROM) != 1:
        raise ValueError('HISTORY_QUERY_TEMPLATE_CHANGED')
    return query.replace(NEW_CTE, '        )').replace(NEW_FROM, OLD_FROM)


def validate_copy_auth(auth):
    if set(auth) != {'host', 'port', 'user', 'dbname', 'password'} or (
            auth.get('host'), auth.get('port'), auth.get('user'), auth.get('dbname')) != (
                '127.0.0.1', 55439, 'yield_activation_lab', 'yield_activation_copy'):
        raise ValueError('APPROVED_LOOPBACK_COPY_REQUIRED')
    if not isinstance(auth['password'], str) or not auth['password']:
        raise ValueError('COPY_PASSWORD_MISSING')


def verify_copy(conn, expected_system_id, expected_data):
    row = conn.execute('''SELECT host(inet_server_addr()), inet_server_port(),
        current_database(), current_user, current_setting('data_directory'),
        (SELECT system_identifier::text FROM pg_control_system()),
        current_setting('transaction_read_only')''').fetchone()
    if (row[:4] != ('127.0.0.1', 55439, 'yield_activation_copy', 'yield_activation_lab')
            or str(row[5]) != expected_system_id or row[6] != 'on'
            or os.path.normcase(os.path.realpath(row[4])) != os.path.normcase(os.path.realpath(expected_data))):
        raise ValueError('COPY_IDENTITY_OR_READONLY_MISMATCH')


def digest(rows):
    return hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def compare_queries(execute, *, backend, repeats=2, explain=False):
    query, params = loader_query(backend=backend)
    queries = {'legacy': legacy_query(query), 'candidate': query}
    if backend == 'postgresql':
        queries = {name: sql.replace('?', '%s') for name, sql in queries.items()}
    count = execute('SELECT count(*) FROM toner_snapshots_v2', ()).fetchone()[0]
    slots = execute('SELECT count(*) FROM (SELECT printer_ip, LOWER(color) FROM toner_snapshots_v2 GROUP BY printer_ip, LOWER(color)) slots', ()).fetchone()[0]
    if slots > 256:
        raise ValueError('SLOT_COUNT_NEEDS_REVIEW')
    result = {'backend': backend, 'history_rows': count, 'slots': slots,
              'scope': 'history_reader_only; warm-cache effects possible; not full API',
              'measurements': [], 'exact_ordered_equal': True}
    reference = None
    for iteration in range(repeats):
        order = ('legacy', 'candidate') if iteration % 2 == 0 else ('candidate', 'legacy')
        for name in order:
            start = time.perf_counter()
            rows = execute(queries[name], params).fetchall()
            seconds = round(time.perf_counter() - start, 4)
            if reference is None:
                reference = rows
            if rows != reference:
                raise ValueError('ORDERED_OUTPUT_MISMATCH')
            result['measurements'].append({'iteration': iteration + 1, 'query': name,
                                           'seconds': seconds, 'returned_rows': len(rows),
                                           'sha256': digest(rows)})
    if explain and backend == 'postgresql':
        result['plans'] = {}
        for name, sql in queries.items():
            plan = execute('EXPLAIN (FORMAT JSON) ' + sql, params).fetchone()[0][0]['Plan']
            nodes = {}
            def walk(node):
                label = node['Node Type']
                nodes[label] = nodes.get(label, 0) + 1
                for child in node.get('Plans', []):
                    walk(child)
            walk(plan)
            result['plans'][name] = {'node_counts': nodes, 'total_cost': plan['Total Cost'],
                                     'estimated_rows': plan['Plan Rows']}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--sqlite', type=Path)
    group.add_argument('--postgres-copy-auth', type=Path)
    parser.add_argument('--expected-system-id')
    parser.add_argument('--expected-data-directory', type=Path)
    parser.add_argument('--timeout-seconds', type=int, default=120)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--explain', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 120 or not 1 <= args.repeats <= 3:
        parser.error('timeout must be 1..120 seconds and repeats 1..3')
    if args.postgres_copy_auth and (not args.expected_system_id or not args.expected_data_directory):
        parser.error('PostgreSQL requires expected copy system ID and data directory')
    start = time.monotonic()
    deadline = start + 600
    conn = None
    try:
        if args.sqlite:
            path = args.sqlite.resolve(strict=True)
            conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            conn.execute('PRAGMA query_only=ON')
            local_deadline = [min(deadline, time.monotonic() + args.timeout_seconds)]
            conn.set_progress_handler(lambda: int(time.monotonic() >= local_deadline[0]), 1000)
            conn.execute('BEGIN')
            def execute(sql, params):
                local_deadline[0] = min(deadline, time.monotonic() + args.timeout_seconds)
                if time.monotonic() >= deadline:
                    raise TimeoutError('TOTAL_BUDGET_EXCEEDED')
                return conn.execute(sql, params)
            result = compare_queries(execute, backend='sqlite', repeats=args.repeats)
        else:
            import psycopg
            auth = json.loads(args.postgres_copy_auth.read_text(encoding='utf-8-sig'))
            validate_copy_auth(auth)
            with isolated_libpq_environment():
                conn = psycopg.connect(**auth, connect_timeout=5,
                    options='-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000 -c timezone=UTC')
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            verify_copy(conn, args.expected_system_id, args.expected_data_directory)
            def execute(sql, params):
                remaining = min(args.timeout_seconds, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('TOTAL_BUDGET_EXCEEDED')
                conn.execute("SELECT set_config('statement_timeout', %s, true)",
                             (str(max(1, int(remaining * 1000))) + 'ms',))
                return conn.execute(sql, params)
            result = compare_queries(execute, backend='postgresql', repeats=args.repeats, explain=args.explain)
        result.update(status='READONLY_COMPARISON_PASSED', elapsed_seconds=round(time.monotonic()-start, 4))
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        state = getattr(exc, 'sqlstate', None)
        state = state if isinstance(state, str) and len(state) == 5 and state.isascii() and state.isalnum() else 'unknown'
        # Never print an exception message/traceback: drivers may include DSNs/SQL/rows.
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__, 'sqlstate': state}))
        return 1
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                print('{"cleanup_rollback_failed": true}')
            try:
                conn.close()
            except Exception:
                print('{"cleanup_close_failed": true}')


if __name__ == '__main__':
    raise SystemExit(main())

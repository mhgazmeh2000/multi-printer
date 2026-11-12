"""Independent endpoint oracle for query-lab candidates; no application DB access."""
import os
import random
import sqlite3
import unittest
from unittest.mock import patch

from tools.yield_query_lab import candidates, isolated_libpq_environment, loader_query, plan_summary


DDL = """CREATE TEMP TABLE toner_snapshots_v2 (
    id BIGINT PRIMARY KEY, printer_ip TEXT, color TEXT, usage_counter INTEGER,
    toner_level INTEGER, raw_level INTEGER, source TEXT, valid INTEGER,
    reject_reason TEXT, timestamp TEXT, cartridge_key TEXT,
    full_color INTEGER, print_total INTEGER)"""


def fixture(seed, count=1500):
    rng = random.Random(seed)
    rows = []
    for i in range(count):
        slot = rng.randrange(6)
        # Long runs, gapped globally ordered ids, mixed-case slot identities,
        # singleton runs, NULL normalization, admission and rollback changes.
        plateau = (i // 47) % 5
        rows.append((i * 3 + 2, f'192.0.2.{slot // 2 + 1}',
                     ('BLACK' if i % 2 else 'black') if slot % 2 else 'cyan',
                     None if i % 53 == 0 else i % 199,
                     None if plateau == 0 else 80 - plateau,
                     None if plateau == 1 else 80 - plateau,
                     rng.choice((None, '', 'poll')) if plateau == 2 else 'poll',
                     (None, 0, 1, 1, 1)[plateau],
                     (None, '', 'quarantined_chip', '', '')[plateau],
                     str(i), ('', None, 'old', 'new', 'new')[plateau],
                     i if plateau % 2 else None, i))
    return rows


def oracle(rows, *, ip=None, color=None, limit=400):
    slots = {}
    for row in sorted(rows):
        if ip and row[1] != ip:
            continue
        if color and (row[2] or '').lower() != color.lower():
            continue
        slots.setdefault((row[1], row[2].lower() if row[2] is not None else None), []).append(row)
    endpoints = []
    for history in slots.values():
        runs = []
        previous = None
        signature = None
        for row in history:
            current = (row[4], row[5], row[6] or '', row[7], row[8] or '',
                       row[10] or '', row[11] is None)
            rollback = (previous is not None and row[3] is not None
                        and previous[3] is not None and row[3] < previous[3])
            if previous is None or current != signature or rollback:
                runs.append([row, row])
            else:
                runs[-1][1] = row
            previous, signature = row, current
        for first, last in runs[-max(1, int(limit)):]:
            endpoints.append(first)
            if first[0] != last[0]:
                endpoints.append(last)
    return [row[1:] for row in sorted(endpoints)]


class QueryCandidateTests(unittest.TestCase):
    def check_queries(self, conn, backend, rows):
        for kwargs in ({'limit': 1}, {'limit': 3}, {'limit': 400}, {'limit': 1000},
                       {'limit': 0}, {'ip': '192.0.2.1', 'color': 'BLACK', 'limit': 3},
                       {'color': 'cyan', 'limit': 400}, {'ip': 'missing'}):
            query, params = loader_query(backend=backend, **kwargs)
            expected = oracle(rows, **kwargs)
            for name, sql in candidates(query).items():
                with self.subTest(backend=backend, candidate=name, kwargs=kwargs, count=len(rows)):
                    if backend == 'postgresql':
                        sql = sql.replace('?', '%s')
                    self.assertEqual([tuple(r) for r in conn.execute(sql, params).fetchall()], expected)

    def test_randomized_sqlite_candidates_match_independent_oracle(self):
        for seed in (7, 31, 101):
            rows = fixture(seed)
            with sqlite3.connect(':memory:') as conn:
                conn.execute(DDL)
                conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?'] * 13) + ')', rows)
                self.check_queries(conn, 'sqlite', rows)
                # No append-only assumption: change an old boundary then delete
                # history, using the same connection and the same max(id).
                rows[50] = rows[50][:6] + ('manual_reset:empty',) + rows[50][7:]
                conn.execute('UPDATE toner_snapshots_v2 SET source=? WHERE id=?',
                             (rows[50][6], rows[50][0]))
                conn.execute('DELETE FROM toner_snapshots_v2 WHERE id=?', (rows[80][0],))
                del rows[80]
                self.check_queries(conn, 'sqlite', rows)

    def test_empty_singleton_and_long_constant_tail(self):
        for count in (0, 1, 1500):
            rows = [(i * 3, '192.0.2.1', 'black', i, 70, 70, None if i % 2 else '',
                     1, None if i % 2 else '', str(i), '', i, i) for i in range(count)]
            with sqlite3.connect(':memory:') as conn:
                conn.execute(DDL)
                conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?'] * 13) + ')', rows)
                self.check_queries(conn, 'sqlite', rows)

    def test_more_than_four_hundred_runs_and_historical_backfill(self):
        rows = [(i * 3 + 2, '192.0.2.1', 'BLACK' if i % 2 else 'black',
                 i, i // 2, i // 2, 'poll', 1, '', str(i), '', None, i)
                for i in range(1105)]
        with sqlite3.connect(':memory:') as conn:
            conn.execute(DDL)
            conn.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?'] * 13) + ')', rows)
            self.check_queries(conn, 'sqlite', rows)
            self.assertLessEqual(len(oracle(rows)), 800)
            # Backfill and a cross-slot edit below max(id) must take effect now.
            backfill = (rows[850][0] + 1, '192.0.2.1', 'black', 0, 5, 5,
                        'manual_reset:empty', 1, '', 'backfill', '', None, 0)
            rows.append(backfill)
            conn.execute('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['?'] * 13) + ')', backfill)
            conn.execute("UPDATE toner_snapshots_v2 SET printer_ip='192.0.2.2' WHERE id=?", (rows[900][0],))
            rows[900] = (rows[900][0], '192.0.2.2') + rows[900][2:]
            self.check_queries(conn, 'sqlite', rows)

    def test_sql_capture_does_not_import_application_or_dotenv(self):
        import builtins
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name.split('.')[0] in ('core', 'config', 'dotenv'):
                raise AssertionError('Application import is forbidden in the query lab')
            return original(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=guarded):
            sql, params = loader_query(ip='192.0.2.1', color='BLACK', limit=0)
        self.assertIn('ROW(toner_level', sql)
        self.assertEqual(params, ['192.0.2.1', 'black', 1])

    def test_lab_clears_libpq_overrides_and_restores_them_on_failure(self):
        with patch.dict(os.environ, PGHOSTADDR='must-not-be-used', PGSERVICE='must-not-be-used'):
            with self.assertRaises(RuntimeError):
                with isolated_libpq_environment():
                    self.assertFalse(any(k.upper().startswith('PG') for k in os.environ))
                    raise RuntimeError('synthetic failure')
            self.assertEqual(os.environ['PGHOSTADDR'], 'must-not-be-used')
            self.assertEqual(os.environ['PGSERVICE'], 'must-not-be-used')

    def test_templates_fail_closed_when_loader_changes(self):
        with self.assertRaises(ValueError):
            candidates('SELECT 1')

    def test_temp_blocks_not_double_counted(self):
        summary = plan_summary([{'Execution Time': 1, 'Planning Time': 2,
                                'Plan': {'Actual Rows': 3, 'Temp Read Blocks': 4,
                                         'Plans': [{'Temp Read Blocks': 4}]}}])
        self.assertEqual(summary['temp_read_blocks'], 4)

    @unittest.skipUnless(os.getenv('YIELD_TEST_PG_DSN'), 'isolated PostgreSQL DSN not supplied')
    def test_postgresql_candidates_match_independent_oracle(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        dsn = os.environ['YIELD_TEST_PG_DSN']
        if not conninfo_to_dict(dsn).get('dbname', '').startswith('printguard_test_'):
            self.fail('YIELD_TEST_PG_DSN must name an isolated printguard_test_ database')
        with psycopg.connect(dsn) as conn:
            conn.execute(DDL)
            for rows in ([], fixture(7, 1), fixture(31), fixture(101)):
                conn.execute('TRUNCATE toner_snapshots_v2')
                with conn.cursor() as cursor:
                    cursor.executemany('INSERT INTO toner_snapshots_v2 VALUES (' + ','.join(['%s'] * 13) + ')', rows)
                self.check_queries(conn, 'postgresql', rows)
                if rows:
                    conn.execute("UPDATE toner_snapshots_v2 SET source='manual_reset:empty' WHERE id=%s", (rows[0][0],))
                    rows[0] = rows[0][:6] + ('manual_reset:empty',) + rows[0][7:]
                    conn.execute('DELETE FROM toner_snapshots_v2 WHERE id=%s', (rows[-1][0],))
                    rows.pop()
                    self.check_queries(conn, 'postgresql', rows)

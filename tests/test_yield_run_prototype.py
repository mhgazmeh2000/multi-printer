"""Local-only PostgreSQL proof tests. Never reads a user DSN or application .env."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import tempfile
import threading
import time
import uuid

import pytest

from tools.yield_query_lab import isolated_libpq_environment
from tools.yield_run_prototype import create_source, install, rebuild, reader_query, reference_query
from tests.test_yield_query_lab import fixture, oracle

try:
    import psycopg
    import pgserver
except ImportError:
    psycopg = pgserver = None

# Keep unittest discovery/import safe without the optional local-lab package.
# Plain pytest functions belong to CI's pytest-only route, not unittest.
pytestmark = [pytest.mark.pytest_only,
              pytest.mark.skipif(psycopg is None or pgserver is None,
                                 reason='optional psycopg/pgserver lab dependencies not installed')]
TABLE = 'yield_run_lab.toner_snapshots_v2'
INSERT = 'INSERT INTO '+TABLE+' VALUES ('+','.join(['%s']*13)+')'


@pytest.fixture(scope='module')
def private_server():
    with isolated_libpq_environment(), tempfile.TemporaryDirectory(prefix='yield-run-proof-') as directory:
        server = pgserver.get_server(Path(directory)/'data', cleanup_mode='delete')
        try:
            yield server
        finally:
            server.cleanup()


@pytest.fixture
def lab(private_server):
    name = 'printguard_test_runs_' + uuid.uuid4().hex[:10]
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('CREATE DATABASE '+name)
    uri = private_server.get_uri(database=name)
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("SET statement_timeout='10s'")
        create_source(conn)
        with conn.transaction():
            install(conn)
            rebuild(conn)
        yield conn, uri
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('DROP DATABASE '+name)


def insert_many(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(INSERT, rows)


def wait_for_lock(observer, pid):
    """Prove actual overlap, rather than relying on thread scheduling."""
    deadline = time.monotonic() + 3
    pause = threading.Event()
    while time.monotonic() < deadline:
        if observer.execute('SELECT cardinality(pg_blocking_pids(%s))>0', (pid,)).fetchone()[0]:
            return
        pause.wait(.01)
    raise AssertionError('second writer never entered the expected lock wait')


def assert_exact(conn):
    rows = conn.execute('SELECT * FROM '+TABLE+' ORDER BY id').fetchall()
    expected_boundaries = []
    slots = {}
    for r in rows:
        key = r[1], r[2].lower()
        previous = slots.get(key)
        sig = lambda x: (x[4], x[5], x[6] or '', x[7], x[8] or '', x[10] or '', x[11] is None)
        if previous is None or sig(r) != sig(previous) or (
                r[3] is not None and previous[3] is not None and r[3] < previous[3]):
            expected_boundaries.append((r[0], *key, previous[0] if previous else None))
        slots[key] = r
    assert conn.execute('SELECT * FROM yield_run_lab.boundaries ORDER BY start_id').fetchall() == expected_boundaries
    actual_tails = conn.execute('SELECT printer_ip, slot_color, last_id FROM yield_run_lab.slots WHERE last_id IS NOT NULL').fetchall()
    assert sorted(actual_tails) == sorted((*key, r[0]) for key, r in slots.items())
    for options in ({'limit': 1}, {'limit': 3}, {'limit': 400}, {'limit': 1000}, {'limit': 0},
                    {'ip': '192.0.2.1', 'color': 'BLACK'}, {'color': 'cyan'}, {'ip': 'missing'}):
        sql, params = reader_query(**options)
        reference, ref_params = reference_query(**options)
        expected = oracle(rows, **options)
        assert conn.execute(sql, params).fetchall() == expected
        assert conn.execute(reference, ref_params).fetchall() == expected


@pytest.mark.parametrize('seed', [7, 31, 101])
def test_randomized_bulk_historical_mutations(lab, seed):
    conn, _ = lab
    rows = fixture(seed, 800)
    # One bulk INSERT statement; unordered ids must not determine trigger order.
    random.Random(seed).shuffle(rows)
    placeholders = ','.join(['('+','.join(['%s']*13)+')']*len(rows))
    conn.execute('INSERT INTO '+TABLE+' VALUES '+placeholders,
                 [value for row in rows for value in row])
    assert_exact(conn)
    conn.execute("UPDATE "+TABLE+" SET source='manual_reset:empty', valid=NULL WHERE id % 7=0")
    assert_exact(conn)
    conn.execute('DELETE FROM '+TABLE+' WHERE id % 11=0')
    assert_exact(conn)
    conn.execute("UPDATE "+TABLE+" SET printer_ip='192.0.2.1', color='BLACK', id=id+10000 WHERE id % 5=0")
    assert_exact(conn)
    # Backfill below the unchanged high-water id, then remove oldest/latest.
    insert_many(conn, [(1, '192.0.2.1', 'black', 0, 70, 70, '', 1, '', 'backfill', '', None, 0)])
    assert_exact(conn)
    conn.execute('DELETE FROM '+TABLE+' WHERE id IN (SELECT MIN(id) FROM '+TABLE+' UNION SELECT MAX(id) FROM '+TABLE+')')
    assert_exact(conn)


def test_long_idle_more_than_400_runs_and_upsert(lab):
    conn, _ = lab
    rows = [(i*3+2, '192.0.2.1', 'BLACK' if i%2 else 'black', i,
             i//2 if i<1200 else 599, i//2 if i<1200 else 599,
             None if i%2 else '', 1, None if i%2 else '', str(i), '', i, i)
            for i in range(1600)]
    insert_many(conn, rows)
    assert_exact(conn)
    conn.execute(INSERT+' ON CONFLICT(id) DO UPDATE SET source=EXCLUDED.source, usage_counter=EXCLUDED.usage_counter',
                 (rows[1200][0], '192.0.2.1', 'black', 0, 1, 1, 'reset', 1, '', 'upsert', '', None, 0))
    assert_exact(conn)


def test_rollback_truncate_rebuild_and_version_fallback(lab):
    conn, _ = lab
    insert_many(conn, fixture(7, 150))
    assert_exact(conn)
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.execute('DELETE FROM '+TABLE)
            conn.execute('TRUNCATE '+TABLE)
            raise RuntimeError('abort')
    assert_exact(conn)
    # A partially built/corrupt derived view must never be used if unready.
    with conn.transaction():
        conn.execute('UPDATE yield_run_lab.meta SET ready=false')
        conn.execute('DELETE FROM yield_run_lab.boundaries')
        sql, params = reader_query()
        ref, ref_params = reference_query()
        assert conn.execute(sql, params).fetchall() == conn.execute(ref, ref_params).fetchall()
        rebuild(conn)
    assert_exact(conn)
    conn.execute('UPDATE yield_run_lab.meta SET version=999')
    query, params = reader_query()
    ref, ref_params = reference_query()
    assert conn.execute(query, params).fetchall() == conn.execute(ref, ref_params).fetchall()
    conn.execute('DELETE FROM yield_run_lab.meta')
    assert conn.execute(query, params).fetchall() == conn.execute(ref, ref_params).fetchall()
    with conn.transaction():
        rebuild(conn)
    assert conn.execute('SELECT ready AND version=1 FROM yield_run_lab.meta').fetchone()[0]
    assert_exact(conn)
    conn.execute('TRUNCATE '+TABLE)
    assert_exact(conn)
    insert_many(conn, fixture(31, 20))
    assert_exact(conn)


def test_interrupted_rebuild_and_maintenance_failure_rollback(lab):
    conn, _ = lab
    insert_many(conn, fixture(7, 50))
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.execute('UPDATE yield_run_lab.meta SET ready=false')
            conn.execute('TRUNCATE yield_run_lab.boundaries')
            raise RuntimeError('interrupted build')
    assert_exact(conn)
    # Inject an error in maintenance. The underlying INSERT must not commit.
    conn.execute('ALTER TABLE yield_run_lab.boundaries ADD CONSTRAINT injected_failure CHECK (start_id<>99999)')
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(INSERT, (99999, '192.0.2.99', 'black', 0, 50, 50, 'poll', 1, '', '', '', None, 0))
    assert conn.execute('SELECT COUNT(*) FROM '+TABLE+' WHERE id=99999').fetchone()[0] == 0
    assert_exact(conn)


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_two_writers_same_slot_and_stale_snapshot(lab, isolation):
    conn, uri = lab
    insert_many(conn, [(10, '192.0.2.1', 'black', 10, 70, 70, 'poll', 1, '', '', '', None, 10),
                       (1000, '192.0.2.1', 'black', 1000, 70, 70, 'poll', 1, '', '', '', None, 1000)])
    with psycopg.connect(uri) as first, psycopg.connect(uri) as second:
        for c in (first, second):
            c.execute("SET statement_timeout='5s'")
            c.execute('SET TRANSACTION ISOLATION LEVEL '+isolation)
        # Force second's snapshot before first commits.
        second.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()
        first.execute(INSERT, (20, '192.0.2.1', 'black', 0, 5, 5, 'reset', 1, '', '', '', None, 0))
        entered = threading.Event()

        def write_second():
            entered.set()
            try:
                second.execute(INSERT, (30, '192.0.2.1', 'black', 30, 70, 70, 'poll', 1, '', '', '', None, 30))
                second.commit()
                return 'committed'
            except psycopg.Error as exc:
                second.rollback()
                return exc.sqlstate

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(write_second)
            assert entered.wait(2)
            wait_for_lock(conn, second.info.backend_pid)
            first.commit()
            outcome = pending.result(timeout=6)
        if isolation == 'REPEATABLE READ':
            assert outcome == '40001'
            # Retry the WHOLE transaction, with a new snapshot.
            second.execute(INSERT, (30, '192.0.2.1', 'black', 30, 70, 70, 'poll', 1, '', '', '', None, 30))
            second.commit()
        else:
            assert outcome == 'committed'
    assert_exact(conn)


def test_reader_snapshot_during_uncommitted_slot_move(lab):
    conn, uri = lab
    insert_many(conn, fixture(31, 80))
    sql, params = reader_query()
    before = conn.execute(sql, params).fetchall()
    with psycopg.connect(uri) as writer, psycopg.connect(uri) as reader:
        writer.execute("UPDATE "+TABLE+" SET printer_ip='192.0.2.1', color='BLACK', usage_counter=0 WHERE id % 5=0")
        reader.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        assert reader.execute(sql, params).fetchall() == before
        writer.commit()
        assert reader.execute(sql, params).fetchall() == before
        reader.commit()
    assert_exact(conn)


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_conflicting_bulk_slot_moves_retry_no_partial_commit(lab, isolation):
    conn, uri = lab
    insert_many(conn, fixture(31, 120))
    start = threading.Barrier(2)

    def worker(reverse):
        outcomes = []
        with psycopg.connect(uri) as writer:
            for attempt in range(5):
                try:
                    writer.execute('SET TRANSACTION ISOLATION LEVEL '+isolation)
                    writer.execute("SET LOCAL statement_timeout='5s'")
                    writer.execute("SET LOCAL lock_timeout='2s'")
                    if attempt == 0:
                        start.wait(timeout=3)
                    # Different rows, shared old/new slots, opposite operations.
                    writer.execute('UPDATE '+TABLE+" SET printer_ip=%s, color='BLACK', usage_counter=0 WHERE id %% 2=%s",
                                   ('192.0.2.1' if reverse else '192.0.2.2', int(reverse)))
                    writer.commit()
                    return outcomes + ['committed']
                except psycopg.Error as exc:
                    writer.rollback()
                    outcomes.append(exc.sqlstate)
                    if exc.sqlstate not in ('40001', '40P01'):
                        raise
            raise AssertionError('retry budget exhausted')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (False, True)))
    assert all(r[-1] == 'committed' for r in results)
    assert_exact(conn)


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_deliberate_deadlock_retries_whole_transaction(lab, isolation):
    conn, uri = lab
    base = lambda i, slot: (i, '192.0.2.'+str(slot), 'black', i, 70, 70, 'poll', 1, '', '', '', None, i)
    insert_many(conn, [base(10, 1), base(11, 2)])
    with psycopg.connect(uri) as a, psycopg.connect(uri) as b:
        for c in (a, b):
            c.execute('SET TRANSACTION ISOLATION LEVEL '+isolation)
            c.execute("SET LOCAL statement_timeout='5s'")
        a.execute(INSERT, base(20, 1))
        b.execute(INSERT, base(21, 2))
        barrier = threading.Barrier(2)

        def cross_write(c, row):
            barrier.wait(timeout=3)
            try:
                c.execute(INSERT, row)
                c.commit()
                return 'committed'
            except psycopg.Error as exc:
                c.rollback()
                return exc.sqlstate

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(cross_write, a, base(22, 2))
            two = pool.submit(cross_write, b, base(23, 1))
            outcomes = [one.result(timeout=7), two.result(timeout=7)]
        assert sorted(outcomes) == ['40P01', 'committed']
        # Reapply BOTH writes of the aborted transaction, not only the last one.
        victim, first, last = (a, base(20, 1), base(22, 2)) if outcomes[0]=='40P01' else (b, base(21, 2), base(23, 1))
        victim.execute(INSERT, first)
        victim.execute(INSERT, last)
        victim.commit()
    assert conn.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0] == 6
    assert_exact(conn)


def test_new_slot_created_after_repeatable_read_snapshot(lab):
    conn, uri = lab
    with psycopg.connect(uri) as stale:
        stale.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        stale.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()
        conn.execute(INSERT, (1, '192.0.2.1', 'black', 1, 70, 70, '', 1, '', '', '', None, 1))
        with pytest.raises(psycopg.errors.SerializationFailure):
            stale.execute(INSERT, (2, '192.0.2.1', 'black', 2, 70, 70, '', 1, '', '', '', None, 2))
        stale.rollback()
        stale.execute(INSERT, (2, '192.0.2.1', 'black', 2, 70, 70, '', 1, '', '', '', None, 2))
        stale.commit()
    assert_exact(conn)


def test_refuses_non_test_database(private_server):
    with psycopg.connect(private_server.get_uri(), autocommit=True) as conn:
        with pytest.raises(RuntimeError, match='LOCAL_TEST_DATABASE_REQUIRED'):
            create_source(conn)
        assert conn.execute("SELECT to_regnamespace('yield_run_lab')").fetchone()[0] is None


def test_ready_plan_does_not_scan_raw_history(lab):
    conn, _ = lab
    insert_many(conn, fixture(7, 300))
    sql, params = reader_query()
    plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) '+sql, params).fetchone()[0]

    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from nodes(child)

    raw_scans = [n for n in nodes(plan[0]['Plan'])
                 if n.get('Relation Name') == 'toner_snapshots_v2' and n.get('Actual Loops', 0)>0]
    assert len(raw_scans) == 1
    assert raw_scans[0]['Index Name'] == 'toner_snapshots_v2_pkey'
    assert raw_scans[0]['Actual Rows'] == 1
    assert raw_scans[0]['Actual Loops'] == len(conn.execute(sql, params).fetchall())


def test_committed_bulk_benchmark_restores_logical_input(lab):
    from tools.yield_run_prototype import measure_bulk_mutations
    conn, _ = lab
    insert_many(conn, fixture(7, 500))
    before = conn.execute('SELECT * FROM '+TABLE+' ORDER BY id').fetchall()
    result = measure_bulk_mutations(conn, 1000)
    assert result['batch_rows'] > 0
    assert all(item['wall_ms'] > 0 for item in result['operations'].values())
    assert conn.execute('SELECT * FROM '+TABLE+' ORDER BY id').fetchall() == before
    assert_exact(conn)


def test_segmented_rebuild_covers_all_history_and_reports_no_slot_identity(lab):
    conn, _ = lab
    rows = fixture(7, 800)
    rows += [(10000+i*3, '192.0.2.99', 'BLACK' if i%2 else 'black', i,
              i//2 if i<1200 else 599, i//2 if i<1200 else 599,
              '', 1, '', str(i), '', None, i) for i in range(1700)]
    insert_many(conn, rows)
    expected = conn.execute('SELECT * FROM yield_run_lab.boundaries ORDER BY start_id').fetchall()
    events, calls = [], []
    class RecordingConnection:
        def execute(self, query, params=None):
            if isinstance(query, str) and query.lstrip().startswith('INSERT INTO yield_run_lab.boundaries'):
                calls.append((query, params))
            return conn.execute(query, params)
    with conn.transaction():
        rebuild(RecordingConnection(), progress=lambda phase, info: events.append((phase, info)))
    assert conn.execute('SELECT * FROM yield_run_lab.boundaries ORDER BY start_id').fetchall() == expected
    slots = conn.execute('SELECT COUNT(*) FROM yield_run_lab.slots WHERE last_id IS NOT NULL').fetchone()[0]
    assert len(calls) == slots
    assert all('WHERE printer_ip=%s AND LOWER(color)=%s' in q and 'LIMIT' not in q and len(p)==2 for q,p in calls)
    assert [v['ordinal'] for k,v in events if k=='BUILD_BOUNDARIES_SLOT'] == list(range(1, slots+1))
    assert all(set(v) <= {'ordinal','total'} for _,v in events)
    assert_exact(conn)


def test_segmented_rebuild_interruption_or_slot_limit_rolls_back_atomically(lab):
    conn, _ = lab
    insert_many(conn, fixture(31, 120))
    def interrupt(phase, info):
        if phase=='BUILD_BOUNDARIES_SLOT' and info['ordinal']==2:
            raise RuntimeError('INJECTED_BETWEEN_SLOTS')
    with pytest.raises(RuntimeError, match='INJECTED_BETWEEN_SLOTS'):
        with conn.transaction():
            rebuild(conn, progress=interrupt)
    assert_exact(conn)
    assert conn.execute('SELECT ready FROM yield_run_lab.meta').fetchone()[0]
    with pytest.raises(RuntimeError, match='HISTORY_SLOT_COUNT_NEEDS_REVIEW'):
        with conn.transaction():
            rebuild(conn, max_slots=1)
    assert_exact(conn)

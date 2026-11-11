"""App integration on an owned disposable PostgreSQL, never an operational DSN."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from core.db.connection import ConnectionAdapter
from core.yield_engine import _load_interval_snapshots, _interval_history_query
from core.yield_runs import rebuild_yield_runs
from tests.test_yield_query_lab import fixture, oracle
from tests.test_yield_run_prototype import private_server, psycopg, pgserver

pytestmark = [pytest.mark.pytest_only,
              pytest.mark.skipif(psycopg is None or pgserver is None,
                                 reason='optional local PostgreSQL dependencies not installed')]
ROOT = Path(__file__).resolve().parents[1]


def migration():
    spec = importlib.util.spec_from_file_location('migration_0009', ROOT/'alembic/versions/0009_yield_derived_runs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migrate(uri, method):
    engine = create_engine(uri.replace('postgresql://', 'postgresql+psycopg://'))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration(), method)()
    finally:
        engine.dispose()


@pytest.fixture
def appdb(private_server, monkeypatch):
    name = 'printguard_test_app_' + uuid.uuid4().hex[:10]
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('CREATE DATABASE '+name)
    uri = private_server.get_uri(database=name)
    from tools.yield_run_prototype import BASE_DDL
    raw = psycopg.connect(uri)
    raw.execute(BASE_DDL.replace('yield_run_lab.', ''))
    raw.commit()
    conn = ConnectionAdapter(raw, backend='postgresql')
    monkeypatch.setenv('YIELD_DERIVED_RUNS_ENABLED', '1')
    try:
        yield conn, uri
    finally:
        conn.close()
        with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
            admin.execute('DROP DATABASE '+name)


def insert(conn, rows):
    conn.executemany('INSERT INTO toner_snapshots_v2 VALUES ('+','.join(['?']*13)+')', rows)
    conn.commit()


def assert_exact(conn):
    rows = conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall()
    for options in ({}, {'limit': 1}, {'limit': 3}, {'limit': 0}, {'limit': 1000},
                    {'ip': '192.0.2.1', 'color': 'BLACK'}, {'color': 'cyan'}, {'ip': 'missing'}):
        assert _load_interval_snapshots(conn, **options) == oracle(rows, **options)
    conn.rollback()


def install_build(conn, uri):
    conn.rollback()
    migrate(uri, 'upgrade')
    rebuild_yield_runs(conn)
    conn.commit()


def test_absent_disabled_unready_unknown_and_ready(appdb, monkeypatch):
    conn, uri = appdb
    insert(conn, fixture(7, 1000))
    assert_exact(conn)  # schema absent, savepoint leaves connection usable
    monkeypatch.delenv('YIELD_DERIVED_RUNS_ENABLED')
    assert_exact(conn)  # OFF is default
    monkeypatch.setenv('YIELD_DERIVED_RUNS_ENABLED', '1')
    migrate(uri, 'upgrade')
    assert conn.execute('SELECT ready FROM yield_run_meta').fetchone() is None
    assert_exact(conn)  # deliberately empty summaries, unready: exact history
    rebuild_yield_runs(conn)
    conn.commit()
    assert_exact(conn)
    conn.execute('UPDATE yield_run_meta SET version=999')
    conn.execute('DELETE FROM yield_run_boundaries')
    conn.commit()
    assert_exact(conn)
    rebuild_yield_runs(conn)
    conn.commit()
    assert_exact(conn)


@pytest.mark.parametrize('seed', [7, 31, 101])
def test_historical_bulk_mutations_truncate_rollback_and_rebuild(appdb, seed):
    conn, uri = appdb
    insert(conn, fixture(seed, 1400))
    install_build(conn, uri)
    assert_exact(conn)
    for sql in (
        "UPDATE toner_snapshots_v2 SET source='manual_reset:empty', valid=NULL WHERE id % 7=0",
        'DELETE FROM toner_snapshots_v2 WHERE id % 11=0',
        "UPDATE toner_snapshots_v2 SET printer_ip='192.0.2.1', color='BLACK', id=id+10000 WHERE id % 5=0",
    ):
        conn.execute(sql)
        conn.commit()
        assert_exact(conn)
    insert(conn, [(1, '192.0.2.1', 'black', 0, 70, 70, '', 1, '', 'backfill', '', None, 0)])
    assert_exact(conn)
    conn.execute('TRUNCATE toner_snapshots_v2')
    assert _load_interval_snapshots(conn) == []
    conn.rollback()
    assert_exact(conn)
    conn.execute('TRUNCATE toner_snapshots_v2')
    conn.commit()
    assert_exact(conn)


def test_missing_column_fallback_keeps_callers_earlier_write(appdb):
    conn, uri = appdb
    install_build(conn, uri)
    conn.execute('ALTER TABLE yield_run_meta RENAME COLUMN version TO broken_version')
    conn.commit()
    insert(conn, fixture(7, 100))
    conn.execute("UPDATE toner_snapshots_v2 SET source='before-savepoint' WHERE id=2")
    rows = conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall()
    assert _load_interval_snapshots(conn) == oracle(rows)
    # Savepoint fallback must not roll back UPDATE or poison the transaction.
    assert conn.execute('SELECT source FROM toner_snapshots_v2 WHERE id=2').fetchone()[0] == 'before-savepoint'
    conn.commit()


def test_downgrade_preserves_source_and_reader_falls_back(appdb):
    conn, uri = appdb
    insert(conn, fixture(31, 200))
    install_build(conn, uri)
    original = conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall()
    conn.rollback()
    migrate(uri, 'downgrade')
    assert conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall() == original
    assert_exact(conn)
    assert conn.execute("SELECT to_regclass('yield_run_meta')").fetchone()[0] is None
    conn.rollback()
    migrate(uri, 'upgrade')
    rebuild_yield_runs(conn)
    conn.commit()
    assert_exact(conn)


def test_interrupted_rebuild_is_atomic(appdb):
    conn, uri = appdb
    insert(conn, fixture(7, 500))
    install_build(conn, uri)
    before = conn.execute('SELECT * FROM yield_run_boundaries ORDER BY start_id').fetchall()
    conn.rollback()
    def interrupt(phase, info):
        if phase == 'BUILD_BOUNDARIES_SLOT' and info['ordinal'] == 2:
            raise RuntimeError('injected')
    with pytest.raises(RuntimeError, match='injected'):
        rebuild_yield_runs(conn, progress=interrupt)
    conn.rollback()
    assert conn.execute('SELECT * FROM yield_run_boundaries ORDER BY start_id').fetchall() == before
    assert_exact(conn)


def test_active_plan_does_not_execute_history_windows(appdb):
    conn, uri = appdb
    insert(conn, [(i, '192.0.2.1', 'black', i, i//10, i//10, 'poll', 1, '', str(i), '', None, i)
                  for i in range(1, 1801)])
    install_build(conn, uri)
    from core.yield_runs import reader_query
    sql, params = _interval_history_query(conn)
    sql, params = reader_query(sql, params)
    plan = conn.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+sql, params).fetchone()[0][0]['Plan']
    def walk(node):
        yield node
        for child in node.get('Plans', []):
            yield from walk(child)
    windows = [n for n in walk(plan) if n['Node Type'] == 'WindowAgg']
    assert windows and any(n['Actual Loops'] == 0 for n in windows)
    scans = [n for n in walk(plan) if n.get('Relation Name') == 'toner_snapshots_v2' and n['Actual Loops'] > 0]
    assert scans and all(n['Node Type'] == 'Index Scan' and n['Actual Rows'] <= 1 for n in scans)
    conn.rollback()


def test_custom_app_schema_migration_and_reader(appdb):
    conn, uri = appdb
    conn.execute('CREATE SCHEMA custom_app')
    conn.execute('ALTER TABLE toner_snapshots_v2 SET SCHEMA custom_app')
    conn.commit()
    # Migrate with a non-public current schema, as the repo test harness does.
    engine = create_engine(uri.replace('postgresql://', 'postgresql+psycopg://'))
    try:
        with engine.begin() as connection:
            connection.execute(text('SET search_path=custom_app'))
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()
        conn.execute('SET search_path=custom_app')
        insert(conn, fixture(7, 500))
        rebuild_yield_runs(conn)
        conn.commit()
        assert_exact(conn)
        with engine.begin() as connection:
            connection.execute(text('SET search_path=custom_app'))
            with Operations.context(MigrationContext.configure(connection)):
                migration().downgrade()
        assert_exact(conn)
    finally:
        engine.dispose()


def migrate_chain(uri, revision='head', direction='upgrade'):
    from alembic import command
    from alembic.config import Config
    from unittest.mock import patch
    config = Config(str(ROOT/'alembic.ini'))
    with patch.dict('os.environ', {'ALEMBIC_DB_URL': uri, 'DB_BACKEND': 'sqlite', 'PG_DSN': ''}):
        getattr(command, direction)(config, revision)


@pytest.mark.parametrize("estimator", ["", "v2"])
def test_full_migration_chain_roundtrip_and_real_poll_refill_commit_retry(appdb, monkeypatch, estimator):
    from contextlib import contextmanager
    from core import yield_engine as engine
    from core.db.exceptions import DatabaseError
    conn, uri = appdb
    conn.execute('DROP TABLE toner_snapshots_v2')
    conn.commit()
    migrate_chain(uri)
    rebuild_yield_runs(conn)
    conn.commit()
    @contextmanager
    def app_connection(*, commit=False, **kwargs):
        # Use the actual adapter/context transaction semantics, fresh every retry.
        c = ConnectionAdapter(psycopg.connect(uri), backend='postgresql')
        try:
            yield c
            if commit:
                c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    monkeypatch.setattr(engine, 'db_connection', app_connection)
    monkeypatch.setattr(engine, 'ensure_yield_tables', lambda: None)
    monkeypatch.setenv('YIELD_ESTIMATOR', estimator)
    monkeypatch.setattr('core.db.retry.time.sleep', lambda _: None)
    real_commit = ConnectionAdapter.commit
    writes = {'attempts': 0}
    def fail_once_at_commit(self):
        writes['attempts'] += 1
        if writes['attempts'] == 1:
            # Transaction really inserted a snapshot and mutated local anchors.
            assert self.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0] > 0
            raise DatabaseError('injected commit serialization', sqlstate='40001')
        real_commit(self)
    monkeypatch.setattr(ConnectionAdapter, 'commit', fail_once_at_commit)
    poll = dict(ip='192.0.2.250', color='black', printer_model='Test', cartridge_name='Test',
                level=90, raw_level=90, counters={'total': 100}, device_type='mono',
                timestamp='2026-01-01T00:00:00Z', exact_level=90.5, _ensure_tables=False)
    result = engine.process_cartridge_snapshot(**poll)
    assert result
    assert writes['attempts'] == 2
    assert conn.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0] == 1
    conn.rollback()
    writes['attempts'] = 0
    learned = dict(poll, level=80, raw_level=80, exact_level=80.5,
                   counters={'total': 1100}, timestamp='2026-01-02T00:00:00Z')
    assert engine.process_cartridge_snapshot(**learned)
    assert writes['attempts'] == 2
    assert conn.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM yield_samples WHERE accepted=true').fetchone()[0] == 1
    conn.rollback()
    writes['attempts'] = 0
    result = engine.register_manual_refill(ip=poll['ip'], color='black', printer_model='Test',
        cartridge_name='Test', new_level=100, counters={'total': 1200}, device_type='mono',
        timestamp='2026-01-03T00:00:00Z')
    assert result
    assert writes['attempts'] == 2
    # Failed refill attempt must not duplicate any snapshot/sample/state writes.
    snapshots = conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall()
    assert len(snapshots) == 3
    assert conn.execute('SELECT count(*) FROM cartridge_state').fetchone()[0] == 1
    assert _load_interval_snapshots(conn)  # actual reader and maintained summaries
    conn.rollback()
    engine._EXACT_ANCHOR_CACHE.pop((poll['ip'], 'black'), None)
    # Drop derived state only, ensure source preserved; then install fresh again.
    monkeypatch.setattr(ConnectionAdapter, 'commit', real_commit)
    migrate_chain(uri, '0008', 'downgrade')
    assert conn.execute('SELECT * FROM toner_snapshots_v2 ORDER BY id').fetchall() == snapshots
    assert _load_interval_snapshots(conn)
    conn.rollback()
    migrate_chain(uri)
    rebuild_yield_runs(conn)
    conn.commit()


def test_sqlite_full_chain_0009_noop(tmp_path):
    import sqlite3
    uri = 'sqlite:///'+str(tmp_path/'migrations.db')
    migrate_chain(uri)
    with sqlite3.connect(tmp_path/'migrations.db') as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchone() == ('0009',)
        assert c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'yield_run_%'").fetchall() == []
    migrate_chain(uri, '0008', 'downgrade')
    migrate_chain(uri)


@pytest.mark.parametrize('isolation', ['READ COMMITTED', 'REPEATABLE READ'])
def test_overlapping_writers_real_conflict_and_whole_transaction_retry(appdb, isolation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from core.db.retry import retry_transaction
    from tests.test_yield_run_prototype import wait_for_lock
    conn, uri = appdb
    install_build(conn, uri)
    insert(conn, [(10, '192.0.2.1', 'black', 10, 70, 70, 'poll', 1, '', '', '', None, 10),
                  (1000, '192.0.2.1', 'black', 1000, 70, 70, 'poll', 1, '', '', '', None, 1000)])
    statement = 'INSERT INTO toner_snapshots_v2 VALUES ('+','.join(['?']*13)+')'
    attempts = []
    monkeypatch.setattr('core.db.retry.time.sleep', lambda _: None)
    first = ConnectionAdapter(psycopg.connect(uri), backend='postgresql')
    second = ConnectionAdapter(psycopg.connect(uri), backend='postgresql')
    try:
        first.execute(statement, (20, '192.0.2.1', 'black', 0, 5, 5, 'reset', 1, '', '', '', None, 0))
        def attempt():
            try:
                second.execute("SET LOCAL statement_timeout='5s'")
                second.execute('SET TRANSACTION ISOLATION LEVEL '+isolation)
                second.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()
                attempts.append('started')
                second.execute(statement, (30, '192.0.2.1', 'black', 30, 70, 70, 'poll', 1, '', '', '', None, 30))
                second.commit()
            except Exception:
                second.rollback()
                raise
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(retry_transaction, attempt)
            wait_for_lock(conn.raw_connection, second.raw_connection.info.backend_pid)
            # Release the observer's transaction before validating final state.
            conn.rollback()
            first.commit()
            pending.result(timeout=6)
        assert len(attempts) == (2 if isolation == 'REPEATABLE READ' else 1)
        assert conn.execute('SELECT count(*) FROM toner_snapshots_v2 WHERE id=30').fetchone()[0] == 1
        assert_exact(conn)
    finally:
        first.close()
        second.close()


def test_retention_real_triggers_and_open_transaction_guard(appdb):
    from core.retention_policy import purge_table
    conn, uri = appdb
    install_build(conn, uri)
    insert(conn, fixture(7, 300))
    conn.execute("UPDATE toner_snapshots_v2 SET timestamp='2000-01-01'")
    with pytest.raises(ValueError, match='idle'):
        purge_table(conn, 'toner_snapshots_v2', days=90, batch_size=64)
    assert conn.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0] == 300
    conn.commit()
    assert purge_table(conn, 'toner_snapshots_v2', days=90, batch_size=64) == 300
    assert_exact(conn)


def test_rebuild_refuses_autocommit(appdb):
    conn, uri = appdb
    migrate(uri, 'upgrade')
    conn.raw_connection.autocommit = True
    with pytest.raises(ValueError, match='autocommit=False'):
        rebuild_yield_runs(conn)
    conn.raw_connection.autocommit = False

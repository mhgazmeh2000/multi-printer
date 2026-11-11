"""Validate the exact self-contained copy trial on an owned local cluster."""
import types

import pytest

from tools.build_yield_stage2_block import powershell_block, standalone_python
from tools import yield_run_prototype as prototype
from tests.test_yield_run_prototype import private_server, psycopg, pgserver

pytestmark = [pytest.mark.pytest_only,
              pytest.mark.skipif(psycopg is None or pgserver is None,
                                 reason='optional local PostgreSQL dependencies not installed')]


@pytest.fixture
def source_copy(private_server):
    import uuid
    name = 'printguard_test_stage2_' + uuid.uuid4().hex[:8]
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('CREATE DATABASE '+name)
    with psycopg.connect(private_server.get_uri(database=name), autocommit=True) as conn:
        prototype.create_source(conn)
        prototype.seed(conn, 1200, 6)
        conn.execute('ALTER TABLE yield_run_lab.toner_snapshots_v2 SET SCHEMA public')
        conn.execute('DROP SCHEMA yield_run_lab')
        conn.execute("ALTER TABLE public.toner_snapshots_v2 ADD COLUMN extra_payload TEXT NOT NULL DEFAULT 'private-fixture'")
        conn.execute('ALTER TABLE public.toner_snapshots_v2 ADD COLUMN extra_serial BIGINT GENERATED ALWAYS AS IDENTITY')
        conn.execute('SET default_transaction_read_only=on')
        yield conn
    with psycopg.connect(private_server.get_uri(), autocommit=True) as admin:
        admin.execute('DROP DATABASE '+name)


def generated_module():
    module = types.ModuleType('generated_standalone_stage2')
    exec(compile(standalone_python(), '<exact-standalone-stage2>', 'exec'), module.__dict__)
    return module


@pytest.mark.parametrize('standalone', [False, True])
def test_exact_trial_rolls_back_all_lab_objects_and_preserves_source(source_copy, tmp_path, standalone):
    from tools import yield_copy_stage2
    module = generated_module() if standalone else yield_copy_stage2
    conn = source_copy
    before = conn.execute('SELECT * FROM public.toner_snapshots_v2 ORDER BY id').fetchall()
    sequence = conn.execute("SELECT pg_get_serial_sequence('public.toner_snapshots_v2','extra_serial')").fetchone()[0]
    # Known local fixture identifier, not a user-supplied target.
    serial_before = conn.execute('SELECT last_value FROM '+sequence).fetchone()[0]
    report = {}
    module.run_trial(conn, tmp_path, 'test', report, expected_rows=1200)
    assert report['trial_completed'] and report['lab_schema_rolled_back']
    assert report['exact_ordered_equal'] and report['actual_history_slots']==6
    assert len(report['warm_full_fleet_reads'])==5
    assert len(report['mutations'])==5
    assert report['build_slots_completed']==6
    assert report['build_slots_total']==6
    assert len(report['build_slot_seconds'])==6
    assert report['boundary_build_mode']=='complete_history_per_slot_atomic'
    assert all(item['exact_equal'] for item in report['mutations'].values())
    assert report['mutation_timings_include_commit'] is False
    assert conn.execute('SHOW default_transaction_read_only').fetchone()[0]=='on'
    assert conn.execute("SELECT to_regnamespace('yield_run_lab')").fetchone()[0] is None
    assert conn.execute('SELECT * FROM public.toner_snapshots_v2 ORDER BY id').fetchall()==before
    assert conn.execute('SELECT last_value FROM '+sequence).fetchone()[0]==serial_before


def test_exception_after_install_rolls_back_source_copy_schema_and_triggers(source_copy, tmp_path, monkeypatch):
    from tools import yield_copy_stage2 as module
    original = module.install
    def fail(conn):
        original(conn)
        raise RuntimeError('INJECTED_INSTALL_FAILURE')
    monkeypatch.setattr(module, 'install', fail)
    report = {}
    with pytest.raises(RuntimeError, match='INJECTED_INSTALL_FAILURE'):
        module.run_trial(source_copy, tmp_path, 'fail', report, expected_rows=1200)
    assert report['lab_schema_rolled_back']
    assert source_copy.execute('SELECT COUNT(*) FROM public.toner_snapshots_v2').fetchone()[0]==1200
    assert source_copy.execute('SHOW transaction_read_only').fetchone()[0]=='on'


def test_deadline_cleanup_and_refusal_to_adopt_existing_schema(source_copy, tmp_path):
    from tools import yield_copy_stage2 as module
    report = {}
    with pytest.raises(RuntimeError, match='TOTAL_TEST_TIME_LIMIT'):
        module.run_trial(source_copy, tmp_path, 'deadline', report, expected_rows=1200, total_seconds=0)
    assert report['lab_schema_rolled_back']
    source_copy.execute('BEGIN READ WRITE')
    source_copy.execute('CREATE SCHEMA yield_run_lab')
    source_copy.execute('COMMIT')
    with pytest.raises(RuntimeError, match='LAB_SCHEMA_ALREADY_EXISTS'):
        module.run_trial(source_copy, tmp_path, 'existing', {}, expected_rows=1200)
    assert source_copy.execute("SELECT to_regnamespace('yield_run_lab')").fetchone()[0] is not None


def test_generated_reference_matches_canonical_for_all_filter_shapes():
    module = generated_module()
    for kwargs in ({}, {'ip':'192.0.2.1'}, {'color':'BLACK'},
                   {'ip':'192.0.2.1','color':'CYAN','limit':0}):
        canonical, cp = prototype.reference_query(**kwargs)
        inline, ip = module.reference_query(**kwargs)
        assert canonical.split()==inline.split()
        assert cp==ip
    block = powershell_block()
    assert "'@ | & $Python -" in block
    assert 'from tools' not in standalone_python()
    assert '__file__' not in standalone_python()


def test_sql_failure_records_stage_elapsed_and_effective_timeout(source_copy, tmp_path, monkeypatch):
    from tools import yield_copy_stage2 as module
    def failed_build(conn, *, progress, max_slots):
        progress('BUILD_BOUNDARIES_SLOT', {'ordinal':2, 'total':6})
        conn.execute('SELECT 1/0')
    monkeypatch.setattr(module, 'rebuild', failed_build)
    report = {}
    with pytest.raises(psycopg.errors.DivisionByZero):
        module.run_trial(source_copy, tmp_path, 'sqlfail', report, expected_rows=1200)
    assert report['last_stage']=='BUILD_BOUNDARIES_SLOT'
    assert report['build_slot_ordinal']==2
    assert report['failed_statement_wall_seconds'] >= 0
    assert 0 < report['failed_statement_timeout_ms'] <= 120000
    assert report['lab_schema_rolled_back']

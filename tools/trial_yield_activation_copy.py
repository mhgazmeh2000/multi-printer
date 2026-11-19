"""Approved Windows COPY ONLY: actual activation, reader equality, rollback, disable.

No app/server/collector is started. Source code comes from the verified base ZIP.
All target details are pinned, credentials remain local, and no .env is loaded.
"""
import argparse
import ast
from contextlib import contextmanager, nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import zipfile

ROOT = Path(r'E:\PrintGuard-Test\yield-activation-20261119-085347-ad6696-v2')
BIN = Path(r'E:\pgsql\binaries\bin')
SYSTEM_ID = '7709766716166001212'
PORT = 55439
USER = 'yield_activation_lab'
DB = 'yield_activation_copy'
HEAD = '9f9d9332af40d9d472e0ab624672f0e8b044afaf'


def safe_archive_names(names):
    for name in names:
        parts = name.replace('\\', '/').split('/')
        if name.startswith(('/', '\\')) or ':' in name or '..' in parts or '.env' in parts:
            raise RuntimeError('UNSAFE_SOURCE_ARCHIVE')


def verify_source_archive(archive, expected_bytes):
    """Compare to git archive output, including .gitattributes export conversions."""
    safe_archive_names(archive.namelist())
    with zipfile.ZipFile(io.BytesIO(expected_bytes)) as expected:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected.namelist()):
            raise RuntimeError('SOURCE_ARCHIVE_MEMBERS_MISMATCH')
        for name in names:
            if hashlib.sha256(archive.read(name)).digest() != hashlib.sha256(expected.read(name)).digest():
                raise RuntimeError('SOURCE_ARCHIVE_COMMIT_MISMATCH')


def safe_error_code(exc):
    # Explicit allowlist: no arbitrary exception messages or private paths.
    codes = {
        'WINDOWS_ONLY', 'TRIAL_ALREADY_ATTEMPTED_DO_NOT_REPEAT',
        'COPY_REPORT_MISMATCH', 'COPY_AUTH_TARGET_MISMATCH', 'COPY_ALREADY_RUNNING',
        'SOURCE_DIRECTORY_ALREADY_EXISTS', 'UNSAFE_SOURCE_ARCHIVE',
        'SOURCE_ARCHIVE_MEMBERS_MISMATCH', 'SOURCE_ARCHIVE_COMMIT_MISMATCH',
        'COPY_IDENTITY_MISMATCH', 'CLUSTER_START_FAILED', 'TRIAL_BUDGET_EXCEEDED',
        'REVISION_MISMATCH', 'SOURCE_COUNT_MISMATCH', 'SLOT_COUNT_NEEDS_REVIEW',
        'READY_MARKER_MISMATCH', 'ORDERED_OUTPUT_MISMATCH',
        'TRIGGER_UPDATE_OUTPUT_MISMATCH', 'DISABLE_TRIGGERS_REMAIN',
        'DISABLE_STATE_REMAINS', 'DISABLED_FALLBACK_MISMATCH', 'SOURCE_SIGNATURE_CHANGED',
        'BASELINE_RETRY_STATE_MISMATCH', 'COPY_NOT_INACTIVE_BEFORE_ANALYZE',
        'LOCK_RETRY_STATE_MISMATCH', 'COPY_NOT_INACTIVE_BEFORE_LOCK_TRIAL',
    }
    return str(exc) if type(exc) is RuntimeError and str(exc) in codes else 'UNCLASSIFIED'


def safe_sqlstate(exc):
    state = getattr(exc, 'sqlstate', None)
    return state if isinstance(state, str) and len(state) == 5 and state.isascii() and state.isalnum() else 'unknown'


def validate_baseline_retry(previous):
    # Only the observed, stopped, pre-activation first-read failure is eligible.
    if not (previous.get('status') == 'FAILED'
            and previous.get('cluster_stopped') is True
            and previous.get('activation_committed') is False
            and previous.get('disable_committed') is False
            and previous.get('baseline_seconds') == []
            and previous.get('slot_count') == 51
            and previous.get('source_commit') == HEAD
            and str(previous.get('system_identifier')) == SYSTEM_ID
            and previous.get('error_type') == 'DatabaseOperationalError'):
        raise RuntimeError('BASELINE_RETRY_STATE_MISMATCH')


def validate_lock_retry(previous):
    if not (previous.get('status') == 'FAILED'
            and previous.get('cluster_stopped') is True
            and previous.get('activation_committed') is False
            and previous.get('disable_committed') is False
            and previous.get('analyze_committed') is True
            and previous.get('phase') == 'ACTIVATION'
            and previous.get('sqlstate') == '55P03'
            and previous.get('slot_count') == 51
            and previous.get('source_commit') == HEAD
            and str(previous.get('system_identifier')) == SYSTEM_ID
            and len(previous.get('baseline_seconds', [])) == 4):
        raise RuntimeError('LOCK_RETRY_STATE_MISMATCH')


def maintenance_stage(sql):
    text = ' '.join(sql.upper().split())
    if text.startswith('LOCK TABLE TONER_SNAPSHOTS_V2 '):
        return 'LOCK_SOURCE_EXCLUSIVE'
    if text.startswith('CREATE INDEX '):
        return 'CREATE_SOURCE_INDEX'
    if text.startswith('CREATE TRIGGER '):
        return 'CREATE_TRIGGER'
    if text.startswith('CREATE FUNCTION ') or text.startswith('CREATE OR REPLACE FUNCTION '):
        return 'CREATE_FUNCTION'
    if text.startswith('INSERT INTO YIELD_RUN_BOUNDARIES'):
        return 'BUILD_BOUNDARIES'
    if text.startswith('COMMIT'):
        return 'COMMIT'
    return 'OTHER_MAINTENANCE_SQL'


def blocker_sample(diag, pid):
    # Do not select query, application_name, addresses, usernames or row payloads.
    rows = diag.execute('''SELECT p.pid, a.backend_type, a.state
        FROM unnest(pg_blocking_pids(%s)) AS p(pid)
        LEFT JOIN pg_stat_activity a ON a.pid=p.pid''', (pid,)).fetchall()
    types = {'client backend', 'autovacuum worker', 'parallel worker', 'background worker'}
    states = {'active', 'idle', 'idle in transaction', 'idle in transaction (aborted)', 'disabled'}
    return [{'pid': int(row[0]),
             'backend_type': row[1] if row[1] in types else 'other_or_unavailable',
             'state': row[2] if row[2] in states else 'other_or_unavailable'} for row in rows]


@contextmanager
def observe_activation(conn, report, diagnostic_factory):
    """Read-only bounded observer; never cancels, terminates or retries SQL."""
    diag = diagnostic_factory()  # production caller verifies live copy identity
    stop = threading.Event()
    original = conn.execute
    report['activation_lock_observer'] = {'status': 'STARTING', 'samples': []}
    evidence = report['activation_lock_observer']
    current = {'stage': 'STARTING'}
    pid = conn.raw_connection.info.backend_pid
    def watched(sql, *args, **kwargs):
        # Preserve omitted params vs explicit None: psycopg treats these
        # differently when processing literal percent signs in function DDL.
        stage = maintenance_stage(sql)
        current['stage'] = stage
        report['activation_last_sql_stage'] = stage
        try:
            return original(sql, *args, **kwargs)
        except Exception:
            report['activation_failed_sql_stage'] = stage
            raise
    def worker():
        started = time.monotonic()
        try:
            evidence['status'] = 'RUNNING'
            while not stop.is_set() and time.monotonic() - started < 610:
                blockers = blocker_sample(diag, pid)
                if blockers and len(evidence['samples']) < 32:
                    sample = {'stage': current['stage'], 'blockers': blockers}
                    if sample not in evidence['samples']:
                        evidence['samples'].append(sample)
                stop.wait(.1)
            evidence['status'] = 'STOPPED' if stop.is_set() else 'BUDGET_REACHED'
        except Exception as exc:
            evidence['status'] = 'FAILED'
            evidence['sqlstate'] = safe_sqlstate(exc)
            evidence['error_type'] = type(exc).__name__
        finally:
            diag.close()
    thread = threading.Thread(target=worker, name='copy-lock-observer', daemon=True)
    conn.execute = watched
    try:
        thread.start()
        yield
    finally:
        conn.execute = original
        stop.set()
        if thread.ident is not None:
            thread.join(5)
            if thread.is_alive():
                evidence['status'] = 'STOP_NOT_CONFIRMED'
        else:
            diag.close()


def analyze_copy(conn, report):
    """Bounded planner-statistics refresh; only called after live copy identity check."""
    conn.execute("SET LOCAL statement_timeout = '120s'")
    conn.execute("SET LOCAL lock_timeout = '1s'")
    active = conn.execute("""SELECT EXISTS(SELECT 1 FROM yield_run_meta)
        OR to_regclass('yield_runs_snapshot_neighbor') IS NOT NULL
        OR EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='toner_snapshots_v2'::regclass
                  AND tgname IN ('yield_runs_lock','yield_runs_insert','yield_runs_delete',
                                 'yield_runs_update','yield_runs_truncate'))""").fetchone()[0]
    if active:
        raise RuntimeError('COPY_NOT_INACTIVE_BEFORE_ANALYZE')
    report['planner_rows_before_analyze'] = conn.execute(
        "SELECT reltuples FROM pg_class WHERE oid='toner_snapshots_v2'::regclass").fetchone()[0]
    print('COPY_ANALYZE_BEGIN (same 120 second statement cap)', flush=True)
    start = time.perf_counter()
    conn.execute('ANALYZE public.toner_snapshots_v2')
    conn.commit()
    report['analyze_committed'] = True
    report['analyze_seconds'] = round(time.perf_counter() - start, 4)
    conn.execute("SET LOCAL statement_timeout = '120s'")
    report['planner_rows_after_analyze'] = conn.execute(
        "SELECT reltuples FROM pg_class WHERE oid='toner_snapshots_v2'::regclass").fetchone()[0]
    conn.rollback()


def validate_report(report, auth):
    if not (report.get('status') == 'RESTORE_VERIFIED'
            and report.get('cluster_stopped') is True
            and report.get('source_commit') == HEAD
            and str(report.get('system_identifier')) == SYSTEM_ID
            and report.get('snapshot_rows') == 2256043):
        raise RuntimeError('COPY_REPORT_MISMATCH')
    if (auth.get('host'), auth.get('port'), auth.get('user'), auth.get('dbname')) != (
            '127.0.0.1', PORT, USER, DB) or not auth.get('password'):
        raise RuntimeError('COPY_AUTH_TARGET_MISMATCH')


def assert_identity(raw):
    row = raw.execute("""SELECT host(inet_server_addr()), inet_server_port(),
        current_database(), current_user, current_setting('data_directory'),
        (SELECT system_identifier::text FROM pg_control_system())""").fetchone()
    expected_dir = os.path.normcase(os.path.realpath(ROOT / 'data'))
    if (row[:4] != ('127.0.0.1', PORT, DB, USER) or str(row[5]) != SYSTEM_ID
            or os.path.normcase(os.path.realpath(row[4])) != expected_dir):
        raise RuntimeError('COPY_IDENTITY_MISMATCH')
    raw.rollback()


def history_factory(source):
    tree = ast.parse(source.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == '_interval_history_query')
    from typing import Optional
    namespace = {'Optional': Optional, 'BACKEND_POSTGRESQL': 'postgresql'}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_interval_history_query']


def execute_trial(conn, source, report, *, expected_rows=2256043, activation_observer=None):
    """Real maintenance library and reader, not prototype SQL; caller closes DB."""
    from core.yield_run_maintenance import enable_yield_runs, disable_yield_runs
    from core.yield_runs import load_derived_snapshots
    history = history_factory(source / 'core/yield_engine.py')
    deadline = time.monotonic() + 1500
    def budget():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('TRIAL_BUDGET_EXCEEDED')
        conn.execute("SELECT set_config('statement_timeout', ?, true)",
                     (str(max(1, int(min(120, remaining) * 1000))) + 'ms',))
        conn.execute("SET LOCAL lock_timeout = '1s'")
    def read(options, enabled):
        budget()
        os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '1' if enabled else '0'
        query, params = history(conn, **options)
        start = time.perf_counter()
        rows = load_derived_snapshots(conn, query, params, **options)
        if rows is None:
            rows = conn.execute(query, params).fetchall()
        return rows, round(time.perf_counter() - start, 4)
    def signature():
        budget()
        return tuple(conn.execute('SELECT count(*), min(id), max(id) FROM toner_snapshots_v2').fetchone())

    budget()
    if conn.execute('SELECT version_num FROM alembic_version').fetchall() != [('0009',)]:
        raise RuntimeError('REVISION_MISMATCH')
    original = signature()
    if original[0] != expected_rows:
        raise RuntimeError('SOURCE_COUNT_MISMATCH')
    keys = conn.execute('SELECT DISTINCT printer_ip, LOWER(color) FROM toner_snapshots_v2 ORDER BY 1,2').fetchall()
    if not 1 <= len(keys) <= 256:
        raise RuntimeError('SLOT_COUNT_NEEDS_REVIEW')
    report['slot_count'] = len(keys)
    options = [{}, {'ip': keys[0][0]}, {'ip': keys[-1][0], 'color': keys[-1][1]}, {'color': keys[0][1]}]
    references = []
    report['baseline_seconds'] = []
    for ordinal, choice in enumerate(options, 1):
        report['phase'] = 'REFERENCE_READ_' + str(ordinal)
        print(report['phase'], flush=True)
        rows, seconds = read(choice, False)
        references.append(rows)
        report['baseline_seconds'].append(seconds)
        conn.rollback()
    report['phase'] = 'ACTIVATION'
    print('ACTIVATION_BEGIN (budget 600 seconds)', flush=True)
    os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '0'
    start = time.perf_counter()
    context = activation_observer(conn, report) if activation_observer else nullcontext()
    with context:
        enable_yield_runs(conn, approved=True, timeout_seconds=600)
        report['activation_last_sql_stage'] = 'COMMIT'
        conn.commit()
        report['activation_committed'] = True
    report['activation_seconds'] = round(time.perf_counter() - start, 4)
    budget()
    if conn.execute('SELECT version, ready FROM yield_run_meta WHERE singleton').fetchone() != (1, True):
        raise RuntimeError('READY_MARKER_MISMATCH')
    report['derived_bytes'] = conn.execute("SELECT sum(pg_total_relation_size(t::regclass))::bigint FROM unnest(ARRAY['yield_run_meta','yield_run_slots','yield_run_boundaries','yield_runs_snapshot_neighbor']) AS t").fetchone()[0]
    report['boundary_rows'] = conn.execute('SELECT count(*) FROM yield_run_boundaries').fetchone()[0]
    conn.rollback()
    report['derived_seconds'] = []
    for ordinal, (choice, expected) in enumerate(zip(options, references), 1):
        report['phase'] = 'DERIVED_COMPARE_' + str(ordinal)
        print(report['phase'], flush=True)
        rows, seconds = read(choice, True)
        if rows != expected:
            raise RuntimeError('ORDERED_OUTPUT_MISMATCH')
        report['derived_seconds'].append(seconds)
        conn.rollback()
    report['exact_ordered_read_equal'] = True
    # Exercise real UPDATE triggers without committing any source mutation.
    report['phase'] = 'TRIGGER_ROLLBACK_CHECK'
    print(report['phase'], flush=True)
    budget()
    last_id = original[2]
    key = conn.execute('SELECT printer_ip, LOWER(color) FROM toner_snapshots_v2 WHERE id=?', (last_id,)).fetchone()
    conn.execute('UPDATE toner_snapshots_v2 SET usage_counter=COALESCE(usage_counter,0)+1 WHERE id=?', (last_id,))
    changed_options = {'ip': key[0], 'color': key[1]}
    old_rows, _ = read(changed_options, False)
    new_rows, _ = read(changed_options, True)
    if old_rows != new_rows:
        raise RuntimeError('TRIGGER_UPDATE_OUTPUT_MISMATCH')
    conn.rollback()
    report['update_trigger_rollback_equal'] = True
    report['phase'] = 'DISABLE'
    print('DISABLE_BEGIN', flush=True)
    os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '0'
    disable_yield_runs(conn, approved=True, timeout_seconds=120)
    conn.commit()
    report['disable_committed'] = True
    budget()
    if conn.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid='toner_snapshots_v2'::regclass AND tgname IN ('yield_runs_lock','yield_runs_insert','yield_runs_delete','yield_runs_update','yield_runs_truncate')").fetchone()[0] != 0:
        raise RuntimeError('DISABLE_TRIGGERS_REMAIN')
    if conn.execute("SELECT to_regclass('yield_runs_snapshot_neighbor') IS NOT NULL OR EXISTS(SELECT 1 FROM yield_run_meta)").fetchone()[0]:
        raise RuntimeError('DISABLE_STATE_REMAINS')
    conn.rollback()
    report['phase'] = 'DISABLED_FALLBACK_READ'
    fallback, seconds = read({}, True)
    if fallback != references[0]:
        raise RuntimeError('DISABLED_FALLBACK_MISMATCH')
    report['disabled_fallback_equal'] = True
    report['disabled_fallback_seconds'] = seconds
    if signature() != original:
        raise RuntimeError('SOURCE_SIGNATURE_CHANGED')
    conn.rollback()
    os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '0'
    report['source_count_min_max_unchanged'] = True
    report['scope'] = 'history_reader_only_not_full_api_or_all_trigger_types'


def main(argv=None):
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_ONLY')
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--retry-baseline-after-analyze', action='store_true',
                        help='Explicit one-time retry of the stopped first-read failure on this copy only')
    modes.add_argument('--diagnose-activation-lock', action='store_true',
                       help='One observed attempt after the verified 55P03 activation failure')
    args = parser.parse_args(argv)
    suffix = '-lock-observed' if args.diagnose_activation_lock else ('-analyzed' if args.retry_baseline_after_analyze else '')
    if args.diagnose_activation_lock:
        previous = json.loads((ROOT / 'activation-trial-analyzed-report.json').read_text(encoding='utf-8'))
        validate_lock_retry(previous)
    if args.retry_baseline_after_analyze:
        previous = json.loads((ROOT / 'activation-trial-report.json').read_text(encoding='utf-8'))
        validate_baseline_retry(previous)
    report_path = ROOT / ('activation-trial' + suffix + '-report.json')
    if report_path.exists():
        raise RuntimeError('TRIAL_ALREADY_ATTEMPTED_DO_NOT_REPEAT')
    restore = json.loads((ROOT / 'restore-report.json').read_text(encoding='utf-8'))
    auth = json.loads((ROOT / 'lab-auth.json').read_text(encoding='utf-8'))
    validate_report(restore, auth)
    if (ROOT / 'data/postmaster.pid').exists():
        raise RuntimeError('COPY_ALREADY_RUNNING')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', PORT))
    source = ROOT / ('trial-source' + suffix)
    if source.exists():
        raise RuntimeError('SOURCE_DIRECTORY_ALREADY_EXISTS')
    expected_archive = subprocess.run(
        ['git', 'archive', '--format=zip', HEAD],
        capture_output=True, check=True, timeout=60).stdout
    with zipfile.ZipFile(ROOT / 'source.zip') as archive:
        verify_source_archive(archive, expected_archive)
        archive.extractall(source)
    # No settings from the production working directory or parent .env files.
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '0'
    os.environ['DB_BACKEND'] = 'postgresql'
    for key in tuple(os.environ):
        if key.upper().startswith('PG'):
            del os.environ[key]
    os.chdir(source)
    sys.path.insert(0, str(source))
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    import psycopg
    from psycopg.conninfo import make_conninfo
    os.environ['PG_DSN'] = make_conninfo(**auth)
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'COMSPEC', 'SYSTEMDRIVE'}}
    report = {'status': 'IN_PROGRESS', 'system_identifier': SYSTEM_ID, 'source_commit': HEAD,
              'cluster_stopped': False, 'activation_committed': False, 'disable_committed': False,
              'analyze_requested': args.retry_baseline_after_analyze, 'analyze_committed': False,
              'phase': 'COPY_START', 'lock_observation_requested': args.diagnose_activation_lock}
    report_path.write_text(json.dumps(report), encoding='utf-8')
    def ctl(args, logfile):
        with (ROOT / logfile).open('xb') as output:
            return subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(ROOT / 'data')] + args,
                env=env, stdout=output, stderr=subprocess.STDOUT, timeout=90).returncode
    raw = None
    attempted = False
    try:
        attempted = True
        if ctl(['-l', str(ROOT / ('trial' + suffix + '-postgresql.log')), '-o', '-h 127.0.0.1 -p 55439 -c max_connections=12', '-w', '-t', '60', 'start'], 'trial' + suffix + '-start.log'):
            raise RuntimeError('CLUSTER_START_FAILED')
        raw = psycopg.connect(**auth, connect_timeout=5, options='-c timezone=UTC -c statement_timeout=120000')
        assert_identity(raw)
        from core.db.connection import ConnectionAdapter
        conn = ConnectionAdapter(raw, backend='postgresql')
        if args.retry_baseline_after_analyze:
            report['phase'] = 'COPY_ANALYZE'
            analyze_copy(conn, report)
        observer = None
        if args.diagnose_activation_lock:
            raw.execute("SET LOCAL statement_timeout = '5s'")
            raw.execute("SET LOCAL lock_timeout = '1s'")
            installed = raw.execute("""SELECT EXISTS(SELECT 1 FROM yield_run_meta)
                OR to_regclass('yield_runs_snapshot_neighbor') IS NOT NULL
                OR EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='toner_snapshots_v2'::regclass
                    AND tgname IN ('yield_runs_lock','yield_runs_insert','yield_runs_delete',
                                   'yield_runs_update','yield_runs_truncate'))""").fetchone()[0]
            raw.rollback()
            if installed:
                raise RuntimeError('COPY_NOT_INACTIVE_BEFORE_LOCK_TRIAL')
            def diagnostic_factory():
                diagnostic = psycopg.connect(**auth, connect_timeout=5,
                    options='-c timezone=UTC -c statement_timeout=500 -c default_transaction_read_only=on')
                try:
                    assert_identity(diagnostic)
                    diagnostic.autocommit = True
                    return diagnostic
                except Exception:
                    diagnostic.close()
                    raise
            observer = lambda connection, evidence: observe_activation(connection, evidence, diagnostic_factory)
        execute_trial(conn, source, report, activation_observer=observer)
        report['phase'] = 'COMPLETE'
        report['status'] = 'COPY_HISTORY_TRIAL_PASSED'
    except Exception as exc:
        report['status'] = 'FAILED'
        report['error_type'] = type(exc).__name__
        report['sqlstate'] = safe_sqlstate(exc)
        report['error_code'] = safe_error_code(exc)
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                report['connection_close_failed'] = True
        try:
            if attempted and ctl(['-m', 'fast', '-w', '-t', '60', 'stop'], 'trial' + suffix + '-stop.log') == 0:
                status = subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(ROOT / 'data'), 'status'], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                report['cluster_stopped'] = status.returncode == 3 and not (ROOT / 'data/postmaster.pid').exists()
        except Exception:
            report['cluster_stopped'] = False
        if not report['cluster_stopped']:
            report['status'] = 'FAILED_CLEANUP_NOT_CONFIRMED'
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    # This report contains no row values, IPs, user data or credentials.
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['status'] == 'COPY_HISTORY_TRIAL_PASSED' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('COPY_TRIAL_PRECHECK_FAILED_TYPE=' + type(exc).__name__)
        print('COPY_TRIAL_PRECHECK_FAILED_CODE=' + safe_error_code(exc))
        print('COPY_TRIAL_PRECHECK_SQLSTATE=' + safe_sqlstate(exc))
        print('Stop; do not repeat or share private files.')
        raise SystemExit(1)

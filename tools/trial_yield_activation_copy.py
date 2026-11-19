"""Approved Windows COPY ONLY: actual activation, reader equality, rollback, disable.

No app/server/collector is started. Source code comes from the verified base ZIP.
All target details are pinned, credentials remain local, and no .env is loaded.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
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


def execute_trial(conn, source, report, *, expected_rows=2256043):
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
        print('REFERENCE_READ_' + str(ordinal), flush=True)
        rows, seconds = read(choice, False)
        references.append(rows)
        report['baseline_seconds'].append(seconds)
        conn.rollback()
    print('ACTIVATION_BEGIN (budget 600 seconds)', flush=True)
    os.environ['YIELD_DERIVED_RUNS_ENABLED'] = '0'
    start = time.perf_counter()
    enable_yield_runs(conn, approved=True, timeout_seconds=600)
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
        print('DERIVED_COMPARE_' + str(ordinal), flush=True)
        rows, seconds = read(choice, True)
        if rows != expected:
            raise RuntimeError('ORDERED_OUTPUT_MISMATCH')
        report['derived_seconds'].append(seconds)
        conn.rollback()
    report['exact_ordered_read_equal'] = True
    # Exercise real UPDATE triggers without committing any source mutation.
    print('TRIGGER_ROLLBACK_CHECK', flush=True)
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


def main():
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_ONLY')
    report_path = ROOT / 'activation-trial-report.json'
    if report_path.exists():
        raise RuntimeError('TRIAL_ALREADY_ATTEMPTED_DO_NOT_REPEAT')
    restore = json.loads((ROOT / 'restore-report.json').read_text(encoding='utf-8'))
    auth = json.loads((ROOT / 'lab-auth.json').read_text(encoding='utf-8'))
    validate_report(restore, auth)
    if (ROOT / 'data/postmaster.pid').exists():
        raise RuntimeError('COPY_ALREADY_RUNNING')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', PORT))
    source = ROOT / 'trial-source'
    if source.exists():
        raise RuntimeError('SOURCE_DIRECTORY_ALREADY_EXISTS')
    with zipfile.ZipFile(ROOT / 'source.zip') as archive:
        safe_archive_names(archive.namelist())
        # Verify every archived payload against the production base commit.
        for name in archive.namelist():
            if name.endswith('/'):
                continue
            blob = subprocess.run(['git', 'show', HEAD + ':' + name], capture_output=True, check=True, timeout=30).stdout
            if hashlib.sha256(blob).digest() != hashlib.sha256(archive.read(name)).digest():
                raise RuntimeError('SOURCE_ARCHIVE_COMMIT_MISMATCH')
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
              'cluster_stopped': False, 'activation_committed': False, 'disable_committed': False}
    report_path.write_text(json.dumps(report), encoding='utf-8')
    def ctl(args, logfile):
        with (ROOT / logfile).open('xb') as output:
            return subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(ROOT / 'data')] + args,
                env=env, stdout=output, stderr=subprocess.STDOUT, timeout=90).returncode
    raw = None
    attempted = False
    try:
        attempted = True
        if ctl(['-l', str(ROOT / 'trial-postgresql.log'), '-o', '-h 127.0.0.1 -p 55439 -c max_connections=12', '-w', '-t', '60', 'start'], 'trial-start.log'):
            raise RuntimeError('CLUSTER_START_FAILED')
        raw = psycopg.connect(**auth, connect_timeout=5, options='-c timezone=UTC -c statement_timeout=120000')
        assert_identity(raw)
        from core.db.connection import ConnectionAdapter
        execute_trial(ConnectionAdapter(raw, backend='postgresql'), source, report)
        report['status'] = 'COPY_HISTORY_TRIAL_PASSED'
    except Exception as exc:
        report['status'] = 'FAILED'
        report['error_type'] = type(exc).__name__
        # Only our fixed uppercase codes, never raw driver errors/credentials.
        code = str(exc)
        report['error_code'] = code if type(exc) is RuntimeError and code.isupper() and code.replace('_', '').isalnum() and len(code) < 80 else 'UNCLASSIFIED'
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                report['connection_close_failed'] = True
        try:
            if attempted and ctl(['-m', 'fast', '-w', '-t', '60', 'stop'], 'trial-stop.log') == 0:
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
        print('Stop; do not repeat or share private files.')
        raise SystemExit(1)

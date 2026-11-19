"""One-shot restore to a newly owned Windows cluster; never reads production config."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timezone

HEAD = '9f9d9332af40d9d472e0ab624672f0e8b044afaf'
DIGEST = '2817a4bd0a871c9e9bb99f0356438fa4fd12b4ddbd0ece63c31e62c7da94cc44'
ROOT = Path(r'E:\PrintGuard-Test\yield-activation-20261119-085347-ad6696-v2')
BACKUP = Path(r'E:\PrintGuard-Backups\pre-pr22-20261118-095743-ae1373')
BIN = Path(r'E:\pgsql\binaries\bin')
PORT = 55439
USER = 'yield_activation_lab'
DB = 'yield_activation_copy'


def check_plan(plan):
    if not (plan.get('purpose') == 'isolated-yield-activation-trial-only'
            and plan.get('source_commit') == HEAD
            and plan.get('dump_sha256') == DIGEST
            and plan.get('lab_port') == PORT
            and plan.get('production_activation_authorized') is False
            and str(plan.get('backup_folder', '')).lower() == str(BACKUP).lower()):
        raise RuntimeError('PLAN_MISMATCH')


def child_environment(password):
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'COMSPEC', 'SYSTEMDRIVE', 'PROCESSOR_ARCHITECTURE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PGHOST='127.0.0.1', PGPORT=str(PORT), PGUSER=USER,
               PGDATABASE=DB, PGPASSWORD=password, PGCONNECT_TIMEOUT='5')
    return env


def verify_identity(conn, data):
    row = conn.execute("""SELECT host(inet_server_addr()), inet_server_port(),
        current_database(), current_user, current_setting('data_directory'),
        (SELECT system_identifier::text FROM pg_control_system())""").fetchone()
    if (row[:4] != ('127.0.0.1', PORT, DB, USER)
            or os.path.normcase(os.path.realpath(row[4])) != os.path.normcase(os.path.realpath(data))):
        raise RuntimeError('LAB_IDENTITY_MISMATCH')
    return row[5]


def main():
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_ONLY')
    plan = json.loads((ROOT / 'trial-plan.json').read_text(encoding='utf-8-sig'))
    check_plan(plan)
    data = ROOT / 'data'
    if data.exists() or (ROOT / 'restore-report.json').exists() or (ROOT / 'lab-auth.json').exists():
        raise RuntimeError('LAB_ALREADY_USED_DO_NOT_REPEAT')
    dump = BACKUP / 'database.dump'
    digest = hashlib.sha256()
    with dump.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if dump.stat().st_size != 36364916 or digest.hexdigest() != DIGEST:
        raise RuntimeError('BACKUP_HASH_MISMATCH')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', PORT))
    # Remove inherited libpq targeting before psycopg is imported. No .env load.
    for key in tuple(os.environ):
        if key.upper().startswith('PG'):
            del os.environ[key]
    import psycopg
    password = secrets.token_urlsafe(36)
    env = child_environment(password)
    password_file = ROOT / 'initdb-password.txt'
    with password_file.open('x', encoding='utf-8', newline='\n') as output:
        output.write(password + '\n')
    report = {'source_commit': HEAD, 'dump_sha256': DIGEST, 'port': PORT,
              'data_directory': str(data), 'database': DB, 'username': USER,
              'scope': 'new_owned_copy_only', 'cluster_stopped': False,
              'status': 'IN_PROGRESS', 'created_at': datetime.now(timezone.utc).isoformat()}
    attempted = False
    stage = 'INITDB'
    failed = None
    def run(name, args, logfile, timeout):
        with (ROOT / logfile).open('xb') as output:
            result = subprocess.run([str(BIN / (name + '.exe'))] + args,
                                    env=env, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout)
        if result.returncode:
            raise RuntimeError('TOOL_NONZERO')
    try:
        print('LAB_INITDB_BEGIN', flush=True)
        run('initdb', ['-D', str(data), '-U', USER, '--auth=scram-sha-256',
            '--pwfile=' + str(password_file), '--encoding=UTF8', '--locale=C'], 'initdb.log', 120)
        # Preserve lab-only credentials even if a later verification fails.
        with (ROOT / 'lab-auth.json').open('x', encoding='utf-8') as output:
            json.dump({'host': '127.0.0.1', 'port': PORT, 'user': USER,
                       'dbname': DB, 'password': password}, output)
        stage = 'START'
        attempted = True
        run('pg_ctl', ['-D', str(data), '-l', str(ROOT / 'postgresql.log'),
            '-o', '-h 127.0.0.1 -p 55439 -c max_connections=12',
            '-w', '-t', '60', 'start'], 'start.log', 90)
        with psycopg.connect(host='127.0.0.1', port=PORT, user=USER, password=password,
                             dbname='postgres', connect_timeout=5, autocommit=True) as conn:
            # Check owned data directory BEFORE creating any database.
            actual = conn.execute('SHOW data_directory').fetchone()[0]
            if os.path.normcase(os.path.realpath(actual)) != os.path.normcase(os.path.realpath(data)):
                raise RuntimeError('WRONG_DATA_DIRECTORY')
            conn.execute('CREATE DATABASE yield_activation_copy TEMPLATE template0')
        stage = 'RESTORE'
        print('LAB_RESTORE_BEGIN (maximum 600 seconds)', flush=True)
        run('pg_restore', ['--no-password', '--exit-on-error', '--no-owner',
            '--no-privileges', '--dbname=' + DB, str(dump)], 'restore.log', 600)
        stage = 'VERIFY'
        with psycopg.connect(host='127.0.0.1', port=PORT, user=USER, password=password,
                             dbname=DB, connect_timeout=5) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '60s'")
            report['system_identifier'] = verify_identity(conn, data)
            revisions = conn.execute('SELECT version_num FROM alembic_version').fetchall()
            if revisions != [('0009',)]:
                raise RuntimeError('RESTORED_REVISION_MISMATCH')
            for table in ('logs', 'users', 'printer_counters', 'cartridge_state',
                          'toner_snapshots_v2', 'yield_run_meta', 'yield_run_slots', 'yield_run_boundaries'):
                if conn.execute('SELECT to_regclass(%s) IS NOT NULL', (table,)).fetchone()[0] is not True:
                    raise RuntimeError('RESTORED_TABLE_MISSING')
            report['snapshot_rows'] = conn.execute('SELECT count(*) FROM toner_snapshots_v2').fetchone()[0]
            report['database_revision'] = '0009'
        report['status'] = 'RESTORE_VERIFIED'
    except Exception as exc:
        failed = type(exc).__name__
        codes = {'LAB_IDENTITY_MISMATCH', 'WRONG_DATA_DIRECTORY',
                 'RESTORED_REVISION_MISMATCH', 'RESTORED_TABLE_MISSING', 'TOOL_NONZERO'}
        report.update(status='FAILED', error_stage=stage, error_type=failed,
                      error_code=str(exc) if isinstance(exc, RuntimeError) and str(exc) in codes else 'UNCLASSIFIED')
    finally:
        report['cluster_stopped'] = not attempted
        if attempted:
            try:
                run('pg_ctl', ['-D', str(data), '-m', 'fast', '-w', '-t', '60', 'stop'], 'stop.log', 90)
                # Reading pg_ctl status does not connect to or stop another cluster.
                result = subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(data), 'status'],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                report['cluster_stopped'] = result.returncode == 3 and not (data / 'postmaster.pid').exists()
            except Exception:
                report['cluster_stopped'] = False
        password_file.unlink(missing_ok=True)
        if not report['cluster_stopped']:
            report['status'] = 'FAILED_CLEANUP_NOT_CONFIRMED'
        (ROOT / 'restore-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('LAB_CLUSTER_STOPPED=' + str(report['cluster_stopped']), flush=True)
    if report['status'] != 'RESTORE_VERIFIED':
        print('LAB_RESTORE_FAILED; do not repeat; keep private logs local', flush=True)
        print('FAILED_STAGE=' + report.get('error_stage', 'CLEANUP'), flush=True)
        print('FAILED_TYPE=' + str(report.get('error_type', 'cleanup')), flush=True)
        print('FAILED_CODE=' + str(report.get('error_code', 'cleanup')), flush=True)
        return 1
    print('RESTORED_SNAPSHOT_ROWS=' + str(report['snapshot_rows']), flush=True)
    print('LAB_SYSTEM_IDENTIFIER=' + str(report['system_identifier']), flush=True)
    print('LAB_RESTORE_OK', flush=True)
    print('LAB_FOLDER=' + str(ROOT), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('LAB_PRECHECK_FAILED_TYPE=' + type(exc).__name__)
        print('Stop; do not repeat or send private logs.')
        raise SystemExit(1)

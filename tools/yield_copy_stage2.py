"""Stage-2 runner for the explicitly approved isolated Windows copy.

The distributed inline block embeds canonical prototype SQL/helpers; this module
is also exercised against an owned local PostgreSQL database before distribution.
All lab DDL/data live in ONE transaction, rolled back even after success. No
source rows, source indexes, source sequences, database settings or service are
changed. Mutation timings exclude durable commit; not production write p95.
"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.pq import TransactionStatus
from psycopg import sql

from tools.yield_run_prototype import COLUMNS, install, rebuild, reader_query, reference_query

ROOT = Path(r'E:\PrintGuard-Test\restore-20261110T110905Z-389986ce')
DATA = ROOT / 'data'
SYSTEM_ID = '7706498255580821636'
PORT = 55432
USER = 'yield_restore_admin'
DB = 'printguard_test_yield'
CTL = Path('E:/pgsql/binaries/bin/pg_ctl.exe')
EXPECTED_ROWS = 2126300


def with_ids(query, derived=False):
    if derived:
        needle = ') SELECT ' + ', '.join(COLUMNS) + ' FROM ('
        replacement = ') SELECT snapshot_id, ' + ', '.join(COLUMNS) + ' FROM ('
    else:
        needle = 'SELECT s.printer_ip, s.color'
        replacement = 'SELECT s.id AS snapshot_id, s.printer_ip, s.color'
    if query.count(needle) != 1:
        raise RuntimeError('QUERY_TEMPLATE_CHANGED')
    return query.replace(needle, replacement)


def run_trial(conn, root, attempt, report, expected_rows=EXPECTED_ROWS, total_seconds=600):
    """Caller has verified target. Owns and rolls back exactly one transaction."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError('CONNECTION_NOT_IDLE')
    if conn.execute("SELECT to_regnamespace('yield_run_lab')").fetchone()[0] is not None:
        raise RuntimeError('LAB_SCHEMA_ALREADY_EXISTS_NO_ACTION_TAKEN')
    deadline = time.monotonic() + total_seconds
    stage = 'BEGIN_COPY_TRIAL'
    report.update(lab_schema_rolled_back=False, mutation_timings_include_commit=False,
                  real_copy_equality_tested=False, deployment_ready=False)
    begun = False

    def execute(query, params=None, cap=30, label=None):
        nonlocal stage
        if label:
            stage = label
            report['last_stage'] = stage
            print(stage, flush=True)
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise RuntimeError('TOTAL_TEST_TIME_LIMIT')
        timeout_ms = max(1, int(min(cap, remaining) * 1000))
        conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
        started = time.perf_counter()
        try:
            return conn.execute(query, params)
        except BaseException:
            report['failed_statement_wall_seconds'] = round(time.perf_counter()-started, 4)
            report['failed_statement_timeout_ms'] = timeout_ms
            raise

    def fetch(query, params):
        cursor = execute(query, params)
        result = []
        while batch := cursor.fetchmany(1024):
            result.extend(batch)
            if len(result) > 100000:
                raise RuntimeError('ENDPOINT_OUTPUT_SAFETY_LIMIT')
        return result

    def reference(key):
        query, params = reference_query(ip=key[0], color=key[1], limit=400)
        return fetch(with_ids(query), params)

    def derived(key=None):
        kwargs = dict(ip=key[0], color=key[1]) if key else {}
        query, params = reader_query(limit=400, **kwargs)
        return fetch(with_ids(query, derived=True), params)

    def verify_after_change(keys, baseline):
        changed = dict(baseline)
        for key in keys:
            ref = reference(key)
            if ref != derived(key):
                raise RuntimeError('MUTATED_SLOT_OUTPUT_DIFFERENCE')
            changed[key] = ref
        expected = sorted((row for rows in changed.values() for row in rows), key=lambda r: r[0])
        if derived() != expected:
            raise RuntimeError('MUTATED_GLOBAL_OUTPUT_DIFFERENCE')

    def probe(label, statement, params, keys=None, baseline=None):
        execute('SAVEPOINT stage2_probe')
        try:
            start = time.perf_counter()
            execute(statement, params, label=label)
            elapsed = round((time.perf_counter()-start)*1000, 3)
            if baseline is not None:
                verify_after_change(keys, baseline)
            return {'wall_ms_excluding_commit': elapsed,
                    'exact_equal': True if baseline is not None else None}
        finally:
            # Even timeout/error aborts only this savepoint before outer cleanup.
            conn.execute('ROLLBACK TO SAVEPOINT stage2_probe')
            conn.execute('RELEASE SAVEPOINT stage2_probe')

    try:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ WRITE')
        begun = True
        execute("SET LOCAL lock_timeout='1s'")
        if execute("""SELECT t.spcname FROM pg_database d JOIN pg_tablespace t
            ON t.oid=d.dattablespace WHERE d.datname=current_database()""").fetchone()[0] != 'pg_default':
            raise RuntimeError('TEST_DATABASE_TABLESPACE_NEEDS_REVIEW')
        execute("SET LOCAL default_tablespace=''")
        execute("SET LOCAL temp_tablespaces=''")
        cols = execute("""SELECT a.attname, t.typname FROM pg_attribute a
            JOIN pg_type t ON t.oid=a.atttypid
            WHERE a.attrelid='public.toner_snapshots_v2'::regclass
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",
                       label='CHECK_SOURCE_TYPES').fetchall()
        types = dict(cols)
        wanted = dict.fromkeys(('printer_ip', 'color', 'source', 'reject_reason', 'timestamp', 'cartridge_key'), 'text')
        wanted.update(dict.fromkeys(('usage_counter','toner_level','raw_level','valid','full_color','print_total'), 'int4'))
        wanted['id'] = 'int8'
        report['source_loader_column_types'] = {key: types.get(key) for key in wanted}
        if any(types.get(k) != v for k, v in wanted.items()):
            raise RuntimeError('SOURCE_COLUMN_TYPES_NEED_REVIEW')
        if execute("SELECT relkind FROM pg_class WHERE oid='public.toner_snapshots_v2'::regclass").fetchone()[0] != 'r':
            raise RuntimeError('SOURCE_MUST_BE_ORDINARY_TABLE')
        if execute('SELECT COUNT(*) FROM public.toner_snapshots_v2').fetchone()[0] != expected_rows:
            raise RuntimeError('RESTORED_SOURCE_ROW_COUNT_CHANGED')
        report['source_index_count'] = execute("SELECT COUNT(*) FROM pg_index WHERE indrelid='public.toner_snapshots_v2'::regclass").fetchone()[0]
        execute('CREATE SCHEMA yield_run_lab', label='CREATE_ROLLBACK_ONLY_LAB')
        # No source defaults, identity sequences, check expressions, foreign keys
        # or triggers are copied. All source columns and their types are retained.
        execute('CREATE TABLE yield_run_lab.toner_snapshots_v2 (LIKE public.toner_snapshots_v2 INCLUDING STORAGE)')
        start = time.perf_counter()
        execute('INSERT INTO yield_run_lab.toner_snapshots_v2 SELECT * FROM public.toner_snapshots_v2',
                cap=120, label='COPY_REAL_HISTORY_MAX_120S')
        report['copy_seconds'] = round(time.perf_counter()-start, 4)
        execute('ALTER TABLE yield_run_lab.toner_snapshots_v2 ADD PRIMARY KEY(id)', cap=120)
        execute('CREATE INDEX idx_toner_snapshots_v2_ip_color_ts ON yield_run_lab.toner_snapshots_v2(printer_ip,color,timestamp)', cap=120)
        execute('ANALYZE yield_run_lab.toner_snapshots_v2', cap=120)
        if execute('SELECT COUNT(*) FROM yield_run_lab.toner_snapshots_v2').fetchone()[0] != expected_rows:
            raise RuntimeError('LAB_COPY_ROW_COUNT_DIFFERENCE')
        report['clone_base_bytes'] = execute("SELECT pg_total_relation_size('yield_run_lab.toner_snapshots_v2')").fetchone()[0]
        selected = execute('SELECT id, printer_ip, LOWER(color) FROM yield_run_lab.toner_snapshots_v2 ORDER BY id DESC LIMIT 1').fetchone()
        if not selected:
            raise RuntimeError('SOURCE_EMPTY')
        row_id, ip, color = selected
        key = (ip, color)
        update = """UPDATE yield_run_lab.toner_snapshots_v2 SET source='__yield_stage2_probe__'
            WHERE id IN (SELECT id FROM yield_run_lab.toner_snapshots_v2
                WHERE printer_ip=%s AND LOWER(color)=%s ORDER BY id DESC LIMIT 64)"""
        report['baseline_update_up_to64'] = probe('BASELINE_UPDATE_ROLLED_BACK', update, key)
        start = time.perf_counter()
        execute('SELECT 1', cap=120, label='INSTALL_SCHEMA_AND_NEIGHBOR_INDEX')
        class BoundedSetup:
            def execute(self, query, params=None):
                return execute(query, params, cap=120)
        setup = BoundedSetup()
        install(setup)
        report['boundary_build_mode'] = 'complete_history_per_slot_atomic'
        report['build_slots_completed'] = 0
        slot_started = None
        def build_progress(phase, info):
            nonlocal stage, slot_started
            if slot_started is not None:
                report['build_slot_seconds'].append(round(time.perf_counter()-slot_started, 4))
                report['build_slots_completed'] += 1
                slot_started = None
            stage = phase
            report['last_stage'] = phase
            if 'total' in info:
                report['build_slots_total'] = info['total']
            if 'ordinal' in info:
                report['build_slot_ordinal'] = info['ordinal']
                slot_started = time.perf_counter()
            print(phase+' '+json.dumps(info), flush=True)
        report['build_slot_seconds'] = []
        rebuild(setup, progress=build_progress, max_slots=256)
        report['install_build_seconds'] = round(time.perf_counter()-start, 4)
        execute('ANALYZE yield_run_lab.slots')
        execute('ANALYZE yield_run_lab.boundaries')
        report['boundary_rows'] = execute('SELECT COUNT(*) FROM yield_run_lab.boundaries').fetchone()[0]
        keys = execute('SELECT printer_ip, slot_color FROM yield_run_lab.slots WHERE last_id IS NOT NULL ORDER BY printer_ip,slot_color').fetchall()
        report['actual_history_slots'] = len(keys)
        if not 1 <= len(keys) <= 256:
            raise RuntimeError('HISTORY_SLOT_COUNT_NEEDS_REVIEW')
        report['lab_relation_bytes'] = {name: execute('SELECT pg_total_relation_size(%s)',
            ('yield_run_lab.'+name,)).fetchone()[0] for name in ('toner_snapshots_v2','boundaries','slots')}
        baseline = {}
        durations = []
        endpoints = 0
        execute('SELECT 1', label='EXACT_COMPARE_ALL_SLOTS_MAX_30S_EACH')
        for ordinal, slot in enumerate(keys, 1):
            report['comparison_slot_ordinal'] = ordinal
            start = time.perf_counter()
            ref = reference(slot)
            durations.append(time.perf_counter()-start)
            if ref != derived(slot):
                raise RuntimeError('REAL_COPY_SLOT_OUTPUT_DIFFERENCE')
            baseline[slot] = ref
            endpoints += len(ref)
            if endpoints > 100000:
                raise RuntimeError('ENDPOINT_OUTPUT_SAFETY_LIMIT')
            print('COMPARED SLOT '+str(ordinal)+'/'+str(len(keys)), flush=True)
        expected = sorted((row for rows in baseline.values() for row in rows), key=lambda r: r[0])
        if derived() != expected:
            raise RuntimeError('REAL_COPY_GLOBAL_ORDER_OR_COLUMN_DIFFERENCE')
        report.update(real_copy_equality_tested=True, exact_ordered_equal=True,
                      endpoint_rows=len(expected), reference_slot_seconds_max=round(max(durations), 4))
        report['warm_full_fleet_reads'] = []
        execute('SELECT 1', label='FIVE_WARM_FULL_FLEET_SELECTS_MAX_30S_EACH')
        for _ in range(5):
            start = time.perf_counter()
            output = derived()
            elapsed = round(time.perf_counter()-start, 4)
            if output != expected:
                raise RuntimeError('REPEATED_OUTPUT_DIFFERENCE')
            report['warm_full_fleet_reads'].append(elapsed)
        report['proposed_read_goal_met'] = all(t<=2 for t in report['warm_full_fleet_reads'])
        query, params = reader_query()
        planned = execute('EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) '+query, params,
                          label='ONE_ACTUAL_PLAN_MAX_30S').fetchone()[0]
        (root/('stage2-plan-'+attempt+'.json')).write_text(json.dumps(planned, indent=2), encoding='utf-8')
        scans = []
        def visit(node):
            if node.get('Relation Name') == 'toner_snapshots_v2' and node.get('Actual Loops',0)>0:
                scans.append({k:node[k] for k in ('Node Type','Index Name','Actual Rows','Actual Loops') if k in node})
            for child in node.get('Plans',[]):
                visit(child)
        visit(planned[0]['Plan'])
        report['active_snapshot_scans'] = scans
        report['server_reader_execution_ms'] = planned[0].get('Execution Time')
        report['mutations'] = {}
        report['mutations']['update_up_to64'] = probe('DERIVED_UPDATE_ROLLED_BACK', update, key, [key], baseline)
        report['mutations']['delete_latest'] = probe('DELETE_PROBE_ROLLED_BACK',
            'DELETE FROM yield_run_lab.toner_snapshots_v2 WHERE id=%s', (row_id,), [key], baseline)
        other = ('__yield_stage2_probe_slot__', 'black')
        report['mutations']['move_slot'] = probe('MOVE_SLOT_PROBE_ROLLED_BACK',
            'UPDATE yield_run_lab.toner_snapshots_v2 SET printer_ip=%s,color=%s WHERE id=%s',
            (*other, row_id), [key, other], baseline)
        minimum, maximum = execute('SELECT MIN(id),MAX(id) FROM yield_run_lab.toner_snapshots_v2').fetchone()
        if minimum <= -(2**63) or maximum >= 2**63-1:
            raise RuntimeError('NO_SAFE_PROBE_ID')
        names = [name for name, _ in cols]
        expressions = [sql.Placeholder() if name=='id' else sql.Identifier(name) for name in names]
        insert = sql.SQL('INSERT INTO yield_run_lab.toner_snapshots_v2 SELECT {} FROM yield_run_lab.toner_snapshots_v2 WHERE id=%s').format(sql.SQL(',').join(expressions))
        for label, value in (('append', maximum+1), ('historical_backfill', minimum-1)):
            report['mutations'][label] = probe(label.upper()+'_PROBE_ROLLED_BACK', insert, (value,row_id), [key], baseline)
        if derived() != expected:
            raise RuntimeError('SAVEPOINT_ROLLBACK_OUTPUT_DIFFERENCE')
        report['trial_completed'] = True
    finally:
        if begun:
            conn.rollback()
            if conn.info.transaction_status != TransactionStatus.IDLE:
                raise RuntimeError('OUTER_TRANSACTION_NOT_ROLLED_BACK')
            report['lab_schema_rolled_back'] = conn.execute("SELECT to_regnamespace('yield_run_lab') IS NULL").fetchone()[0]
            if not report['lab_schema_rolled_back']:
                raise RuntimeError('LAB_SCHEMA_ROLLBACK_NOT_VERIFIED')


def windows_main():
    stage = 'PRECHECK'
    conn = None
    owned = False
    stopped = None
    error = None
    attempt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]
    report = {'scope':'approved isolated copy only; rollback-only lab schema',
              'deployment_ready':False, 'production_changed':False}
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith('PG')}
    for key in tuple(os.environ):
        if key.upper().startswith('PG'):
            os.environ.pop(key,None)

    def command(args, filename):
        with (ROOT/filename).open('wb') as stream:
            subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                           stderr=stream, check=True, timeout=45)

    try:
        if os.name != 'nt':
            raise RuntimeError('WINDOWS_REQUIRED')
        info = json.loads((ROOT/'test-connection.json').read_text(encoding='utf-8-sig'))
        restored = json.loads((ROOT/'restore-report-lockfix.json').read_text(encoding='utf-8-sig'))
        if (info.get('host')!='127.0.0.1' or info.get('port')!=PORT
            or info.get('database')!=DB or info.get('user')!=USER
            or str(info.get('system_identifier'))!=SYSTEM_ID
            or os.path.normcase(os.path.realpath(info['data_directory']))!=os.path.normcase(os.path.realpath(DATA))
            or restored.get('status')!='RESTORE_TEST_PASSED'
            or str(restored.get('system_identifier'))!=SYSTEM_ID):
            raise RuntimeError('TEST_IDENTITY_MISMATCH')
        status = subprocess.run([str(CTL),'-D',str(DATA),'status'], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if status.returncode==0:
            stopped=False
            raise RuntimeError('TEST_SERVER_ALREADY_RUNNING_NO_ACTION_TAKEN')
        if status.returncode!=3:
            raise RuntimeError('CANNOT_VERIFY_SERVER_STOPPED')
        stopped=True
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:
                sock.bind(('127.0.0.1',PORT))
            except OSError:
                raise RuntimeError('TEST_PORT_UNAVAILABLE') from None
        for path in (ROOT, DATA, DATA/'pg_wal'):
            if Path(os.path.realpath(path)).drive.upper() != 'E:':
                raise RuntimeError('TEST_STORAGE_MUST_REMAIN_ON_E')
        if shutil.disk_usage('E:\\').free<20*1024**3:
            raise RuntimeError('INSUFFICIENT_SPACE_ON_E')
        stage='READ_TEST_CREDENTIAL'
        password=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop'; $s=ConvertTo-SecureString "
            "(Get-Content -LiteralPath $env:YIELD_DPAPI_FILE -Raw); "
            "$c=New-Object System.Management.Automation.PSCredential('test',$s); "
            "$c.GetNetworkCredential().Password"],
            env=dict(env,YIELD_DPAPI_FILE=str(ROOT/'test-password.dpapi')),
            stderr=subprocess.DEVNULL,timeout=30,text=True).strip()
        if not password:
            raise RuntimeError('TEST_CREDENTIAL_UNAVAILABLE')
        stage='START_ISOLATED_SERVER'
        owned=True
        stopped=False
        command([str(CTL),'-D',str(DATA),'-l',str(ROOT/'postgresql.log'),'-w','-t','30','start'],
                'stage2-start-'+attempt+'.log')
        stage='VERIFY_ACTUAL_TARGET'
        conn=psycopg.connect(host='127.0.0.1',hostaddr='127.0.0.1',port=PORT,
            user=USER,password=password,dbname=DB,sslmode='disable',connect_timeout=5,
            application_name='yield_copy_stage2_rollback_trial',autocommit=True,
            options='-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=1000')
        del password
        actual=conn.execute('SHOW data_directory').fetchone()[0]
        if (os.path.normcase(os.path.realpath(actual))!=os.path.normcase(os.path.realpath(DATA))
            or str(conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])!=SYSTEM_ID
            or conn.execute('SHOW port').fetchone()[0]!=str(PORT)
            or conn.execute('SELECT current_database()').fetchone()[0]!=DB
            or conn.execute('SHOW transaction_read_only').fetchone()[0]!='on'
            or conn.execute('SHOW server_version_num').fetchone()[0]!='170005'):
            raise RuntimeError('CONNECTED_TARGET_MISMATCH')
        if conn.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND backend_type='client backend'").fetchone()[0]:
            raise RuntimeError('OTHER_TEST_CLIENTS_PRESENT')
        report['settings']={name:conn.execute('SHOW '+name).fetchone()[0] for name in (
            'server_version','shared_buffers','work_mem','max_parallel_workers_per_gather','default_transaction_read_only')}
        stage='RUN_ROLLBACK_ONLY_TRIAL'
        run_trial(conn,ROOT,attempt,report)
    except BaseException as exc:
        primary=str(getattr(getattr(exc,'diag',None),'message_primary','') or '').lower()
        error={'stage':report.get('last_stage',stage),'error_type':type(exc).__name__,
               'sqlstate':getattr(exc,'sqlstate',None),
               'reason':str(exc) if isinstance(exc,RuntimeError) and re.fullmatch('[A-Z_]+',str(exc)) else None,
               'cancellation_cause':('statement_timeout' if 'statement timeout' in primary else
                    'lock_timeout' if 'lock timeout' in primary else 'not_classified')}
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException:
                error=error or {'stage':'CLOSE_CONNECTION','error_type':'CloseFailed'}
        if owned:
            try:
                command([str(CTL),'-D',str(DATA),'-m','fast','-w','-t','30','stop'],
                        'stage2-stop-'+attempt+'.log')
                stopped=True
            except BaseException:
                stopped=False
    report['isolated_server_stopped']=stopped
    report['error']=error
    report['status']=('STAGE2_TRIAL_COMPLETED' if not error and stopped is True
                      and report.get('lab_schema_rolled_back') else 'STAGE2_TRIAL_FAILED')
    path=ROOT/('stage2-summary-'+attempt+'.json')
    if ROOT.is_dir():
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)
    print('REPORT FILE: '+str(path),flush=True)
    return int(report['status']!='STAGE2_TRIAL_COMPLETED')


if __name__=='__main__':
    raise SystemExit(windows_main())

"""Bounded request diagnostics must not alter results, queries, or disclose inputs."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from core import request_timing as timing


class RequestTimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.clock = patch.object(timing, 'perf_counter', side_effect=lambda: self.now)
        self.clock.start()
        self.logger = patch.object(timing.log, 'warning')
        self.warning = self.logger.start()

    def tearDown(self):
        self.logger.stop()
        self.clock.stop()
        self.assertIsNone(timing._current.get())

    def report(self):
        return json.loads(self.warning.call_args.args[1])

    def test_fast_request_and_untraced_stage_are_quiet(self):
        marker = object()
        with timing.timing_stage('outside_request'):
            pass
        @timing.trace_slow_request('constant_operation')
        def run():
            with timing.timing_stage('work'):
                self.now += 0.01
            return marker
        self.assertIs(run(), marker)
        self.warning.assert_not_called()

    def test_slow_summary_aggregates_stages_without_arguments_or_result(self):
        @timing.trace_slow_request('api_printers')
        def run(secret, **kwargs):
            for _ in range(2):
                with timing.timing_stage('yield_history'):
                    self.now += 3
            return secret
        self.assertEqual(run('private-token', sql='private-sql'), 'private-token')
        self.warning.assert_called_once()
        self.assertEqual(self.report(), {
            'operation': 'api_printers', 'total_ms': 6000.0,
            'failed': False, 'stages_ms': {'yield_history': 6000.0},
        })
        self.assertNotIn('private', str(self.warning.call_args))

    def test_exception_identity_and_context_cleanup_without_message_disclosure(self):
        failure = RuntimeError('password-do-not-log')
        @timing.trace_slow_request('api_printers')
        def run():
            with timing.timing_stage('serialize'):
                self.now += 6
                raise failure
        with self.assertRaises(RuntimeError) as result:
            run()
        self.assertIs(result.exception, failure)
        self.assertTrue(self.report()['failed'])
        self.assertEqual(self.report()['stages_ms'], {'serialize': 6000.0})
        self.assertNotIn('password', str(self.warning.call_args))

    def test_nested_trace_restores_parent(self):
        @timing.trace_slow_request('child')
        def child():
            with timing.timing_stage('child_stage'):
                self.now += 3
        @timing.trace_slow_request('parent')
        def parent():
            child()
            with timing.timing_stage('parent_stage'):
                self.now += 3
        parent()
        self.warning.assert_called_once()
        self.assertEqual(self.report()['operation'], 'parent')
        self.assertEqual(self.report()['stages_ms'], {'parent_stage': 3000.0})

    def test_lock_wait_excludes_protected_work_and_releases_on_exception(self):
        lock = Mock()
        lock.acquire.side_effect = lambda: setattr(self, 'now', self.now + 2)
        @timing.trace_slow_request('api_printers')
        def run():
            with timing.timed_lock(lock, 'data_lock_wait'):
                self.now += 4
                raise ValueError()
        with self.assertRaises(ValueError):
            run()
        lock.acquire.assert_called_once_with()
        lock.release.assert_called_once_with()
        self.assertEqual(self.report()['stages_ms'], {'data_lock_wait': 2000.0})

    def test_concurrent_request_contexts_are_isolated(self):
        barrier = threading.Barrier(2)
        @timing.trace_slow_request('api_printers')
        def run(name):
            with timing.timing_stage(name):
                barrier.wait(timeout=5)
            return tuple(timing._current.get())
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(run, 'one')
            two = pool.submit(run, 'two')
            self.assertEqual(one.result(timeout=10), ('one',))
            self.assertEqual(two.result(timeout=10), ('two',))

    def test_real_api_preserves_payload_and_reports_slow_yield_stage(self):
        from flask import Flask
        from core import store
        from web.routes import printers
        app = Flask(__name__)
        app.register_blueprint(printers.bp)
        def slow_yield_status():
            with timing.timing_stage('yield_history'):
                self.now += 6
            return []
        with patch.object(printers, 'user_allowed_offices', return_value=[]), \
             patch.object(printers, 'allowed_printer_ips', return_value=[]), \
             patch.object(store, 'printer_data', {}), \
             patch.object(store, 'PRINTERS', []), \
             patch.object(store._prev, 'warm_many') as warm, \
             patch('core.yield_engine.get_yield_status', side_effect=slow_yield_status):
            response = app.test_client().get('/api/printers')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['printers'], [])
        self.assertEqual(response.get_json()['meta']['total'], 0)
        warm.assert_called_once_with([])
        self.warning.assert_called_once()
        report = self.report()
        self.assertEqual(report['stages_ms']['yield_history'], 6000.0)
        self.assertEqual(report['stages_ms']['yield_status'], 6000.0)
        for key in ('snapshot_and_permissions', 'data_lock_wait', 'printers_lock_wait',
                    'previous_cache', 'metadata', 'serialize'):
            self.assertIn(key, report['stages_ms'])

    def test_real_yield_timings_do_not_change_result_or_add_queries(self):
        import tempfile
        from tests.golden_harness import isolated_db
        from core import yield_engine
        with tempfile.TemporaryDirectory() as tmp, isolated_db(tmp):
            yield_engine.ensure_yield_tables()
            from contextlib import contextmanager
            yield_engine.process_cartridge_snapshot(
                ip='192.0.2.77', color='black', printer_model='HP LaserJet',
                cartridge_name='Black', level=70, raw_level=70,
                counters={'print_total': 1000, 'black_white': 1000}, device_type='mono',
            )
            real_connection = yield_engine.db_connection
            statements = []
            @contextmanager
            def observed_connection():
                with real_connection() as conn:
                    with patch.object(conn, 'execute', wraps=conn.execute) as execute:
                        yield conn
                        statements.append(execute.call_args_list)
            with patch.object(yield_engine, 'db_connection', observed_connection):
                expected = yield_engine.get_yield_status()
                @timing.trace_slow_request('api_printers')
                def run():
                    result = yield_engine.get_yield_status()
                    self.now += 6
                    return result
                self.assertTrue(expected)
                self.assertEqual(run(), expected)
            self.assertEqual(len(statements), 2)
            self.assertEqual(statements[0], statements[1])
            stages = self.report()['stages_ms']
            for key in ('yield_connection', 'yield_state_query', 'yield_profile_query',
                        'yield_history', 'yield_estimation'):
                self.assertIn(key, stages)

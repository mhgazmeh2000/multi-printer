"""Allowlisted summaries must never reproduce private SQL or log prefixes."""
import json
import unittest

from tools.yield_stage2_log_summary import summarize


class Stage2LogSummaryTests(unittest.TestCase):
    def log(self, statement):
        return ('2026-11-11 09:34:01.234 UTC [123] ERROR:  canceling statement due to statement timeout\n'
                '2026-11-11 09:34:01.234 UTC [123] STATEMENT:  '+statement+'\n'
                '2026-11-11 09:34:02.111 UTC [1] LOG:  database system is shut down\n')

    def test_boundary_build_and_no_private_sql_or_prefix_leak(self):
        text = self.log("INSERT INTO yield_run_lab.boundaries SELECT 'sensitive-value', 'private-user';")
        text = text.replace('UTC [123]', 'UTC private-prefix [123]')
        result = summarize(text)
        self.assertEqual(result['statement_category'], 'build_boundaries_window')
        self.assertTrue(result['exact_statement_within_batch_known'])
        encoded = json.dumps(result)
        for private in ('sensitive-value', 'private-user', 'private-prefix', 'INSERT'):
            self.assertNotIn(private, encoded)

    def test_installer_batch_is_not_misidentified_as_index_statement(self):
        result = summarize(self.log('-- installer\nCREATE TABLE yield_run_lab.meta(x int);\n'
                                    'CREATE INDEX run_lab_snapshot_neighbor ON private_table(x);'))
        self.assertEqual(result['statement_category'], 'install_batch_including_neighbor_index')
        self.assertFalse(result['exact_statement_within_batch_known'])

    def test_latest_timeout_and_same_pid_statement(self):
        text = self.log('INSERT INTO yield_run_lab.slots SELECT 1;')
        text += self.log('CREATE INDEX run_lab_snapshot_neighbor ON private_table(x);')
        self.assertEqual(summarize(text)['statement_category'], 'create_neighbor_index')

    def test_missing_error_or_statement_has_no_fabricated_category(self):
        self.assertFalse(summarize('arbitrary private text')['timeout_found'])
        only = self.log('SELECT private_value;').split('STATEMENT:')[0]
        self.assertFalse(summarize(only)['statement_found'])
        self.assertEqual(summarize(self.log('SELECT private_value;'))['statement_category'], 'not_identified')

    def test_different_pid_statement_is_not_attributed_to_error(self):
        text = self.log('INSERT INTO yield_run_lab.slots SELECT 1;')
        text = text.replace('UTC [123] STATEMENT', 'UTC [999] STATEMENT')
        self.assertFalse(summarize(text)['statement_found'])

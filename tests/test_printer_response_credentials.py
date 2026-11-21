"""HTTP role matrix for polled/unpolled printer credentials, without real polling."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config.settings as settings
from core import database, store
from models import User
from web import create_app


class PrinterResponseCredentialsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for context in (
            patch.object(settings, 'DB_PATH', str(Path(tmp.name) / 'credentials.db')),
            patch.dict(os.environ, {'DB_BACKEND': 'sqlite'}),
            patch.object(store, 'PRINTERS', [
                {'ip': '192.0.2.1', 'name': 'Polled', 'group': 'allowed', 'community': 'config-secret'},
                {'ip': '192.0.2.2', 'name': 'Pending', 'group': 'allowed', 'community': 'pending-secret'},
                {'ip': '192.0.2.3', 'name': 'Other office', 'group': 'denied', 'community': 'denied-secret'},
            ]),
            patch.object(store, 'printer_data', {'192.0.2.1': {
                'ip': '192.0.2.1', 'name': 'Polled', 'online': True, 'community': 'polled-secret',
                'device': {'snmp_community': 'nested-secret', 'model': 'Test model'},
                'counters': {'total': 123}, 'diagnostics': [{'SNMPCommunity': 'list-secret'}],
            }}),
            patch.object(store._prev, 'warm_many'), patch.object(store._prev, 'get', return_value={}),
            patch('core.yield_engine.get_yield_status', return_value=[]),
        ):
            context.start()
            self.addCleanup(context.stop)
        database.init_db()
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app.test_client()
        self.users = {role: User.create('credential_' + role, role + '@example.invalid',
                                       'Testing-only-123!', role=role, is_verified=True)
                      for role in ('admin', 'manager', 'viewer')}

    def login(self, role):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.users[role].id)
            session['_fresh'] = True

    def test_non_admin_list_and_detail_strip_credentials_without_mutating_store(self):
        original = copy.deepcopy(store.printer_data)
        config = copy.deepcopy(store.PRINTERS)
        for role in ('viewer', 'manager'):
            self.login(role)
            for endpoint in ('/api/printers', '/api/printer/192.0.2.1'):
                with self.subTest(role=role, endpoint=endpoint):
                    response = self.client.get(endpoint)
                    self.assertEqual(response.status_code, 200)
                    text = response.get_data(as_text=True)
                    self.assertNotIn('-secret', text)
                    self.assertNotIn('community', text.lower())
            printers = self.client.get('/api/printers').get_json()['printers']
            pending = next(row for row in printers if row['ip'] == '192.0.2.2')
            self.assertIsNone(pending['online'])
            self.assertTrue(pending['pending_initial_poll'])
        self.assertEqual(store.printer_data, original)
        self.assertEqual(store.PRINTERS, config)

    def test_admin_retains_existing_configuration_contract(self):
        self.login('admin')
        rows = self.client.get('/api/printers').get_json()['printers']
        self.assertEqual(next(row for row in rows if row['ip'] == '192.0.2.2')['community'], 'pending-secret')
        detail = self.client.get('/api/printer/192.0.2.1').get_json()
        self.assertEqual(detail['community'], 'polled-secret')

    def test_anonymous_is_denied_before_any_telemetry(self):
        for endpoint in ('/api/printers', '/api/printer/192.0.2.1'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('-secret', response.get_data(as_text=True))

    def test_office_restrictions_still_apply(self):
        user = self.users['viewer']
        with database.db_connection(commit=True) as conn:
            conn.execute('UPDATE users SET allowed_offices=? WHERE id=?', (json.dumps(['allowed']), user.id))
        self.login('viewer')
        response = self.client.get('/api/printer/192.0.2.3')
        self.assertEqual(response.status_code, 403)
        rows = self.client.get('/api/printers').get_json()['printers']
        self.assertNotIn('192.0.2.3', [row['ip'] for row in rows])

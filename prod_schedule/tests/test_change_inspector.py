"""Admin can correct the inspector of a submitted report; nobody else can."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

JOB = 'MELBOURNE|PO-1|RSV1'
CSRF = 'test-csrf-token'


class ChangeInspectorTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, display, role in (('admin', '', 'admin'), ('murphy', 'Murphy', 'lead'),
                                        ('yu', '', 'inspector'), ('hq1', '', 'hq')):
                conn.execute('INSERT INTO users (username,display_name,password_hash,role) VALUES (?,?,?,?)',
                             (name, display, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [
            {'inspector_name': 'murphy', 'result': 'Pass', 'inspection_date': '2026-10-01', 'submitted_by': 'murphy'},
            {'inspector_name': 'admin', 'result': 'Pass', 'inspection_date': '2026-10-02', 'submitted_by': 'admin'},
        ]})

    def client_as(self, username):
        client = app.app.test_client()
        with client.session_transaction() as s:
            s['user_id'] = self.ids[username]
            s['_csrf_token'] = CSRF
        return client

    def change(self, client, index, inspector):
        return client.post(f'/inspect/{JOB}/report/{index}/inspector',
                           data={'_csrf_token': CSRF, 'inspector_id': self.ids[inspector]})

    def records(self):
        return app.load_json(app.INSPECTIONS_CACHE, {})[JOB]

    def test_admin_changes_inspector_and_change_is_recorded(self):
        response = self.change(self.client_as('admin'), 1, 'murphy')
        self.assertEqual(response.status_code, 302)
        rec = self.records()[1]
        self.assertEqual(rec['inspector_name'], 'Murphy')
        [change] = rec['inspector_changes']
        self.assertEqual((change['from'], change['to'], change['by']), ('admin', 'Murphy', 'admin'))
        self.assertEqual(rec['submitted_by'], 'admin')          # who submitted stays true
        self.assertEqual(self.records()[0]['inspector_name'], 'murphy')

        # the KPI now counts both reports for Murphy and none for admin
        with db_conn() as conn:
            users = conn.execute('SELECT id, username, display_name, role FROM users').fetchall()
        stats = dict(app.inspector_kpis({JOB: self.records()}, {}, users, ['2026-W40']))
        self.assertEqual(list(stats), ['Murphy'])
        self.assertEqual(stats['Murphy']['total'], 2)

        with mock.patch.object(app, 'find_job', return_value={'job_key': JOB, 'Item Code': 'RSV1',
                                                             'Item Description': 'Valve'}):
            page = self.client_as('admin').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('检验员由 admin 改为 Murphy', page)

    def test_only_inspector_accounts_can_be_chosen(self):
        self.change(self.client_as('admin'), 1, 'hq1')
        self.assertEqual(self.records()[1]['inspector_name'], 'admin')
        self.assertNotIn('inspector_changes', self.records()[1])

    def test_non_admins_cannot_change_and_bad_index_is_404(self):
        for who in ('murphy', 'yu', 'hq1'):
            self.assertEqual(self.change(self.client_as(who), 1, 'murphy').status_code, 403)
        self.assertEqual(self.records()[1]['inspector_name'], 'admin')
        self.assertEqual(self.change(self.client_as('admin'), 5, 'murphy').status_code, 404)


if __name__ == '__main__':
    unittest.main()

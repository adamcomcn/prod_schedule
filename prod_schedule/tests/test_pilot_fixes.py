"""Regression tests for the pilot-blocking bugs fixed before the QC team trial."""
import io
import os
import sys
import tempfile
import unittest
from datetime import date

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from helpers import assign_job
from werkzeug.security import generate_password_hash

HEADERS = ['Daemco Purchase Order', 'Item Code', 'Item Description',
           'Supplier', 'Quantity', 'QA BRTs Sent?']
JOB_KEY = 'MELBOURNE|PO-1|RSV100'


class PilotFixTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app._login_failures.clear()
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments',
                          'outstanding_jobs', 'schedule_uploads'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('inspector-test', generate_password_hash('inspector-test-password'),
                 'inspector'))
        assign_job(JOB_KEY, 'inspector-test')
        schedule = {'MELBOURNE': [
            HEADERS, ['PO-1', 'RSV100', 'Valve DN100', 'Supplier A', '10', 'NO']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        self.client = app.app.test_client()
        with db_conn() as conn:
            user_id = conn.execute(
                "SELECT id FROM users WHERE username='inspector-test'").fetchone()[0]
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['_csrf_token'] = 'test-csrf-token'

    def submit(self, **overrides):
        data = {
            '_csrf_token': 'test-csrf-token',
            'region': 'MELBOURNE', 'order_number': 'PO-1', 'item_code': 'RSV100',
            'inspector_name': '张三', 'inspection_date': '2026-10-08',
            'quantity_inspected': '10', 'quantity_passed': '9',
            'packing_condition': 'Good', 'marking': 'OK',
            'result': 'Pass',
            'ev_result_vtrust': 'Pass', 'ev_notes_vtrust': '',
            'ev_file_vtrust': (io.BytesIO(b'fake video'), '现场视频.mp4'),
        }
        data.update(overrides)
        return self.client.post(f'/inspect/{JOB_KEY}/submit', data=data,
                                content_type='multipart/form-data')

    def test_submit_inspection_succeeds_and_saves_all_fields(self):
        response = self.submit()
        self.assertEqual(response.status_code, 302)
        record = app.load_json(app.INSPECTIONS_CACHE, {})[JOB_KEY][0]
        self.assertEqual(record['result'], 'Pass')
        self.assertEqual(record['quantity_passed'], '9')
        self.assertEqual(record['packing_condition'], 'Good')
        self.assertEqual(record['marking'], 'OK')
        self.assertEqual(record['submitted_by'], 'inspector-test')
        # Chinese file names keep their extension and original display name.
        self.assertEqual(record['file_names'], ['现场视频.mp4'])
        with db_conn() as conn:
            saved = conn.execute(
                'SELECT saved_name FROM inspection_attachments WHERE job_key=?',
                (JOB_KEY,)).fetchone()
        self.assertTrue(saved['saved_name'].endswith('.mp4'))

    def test_submit_without_result_is_rejected_and_nothing_saved(self):
        response = self.submit(result='')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE, {}), {})
        with db_conn() as conn:
            count = conn.execute('SELECT COUNT(*) FROM inspection_attachments').fetchone()[0]
        self.assertEqual(count, 0)

    def test_submit_with_bad_file_type_is_rejected_before_saving(self):
        response = self.submit(ev_file_vtrust=(io.BytesIO(b'x'), 'script.exe'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE, {}), {})

    def test_vtrust_status_reads_evidence_result(self):
        self.assertEqual(app.get_vtrust_status(
            [{'evidence': {'vtrust': {'result': 'Pass'}}}]), 'Pass')
        self.assertEqual(app.get_vtrust_status([{'vtrust_result': 'Fail'}]), 'Fail')
        self.assertEqual(app.get_vtrust_status([]), '')

    def test_tasks_page_tolerates_non_date_completion(self):
        with db_conn() as conn:
            for key, est in (('A|1|X', 'TBC'), ('A|2|Y', '2026/10/05'), ('A|3|Z', '')):
                conn.execute(
                    'INSERT INTO inspection_tasks (job_key, order_number, region, '
                    'est_completion, status) VALUES (?,?,?,?,?)',
                    (key, key, 'A', est, 'Pending'))
        self.assertEqual(self.client.get('/tasks').status_code, 200)

    def test_days_until_handles_month_end(self):
        self.assertEqual(app.days_until('2026-10-02', date(2026, 9, 29)), 3)
        self.assertIsNone(app.days_until('TBC', date(2026, 9, 29)))

    def test_login_lockout_is_per_username(self):
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('other-user', generate_password_hash('other-user-password'), 'inspector'))
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['_csrf_token'] = 'test-csrf-token'
        for _ in range(6):
            client.post('/login', data={'_csrf_token': 'test-csrf-token',
                                        'username': 'inspector-test', 'password': 'wrong'})
        response = client.post('/login', data={'_csrf_token': 'test-csrf-token',
                                               'username': 'other-user',
                                               'password': 'other-user-password'})
        self.assertEqual(response.status_code, 302)

    def test_save_json_is_atomic(self):
        path = os.path.join(app.DATA_DIR, 'atomic-test.json')
        app.save_json(path, {'a': 1})
        self.assertEqual(app.load_json(path), {'a': 1})
        self.assertFalse(os.path.exists(path + '.tmp'))

    def test_dashboard_uses_local_chartjs(self):
        self.assertTrue(os.path.exists(
            os.path.join(app.BASE_DIR, 'static', 'vendor', 'chart.umd.min.js')))
        with open(os.path.join(app.BASE_DIR, 'templates', 'dashboard.html'),
                  encoding='utf-8') as f:
            self.assertNotIn('cdn.jsdelivr.net', f.read())


if __name__ == '__main__':
    unittest.main()

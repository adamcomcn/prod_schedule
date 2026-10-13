"""Two reports submitted at the same moment are both kept (inspection file read-modify-write)."""
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from helpers import assign_job
from werkzeug.security import generate_password_hash

HEADERS = ['Daemco Purchase Order', 'Item Code', 'Item Description', 'Supplier', 'Quantity', 'QA BRTs Sent?']
JOB_KEY = 'MELBOURNE|PO-1|RSV100'


class ConcurrentSubmitTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'outstanding_jobs'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('lead1',?,'lead')",
                         (generate_password_hash('x' * 12),))
            self.uid = conn.execute("SELECT id FROM users WHERE username='lead1'").fetchone()[0]
        assign_job(JOB_KEY, 'lead1')
        schedule = {'MELBOURNE': [HEADERS, ['PO-1', 'RSV100', 'Valve DN100', 'Supplier A', '10', 'NO']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})

    def submit(self, inspector, results):
        client = app.app.test_client()
        with client.session_transaction() as s:
            s['user_id'] = self.uid
            s['_csrf_token'] = 'tok'
        data = {'_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'PO-1', 'item_code': 'RSV100',
                'inspector_name': inspector, 'inspection_date': '2026-10-14', 'result': 'Pass',
                'ev_result_vtrust': 'Pass', 'ev_file_vtrust': (io.BytesIO(b'video'), f'{inspector}.mp4')}
        results[inspector] = client.post(f'/inspect/{JOB_KEY}/submit', data=data,
                                         content_type='multipart/form-data').status_code

    def test_report_saved_meanwhile_is_kept_and_numbers_differ(self):
        # A reads the inspection file, then pauses (as if uploading photos) while B submits in full.
        a_paused, b_done = threading.Event(), threading.Event()
        original = app.readable_report_number

        def slow_for_a(record, index):
            if record.get('inspector_name') == 'A' and not a_paused.is_set():
                a_paused.set()
                b_done.wait(10)
            return original(record, index)

        results = {}
        with mock.patch.object(app, 'readable_report_number', side_effect=slow_for_a), \
                mock.patch.object(app, '_send_review_request_email'):
            a = threading.Thread(target=self.submit, args=('A', results))
            a.start()
            self.assertTrue(a_paused.wait(10))
            self.submit('B', results)
            b_done.set()
            a.join(10)
        self.assertEqual(results, {'A': 302, 'B': 302})

        records = app.load_json(app.INSPECTIONS_CACHE, {})[JOB_KEY]
        self.assertEqual([r['inspector_name'] for r in records], ['B', 'A'])       # B is not lost
        self.assertEqual([r['report_no'][-2:] for r in records], ['-1', '-2'])     # A took the next number
        with db_conn() as conn:
            files = {r['original_name']: r['insp_index'] for r in conn.execute(
                'SELECT original_name, insp_index FROM inspection_attachments WHERE job_key=?', (JOB_KEY,))}
        self.assertEqual(files, {'B.mp4': 0, 'A.mp4': 1})                          # files follow their report


class UpdateConfigTests(unittest.TestCase):
    def test_only_the_given_keys_change(self):
        config = app.load_config()
        config['backup_emails'] = 'qa@example.com'
        app.save_json(app.CONFIG_FILE, config)
        app.update_config(backup_last_day='2026-10-14')
        config = app.load_config()
        self.assertEqual(config['backup_emails'], 'qa@example.com')
        self.assertEqual(config['backup_last_day'], '2026-10-14')


if __name__ == '__main__':
    unittest.main()

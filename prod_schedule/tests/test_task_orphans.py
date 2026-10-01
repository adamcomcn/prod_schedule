"""Tasks whose order left the schedule: marker, filter, bulk close, reminders."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Quantity', 'Estimated Completion Date']


class OrphanTaskTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        soon = (date.today() + timedelta(days=3)).isoformat()
        with db_conn() as conn:
            conn.execute('DELETE FROM users'); conn.execute('DELETE FROM inspection_tasks')
            for u, role in (('lead', 'lead'), ('insp', 'inspector')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (u, generate_password_hash(u * 6), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            for n, (sup, status) in enumerate((('XM', 'Pending'), ('', 'Pending'), ('HR', 'Pending'),
                                               ('', 'Completed')), 1):
                conn.execute(
                    "INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,status,supplier) "
                    "VALUES (?,?,?,?,?,?,?)", (f'MEL|PO{n}|I{n}', f'D{n}', 'MEL', f'I{n}', soon, status, sup))
            self.tid = {r['order_number']: r['id'] for r in conn.execute('SELECT id, order_number FROM inspection_tasks')}
        # only PO1 is still in the schedule
        app.save_json(app.CURRENT_FILE, {'MEL': [H, ['D1', 'PO1', 'I1', '5', soon]]})
        app.save_json(app.PREVIOUS_FILE, {'MEL': [H, ['D1', 'PO1', 'I1', '5', soon]]})
        cfg = app.load_config(); cfg['modules'] = {}; app.save_json(app.CONFIG_FILE, cfg)

    def client(self, user):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]; s['_csrf_token'] = 'tok'
        return c

    def test_marker_and_filter(self):
        page = self.client('lead').get('/tasks').get_data(as_text=True)
        self.assertEqual(page.count('已不在排期'), 2)          # PO2, PO3 (PO4 is completed)
        only = self.client('lead').get('/tasks?orphan=1').get_data(as_text=True)
        self.assertIn('D2', only); self.assertIn('D3', only)
        self.assertNotIn('>D1<', only.replace(' ', ''))

    def test_bulk_close_removes_from_open_work(self):
        r = self.client('insp').post('/tasks/close', data={'_csrf_token': 'tok', 'task_ids': [str(self.tid['D2'])]})
        self.assertEqual(r.status_code, 403)
        r = self.client('lead').post('/tasks/close', data={
            '_csrf_token': 'tok', 'task_ids': [str(self.tid['D2']), str(self.tid['D4'])]})
        self.assertEqual(r.status_code, 302)
        with db_conn() as conn:
            got = {r['order_number']: r['status'] for r in conn.execute('SELECT order_number, status FROM inspection_tasks')}
        self.assertEqual(got['D2'], 'Closed')
        self.assertEqual(got['D4'], 'Completed')                 # finished tasks are left alone
        page = self.client('lead').get('/tasks?orphan=1').get_data(as_text=True)
        self.assertNotIn('D2', page)
        with app.app.test_request_context():                     # closed tasks are not reminded
            app.send_due_reminders()
        with db_conn() as conn:
            marks = {r['order_number']: r['reminder_est'] for r in conn.execute(
                'SELECT order_number, reminder_est FROM inspection_tasks')}
        self.assertFalse(marks['D2'])

    def test_all_suppliers_available_beyond_top_eight(self):
        with db_conn() as conn:
            for n in range(5, 15):
                conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,status,supplier) "
                             "VALUES (?,?,?,?,?,?)", (f'MEL|PO{n}|I{n}', f'D{n}', 'MEL', f'I{n}', 'Pending', f'SUP{n}'))
        page = self.client('lead').get('/tasks').get_data(as_text=True)
        self.assertIn('HR', page)
        self.assertIn('id="supToggle"', page)
        self.assertIn('SUP14', page)


if __name__ == '__main__':
    unittest.main()

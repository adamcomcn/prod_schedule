"""Admin bulk-closes fully shipped lines that have no QA BRT report (with a reason), and can reopen them."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

HEADERS = ['Daemco Purchase Order', 'Item Code', 'Item Description', 'Supplier', 'Quantity', 'QA BRTs Sent?']
KEYS = ['DI FITTING|DPL1|ITEM1', 'DI FITTING|DPL2|ITEM2', 'DI FITTING|DPL3|ITEM3']


class OutstandingCloseTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'outstanding_jobs'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('boss', 'admin'), ('murphy', 'lead')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            for key in KEYS:
                _, po, item = key.split('|')
                conn.execute('INSERT INTO outstanding_jobs (job_key, sheet, order_number, item_code, item_desc) '
                             'VALUES (?,?,?,?,?)', (key, 'DI FITTING', po, item, f'Fitting {item}'))
        schedule = {'DI FITTING': [HEADERS, ['DPL9', 'ITEM9', 'Other', 'Rainbow', '5', 'NO']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})

    def client(self, user='boss'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]
            s['_csrf_token'] = 'tok'
        return c

    def close(self, keys, reason='pre_launch', note='', user='boss'):
        return self.client(user).post('/outstanding/close', data={
            '_csrf_token': 'tok', 'job_key': keys, 'reason': reason, 'note': note})

    def row(self, key):
        with db_conn() as conn:
            return dict(conn.execute('SELECT * FROM outstanding_jobs WHERE job_key=?', (key,)).fetchone())

    def test_admin_closes_selected_lines_with_reason(self):
        page = self.client().get('/').get_data(as_text=True)
        self.assertIn('id="closeForm"', page)
        self.assertEqual(page.count('class="out-check"'), 3)
        self.assertEqual(self.close(KEYS[:2]).status_code, 302)
        row = self.row(KEYS[0])
        self.assertEqual((row['closed'], row['closed_by'], row['close_reason'], row['completed']),
                         (1, 'boss', 'pre_launch', 0))
        page = self.client().get('/').get_data(as_text=True)
        self.assertEqual(page.count('class="out-check"'), 1)                 # only DPL3 still open
        self.assertIn('2 已关闭', page)
        self.assertIn('系统上线前已出货', page)                                # history shows why
        self.assertIn('(0/1)', page)                                          # closed lines left out of the rate

    def test_other_needs_a_note_and_reason_is_required(self):
        self.close(KEYS[:1], reason='other')
        self.close(KEYS[:1], reason='')
        self.assertEqual(self.row(KEYS[0])['closed'], 0)
        self.close(KEYS[:1], reason='other', note='Sample order, no inspection')
        self.assertEqual(self.row(KEYS[0])['close_note'], 'Sample order, no inspection')

    def test_only_admin(self):
        lead = self.client('murphy')
        self.assertNotIn('id="closeForm"', lead.get('/').get_data(as_text=True))
        self.assertEqual(self.close(KEYS, user='murphy').status_code, 403)
        self.assertEqual(self.row(KEYS[0])['closed'], 0)

    def test_reopen(self):
        self.close(KEYS[:1])
        self.client().post('/outstanding/reopen', data={'_csrf_token': 'tok', 'job_key': KEYS[0]})
        row = self.row(KEYS[0])
        self.assertEqual((row['closed'], row['close_reason']), (0, ''))
        self.assertEqual(self.client().get('/').get_data(as_text=True).count('class="out-check"'), 3)

    def test_inspected_line_is_not_closed(self):
        with db_conn() as conn:
            conn.execute("UPDATE outstanding_jobs SET completed=1 WHERE job_key=?", (KEYS[0],))
        self.close(KEYS[:1])
        self.assertEqual(self.row(KEYS[0])['closed'], 0)


if __name__ == '__main__':
    unittest.main()

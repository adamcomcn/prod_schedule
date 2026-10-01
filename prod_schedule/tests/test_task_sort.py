"""The task table can be sorted by its column headers."""
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


class TaskSortTests(unittest.TestCase):
    def test_est_iso_reads_dates_followed_by_text(self):
        self.assertEqual(app.est_iso('2026/6/15 ready for ship'), '2026-06-15')
        self.assertEqual(app.est_iso('TBC'), '')

    def test_headers_and_row_sort_values_are_rendered(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users'); conn.execute('DELETE FROM inspection_tasks')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('adm',?,'admin')",
                         (generate_password_hash('x' * 12),))
            uid = conn.execute("SELECT id FROM users WHERE username='adm'").fetchone()[0]
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,quantity,est_completion,status) "
                         "VALUES ('M|P|I','D1','MEL','I','150','2026/6/15 ready for ship','Pending')")
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = uid
        page = c.get('/tasks').get_data(as_text=True)
        for key in ('region', 'order', 'item', 'desc', 'qty', 'est', 'days', 'result', 'assignee', 'status'):
            self.assertIn(f'class="sortable" data-key="{key}"', page)
        self.assertIn('data-est="2026-06-15"', page)


if __name__ == '__main__':
    unittest.main()

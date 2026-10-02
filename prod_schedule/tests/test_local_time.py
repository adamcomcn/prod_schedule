"""Timestamps are stored in UTC and shown in the viewer's / readers' zones."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class LocalTimeTests(unittest.TestCase):
    def test_dual_zone_for_emails_and_pdf(self):
        # 2 Oct 2026: Melbourne still on AEST (UTC+10); DST starts 4 Oct
        self.assertEqual(app.dual_zone_time('2026-10-02 00:52'), '2026-10-02 08:52 北京 / 10:52 Melbourne')
        # after DST starts Melbourne is UTC+11
        self.assertEqual(app.dual_zone_time('2026-10-05T00:52:00'), '2026-10-05 08:52 北京 / 11:52 Melbourne')
        # different calendar day in the two zones -> full date for both
        self.assertEqual(app.dual_zone_time('2026-10-02 15:30'), '2026-10-02 23:30 北京 / 2026-10-03 01:30 Melbourne')
        self.assertEqual(app.dual_zone_time('02 Oct 2026 00:52'), '2026-10-02 08:52 北京 / 10:52 Melbourne')

    def test_local_time_markup(self):
        html = str(app.local_time('2026-10-02 00:52'))
        self.assertIn('datetime="2026-10-02T00:52:00+00:00"', html)
        self.assertIn('2026-10-02 00:52 UTC', html)                  # shown until the browser converts it
        self.assertEqual(str(app.local_time('2026-10-02')), '2026-10-02')  # dates are not converted
        self.assertEqual(str(app.local_time('<b>')), '&lt;b&gt;')

    def test_inspection_page_marks_assignment_time(self):
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM inspection_tasks')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss','x','admin')")
            uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            conn.execute("INSERT INTO inspection_tasks (job_key, region, order_number, item_code, status, "
                         "assigned_to, assigned_by, assigned_at) VALUES ('MELBOURNE|PO-1|X', 'MELBOURNE', "
                         "'DPL1', 'X', 'Pending', ?, 'murphy', '2026-10-02 00:52')", (uid,))
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': [['Order Number', 'Daemco Purchase Order', 'Item Code'],
                                                       ['DPL1', 'PO-1', 'X']]})
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = uid
        page = client.get('/inspect/MELBOURNE|PO-1|X').get_data(as_text=True)
        self.assertIn('<time class="js-local" datetime="2026-10-02T00:52:00+00:00">', page)
        tasks = client.get('/tasks?scope=all').get_data(as_text=True)
        self.assertIn('data-time="2026-10-02T00:52:00+00:00"', tasks)


if __name__ == '__main__':
    unittest.main()
